#!/usr/bin/env python3
"""Check expected image files for confirmed DZI image-question mappings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .validate_confirmed_dzi_image_mappings import (
        DEFAULT_MAPPING_PATH,
        READY_CONFIDENCE,
        all_entries,
        validate_safe_filename,
    )
except ImportError:
    from validate_confirmed_dzi_image_mappings import (
        DEFAULT_MAPPING_PATH,
        READY_CONFIDENCE,
        all_entries,
        validate_safe_filename,
    )


DEFAULT_IMAGE_DIR = Path("web/static/img/questions/dzi")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check extracted asset files for confirmed DZI image mappings."
    )
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument(
        "--require-files",
        action="store_true",
        help="Treat missing expected PNG files as validation errors.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Alias for --require-files.",
    )
    return parser.parse_args()


def load_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"mapping file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("mapping JSON root must be an object")
    return payload


def validate_image_dir(path: Path) -> list[str]:
    normalised = path.as_posix()
    errors = []
    if path.is_absolute():
        errors.append("image-dir must be repo-relative")
    if "vault/Generated/Quizzes" in normalised:
        errors.append("image-dir must not point into vault/Generated/Quizzes")
    if any(part in {"", ".", ".."} for part in path.parts):
        errors.append("image-dir must not contain empty/current/parent path segments")
    return errors


def ready_mapping_entries(payload: dict[str, object]) -> list[dict[str, object]]:
    entries = []
    for _, _, entry in all_entries(payload):
        if entry.get("needs_image") is True and entry.get("confidence") in READY_CONFIDENCE:
            entries.append(entry)
    return entries


def expected_filename(entry: dict[str, object]) -> str | None:
    source_slug = entry.get("source_slug")
    official_number = entry.get("official_question_number")
    if not isinstance(source_slug, str) or not isinstance(official_number, int):
        return None
    return f"{source_slug}_q{official_number}.png"


def validate_image_files(
    payload: dict[str, object],
    image_dir: Path,
    *,
    require_files: bool,
) -> tuple[list[str], dict[str, int], list[str], list[str]]:
    errors = validate_image_dir(image_dir)
    missing: list[str] = []
    ready_entries = ready_mapping_entries(payload)
    expected_filenames: set[str] = set()
    present_count = 0

    for entry in ready_entries:
        source_slug = entry.get("source_slug", "unknown-source")
        official_number = entry.get("official_question_number", "unknown-number")
        label = f"{source_slug} Q{official_number}"
        filename = entry.get("expected_asset_filename")
        expected = expected_filename(entry)

        if not isinstance(filename, str) or not filename:
            errors.append(f"{label}: expected_asset_filename is required")
            continue
        if expected is not None and filename != expected:
            errors.append(f"{label}: expected_asset_filename must be {expected}, got {filename}")
        for filename_error in validate_safe_filename(filename):
            errors.append(f"{label}: expected_asset_filename {filename_error}")
        expected_filenames.add(filename)

        image_path = image_dir / filename
        if image_path.exists():
            if image_path.is_file():
                present_count += 1
            else:
                errors.append(f"{label}: expected image path is not a file: {image_path}")
        else:
            missing.append(str(image_path))
            if require_files:
                errors.append(f"{label}: missing expected image file: {image_path}")

    unexpected = []
    if image_dir.exists() and image_dir.is_dir():
        unexpected = [
            str(path)
            for path in sorted(image_dir.iterdir())
            if path.is_file() and path.name not in expected_filenames
        ]

    summary = {
        "ready_mappings": len(ready_entries),
        "files_present": present_count,
        "files_missing": len(missing),
        "unexpected_files": len(unexpected),
    }
    return errors, summary, missing, unexpected


def print_summary(
    mapping_path: Path,
    image_dir: Path,
    summary: dict[str, int],
    errors: list[str],
    missing: list[str],
    unexpected: list[str],
    *,
    require_files: bool,
) -> None:
    print("Confirmed DZI asset file validation")
    print("===================================")
    print(f"mapping: {mapping_path}")
    print(f"image_dir: {image_dir}")
    print(f"require_files: {'yes' if require_files else 'no'}")
    print(f"ready mappings: {summary['ready_mappings']}")
    print(f"assets found: {summary['files_present']}")
    print(f"assets missing: {summary['files_missing']}")
    print(f"unexpected files: {summary['unexpected_files']}")
    print(f"validation errors: {len(errors)}")
    print(f"validation_exit={0 if not errors else 1}")
    if missing:
        print()
        print("Missing expected files")
        print("----------------------")
        for path in missing[:25]:
            print(f"- {path}")
        if len(missing) > 25:
            print(f"- ... {len(missing) - 25} more")
    if unexpected:
        print()
        print("Unexpected files")
        print("----------------")
        for path in unexpected[:25]:
            print(f"- {path}")
        if len(unexpected) > 25:
            print(f"- ... {len(unexpected) - 25} more")
    if errors:
        print()
        print("Validation errors")
        print("-----------------")
        for error in errors:
            print(f"- {error}")


def main() -> int:
    args = parse_args()
    require_files = bool(args.require_files or args.strict)
    try:
        payload = load_mapping(args.mapping)
        errors, summary, missing, unexpected = validate_image_files(
            payload,
            args.image_dir,
            require_files=require_files,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(
        args.mapping,
        args.image_dir,
        summary,
        errors,
        missing,
        unexpected,
        require_files=require_files,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
