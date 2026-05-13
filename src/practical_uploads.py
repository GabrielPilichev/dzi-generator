"""Safety helpers for practical-task student uploads.

Policy notes (referenced by tests):
- ZIP uploads are accepted as opaque blobs and are NEVER extracted or
  inspected server-side; this avoids zip-bomb and zip-slip risks.
- Allowed extensions are matched against the FINAL filename suffix only,
  with a dangerous-extension blocklist applied as defense-in-depth. This
  rejects double-extension tricks like "virus.pdf.exe" or "archive.zip.exe".
- Stored filenames are always token-based, never the user-provided name.
- File contents are streamed straight to disk under the configured upload
  root; the route enforces ``ensure_under_upload_root`` before writing.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_UPLOAD_ROOT = Path("data/uploads/practical")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILENAME_LENGTH = 200
ALLOWED_EXTENSIONS = frozenset({
    ".xlsx",
    ".ods",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".html",
    ".css",
    ".js",
    ".zip",
})
DANGEROUS_EXTENSIONS = frozenset({
    ".exe",
    ".sh",
    ".py",
    ".php",
    ".bat",
    ".cmd",
    ".com",
    ".msi",
    ".ps1",
})

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_FILENAME_CHARS = re.compile(r"[\\/:*?\"<>|]+")


class PracticalUploadError(ValueError):
    """Raised when an upload value is unsafe or outside policy."""


@dataclass(frozen=True)
class PracticalStoredPath:
    stored_path: str
    absolute_path: Path
    original_filename: str
    stored_filename: str


def sanitize_original_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise PracticalUploadError("filename must be a string")

    normalized = filename.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    cleaned = _CONTROL_CHARS.sub("", basename).strip()
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")

    if not cleaned or cleaned in {".", ".."}:
        raise PracticalUploadError("filename must not be empty")
    if cleaned in {".", ".."} or ".." in Path(cleaned).parts:
        raise PracticalUploadError("filename must not traverse directories")
    if len(cleaned) > MAX_FILENAME_LENGTH:
        raise PracticalUploadError("filename is too long")
    return cleaned


def validate_upload_extension(filename: str) -> str:
    safe_name = sanitize_original_filename(filename)
    extension = Path(safe_name).suffix.lower()
    if not extension:
        raise PracticalUploadError("filename must have an extension")
    if extension in DANGEROUS_EXTENSIONS or extension not in ALLOWED_EXTENSIONS:
        raise PracticalUploadError(f"extension is not allowed: {extension}")
    return extension


def validate_upload_size(size_bytes: int, *, max_bytes: int = MAX_UPLOAD_BYTES) -> int:
    if not isinstance(size_bytes, int):
        raise PracticalUploadError("size must be an integer")
    if size_bytes <= 0:
        raise PracticalUploadError("upload must not be empty")
    if size_bytes > max_bytes:
        raise PracticalUploadError("upload exceeds maximum size")
    return size_bytes


def _safe_identifier(value: int | str, name: str) -> str:
    text = str(value).strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise PracticalUploadError(f"{name} must be a safe identifier")
    return text


def ensure_under_upload_root(path: Path, upload_root: Path) -> Path:
    root = upload_root.resolve(strict=False)
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PracticalUploadError("path must stay under upload root") from exc
    return candidate


def build_practical_upload_path(
    *,
    upload_root: Path | str = DEFAULT_UPLOAD_ROOT,
    attempt_id: int | str,
    exam_task_id: int | str,
    submission_id: int | str,
    original_filename: str,
    size_bytes: int,
    token: str | None = None,
) -> PracticalStoredPath:
    safe_name = sanitize_original_filename(original_filename)
    extension = validate_upload_extension(safe_name)
    validate_upload_size(size_bytes)

    attempt_part = _safe_identifier(attempt_id, "attempt_id")
    task_part = _safe_identifier(exam_task_id, "exam_task_id")
    submission_part = _safe_identifier(submission_id, "submission_id")
    token_part = _safe_identifier(token or uuid.uuid4().hex, "token")
    stored_filename = f"{token_part}{extension}"

    root = Path(upload_root)
    relative_path = Path(attempt_part) / task_part / submission_part / stored_filename
    absolute_path = ensure_under_upload_root(root / relative_path, root)
    stored_path = str(Path("data/uploads/practical") / relative_path)

    if stored_filename == safe_name:
        raise PracticalUploadError("stored filename must not reuse original filename")

    return PracticalStoredPath(
        stored_path=stored_path,
        absolute_path=absolute_path,
        original_filename=safe_name,
        stored_filename=stored_filename,
    )
