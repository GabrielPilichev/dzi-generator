#!/usr/bin/env python3
"""Read-only QA audit for DZI question image asset files."""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_MAPPING_PATH = Path("docs/reviews/confirmed_dzi_image_question_mappings.json")
DEFAULT_IMAGE_DIR = Path("web/static/img/questions/dzi")


@dataclass(frozen=True)
class ImageAssetAuditRow:
    filename: str
    dimensions_label: str
    file_size: int
    mapping_status: str
    suspicious: tuple[str, ...]


@dataclass(frozen=True)
class ImageAssetAuditReport:
    rows: tuple[ImageAssetAuditRow, ...]
    expected_count: int
    present_expected_count: int
    unexpected_count: int
    missing_expected: list[str]


def read_png_dimensions(path: Path) -> tuple[int, int] | None:
    """Read PNG width/height from the signature and IHDR header."""
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None

    if len(header) < 24 or not header.startswith(PNG_SIGNATURE):
        return None

    chunk_length, chunk_type, width, height = struct.unpack(">I4sII", header[8:24])
    if chunk_length != 13 or chunk_type != b"IHDR":
        return None
    return width, height


def expected_asset_filenames(payload: dict[str, object]) -> list[str]:
    expected: list[str] = []
    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        return expected

    for entry in mappings:
        if not isinstance(entry, dict) or entry.get("needs_image") is not True:
            continue
        filename = entry.get("expected_asset_filename")
        if isinstance(filename, str) and filename and filename not in expected:
            expected.append(filename)
    return expected


def audit_image_assets(
    payload: dict[str, object],
    image_dir: Path,
    *,
    min_width: int = 200,
    min_height: int = 200,
    min_file_size: int = 1024,
) -> ImageAssetAuditReport:
    expected = expected_asset_filenames(payload)
    expected_set = set(expected)
    rows: list[ImageAssetAuditRow] = []

    files = sorted((path for path in image_dir.iterdir() if path.is_file()), key=lambda path: path.name) if image_dir.exists() else []
    present_filenames = {path.name for path in files}

    for path in files:
        suspicious: list[str] = []
        file_size = path.stat().st_size
        dimensions = read_png_dimensions(path)

        if dimensions is None:
            dimensions_label = "n/a"
            suspicious.append("not_png")
        else:
            width, height = dimensions
            dimensions_label = f"{width}x{height}"
            if width < min_width or height < min_height:
                suspicious.append("small_dimensions")

        if file_size < min_file_size:
            suspicious.append("small_file")

        mapping_status = "matches confirmed mapping" if path.name in expected_set else "not in confirmed mapping"
        rows.append(ImageAssetAuditRow(
            filename=path.name,
            dimensions_label=dimensions_label,
            file_size=file_size,
            mapping_status=mapping_status,
            suspicious=tuple(suspicious),
        ))

    missing_expected = [filename for filename in expected if filename not in present_filenames]
    unexpected_count = sum(1 for path in files if path.name not in expected_set)

    return ImageAssetAuditReport(
        rows=tuple(rows),
        expected_count=len(expected),
        present_expected_count=len(expected) - len(missing_expected),
        unexpected_count=unexpected_count,
        missing_expected=missing_expected,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit DZI question image assets without writing files.")
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument("--min-width", type=int, default=200)
    parser.add_argument("--min-height", type=int, default=200)
    parser.add_argument("--min-file-size", type=int, default=1024)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with args.mapping.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    report = audit_image_assets(
        payload,
        args.image_dir,
        min_width=args.min_width,
        min_height=args.min_height,
        min_file_size=args.min_file_size,
    )

    print("DZI question image QA report")
    print(f"expected={report.expected_count}")
    print(f"present_expected={report.present_expected_count}")
    print(f"unexpected={report.unexpected_count}")
    if report.missing_expected:
        print("missing_expected=" + ", ".join(report.missing_expected))

    for row in report.rows:
        suspicious = ",".join(row.suspicious) if row.suspicious else "none"
        print(
            f"{row.filename} | dimensions={row.dimensions_label} | "
            f"size={row.file_size} | mapping={row.mapping_status} | suspicious={suspicious}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
