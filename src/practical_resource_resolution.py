"""Bounded, read-only resolution of DZI practical resource references.

The stored ZIP syntax is ``data/reference/archive.zip::member/path``. No ZIP
member is extracted to disk. Callers must first look up the resource record;
this module never accepts a request path or performs authorization.

Stored references are logical: ``data/reference/...`` normally lives in the
checkout, but a deployment may keep that one tree elsewhere (``reference_root``).
"""

from __future__ import annotations

import zipfile
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from unicodedata import normalize

from src.validate_practical_task_batch import ZIP_RESOURCE_SEPARATOR


# The current five archives are <= 5.9 MB; the largest referenced member is
# < 3 MB and the highest observed ratio is < 1.5:1. Leave room for future
# reviewed resources without allowing large/strongly expanding members.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_ARCHIVE_ENTRIES = 10_000
SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
REFERENCE_PREFIX = ("data", "reference")


class ResourceResolutionError(ValueError):
    """A safe reason code for logging or unavailable-resource display."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedResource:
    kind: str  # file or zip_member
    path: Path
    filename: str
    member: str | None = None


def _relative_parts(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ResourceResolutionError("unsafe_path")
    if (raw.startswith(("/", "~")) or "\\" in raw or ":" in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or normalize("NFC", raw) != raw):
        raise ResourceResolutionError("unsafe_path")
    parts = tuple(raw.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ResourceResolutionError("unsafe_path")
    if PurePosixPath(raw).as_posix() != raw:
        raise ResourceResolutionError("unsafe_path")
    return parts


def _approved_file(raw: str, project_root: Path, allowed_roots: tuple[Path, ...],
                   reference_root: Path | None = None) -> Path:
    parts = _relative_parts(raw)
    base = project_root
    if reference_root is not None and parts[:2] == REFERENCE_PREFIX:
        # Only this prefix is relocated; it never falls back to the checkout.
        base, parts = Path(reference_root), parts[2:]
    try:
        candidate = base.joinpath(*parts).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ResourceResolutionError("unsafe_path") from exc
    if not any(candidate.is_relative_to(Path(root).resolve(strict=False))
               for root in allowed_roots):
        raise ResourceResolutionError("unsafe_path")
    try:
        is_file = candidate.is_file()
    except (OSError, RuntimeError) as exc:
        raise ResourceResolutionError("missing_file") from exc
    if not is_file:
        raise ResourceResolutionError("missing_file")
    return candidate


def _zip_info(archive: zipfile.ZipFile, member: str) -> zipfile.ZipInfo:
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ResourceResolutionError("too_many_entries")
    matching = [info for info in entries if info.filename == member]
    if not matching:
        raise ResourceResolutionError("missing_member")
    if len(matching) != 1:
        raise ResourceResolutionError("ambiguous_member")
    info = matching[0]
    member_type = stat.S_IFMT(info.external_attr >> 16)
    if info.is_dir() or member_type == stat.S_IFDIR:
        raise ResourceResolutionError("directory_member")
    if member_type == stat.S_IFLNK:
        raise ResourceResolutionError("symlink_member")
    if member_type not in {0, stat.S_IFREG}:
        raise ResourceResolutionError("unsupported_member_type")
    if info.flag_bits & 0x1:
        raise ResourceResolutionError("encrypted_member")
    if info.compress_type not in SUPPORTED_COMPRESSION:
        raise ResourceResolutionError("unsupported_compression")
    if info.file_size > MAX_MEMBER_BYTES or info.file_size < 0:
        raise ResourceResolutionError("oversized_member")
    if info.file_size and (info.compress_size <= 0 or
                           info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
        raise ResourceResolutionError("compression_ratio")
    return info


def resolve_resource_reference(
    raw: str | None, *, project_root: Path, allowed_roots: tuple[Path, ...],
    reference_root: Path | None = None,
) -> ResolvedResource:
    """Validate the stored reference and metadata without loading member bytes.

    ``reference_root`` relocates ``data/reference/...``; it must also be one of
    ``allowed_roots``, which stays the only containment check.
    """
    if not isinstance(raw, str) or not raw:
        raise ResourceResolutionError("unsafe_path")
    if ZIP_RESOURCE_SEPARATOR not in raw:
        path = _approved_file(raw, project_root, allowed_roots, reference_root)
        return ResolvedResource("file", path, path.name)
    if raw.count(ZIP_RESOURCE_SEPARATOR) != 1:
        raise ResourceResolutionError("unsafe_path")
    archive_raw, member_raw = raw.split(ZIP_RESOURCE_SEPARATOR)
    if member_raw.endswith("/"):
        raise ResourceResolutionError("directory_member")
    _relative_parts(member_raw)
    archive_path = _approved_file(archive_raw, project_root, allowed_roots, reference_root)
    if archive_path.suffix.lower() != ".zip":
        raise ResourceResolutionError("invalid_archive")
    try:
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ResourceResolutionError("oversized_archive")
        with zipfile.ZipFile(archive_path) as archive:
            _zip_info(archive, member_raw)
    except ResourceResolutionError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ResourceResolutionError("invalid_archive") from exc
    return ResolvedResource("zip_member", archive_path,
                            PurePosixPath(member_raw).name, member_raw)


def read_zip_member(resource: ResolvedResource) -> bytes:
    """Read one already-resolved member, rechecking metadata and actual size."""
    if resource.kind != "zip_member" or resource.member is None:
        raise ResourceResolutionError("invalid_reference")
    try:
        if resource.path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ResourceResolutionError("oversized_archive")
        with zipfile.ZipFile(resource.path) as archive:
            info = _zip_info(archive, resource.member)
            with archive.open(info) as stream:
                data = stream.read(MAX_MEMBER_BYTES + 1)
    except ResourceResolutionError:
        raise
    except (OSError, RuntimeError, NotImplementedError, EOFError,
            zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ResourceResolutionError("unreadable_member") from exc
    if len(data) > MAX_MEMBER_BYTES or len(data) != info.file_size:
        raise ResourceResolutionError("oversized_or_truncated_member")
    return data
