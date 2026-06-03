#!/usr/bin/env python3
"""Extract candidate PNG pages for confirmed DZI image-dependent questions."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    from .validate_confirmed_dzi_image_files import (
        DEFAULT_IMAGE_DIR,
        ready_mapping_entries,
        validate_image_dir,
    )
    from .validate_confirmed_dzi_image_mappings import DEFAULT_MAPPING_PATH
except ImportError:
    from validate_confirmed_dzi_image_files import (
        DEFAULT_IMAGE_DIR,
        ready_mapping_entries,
        validate_image_dir,
    )
    from validate_confirmed_dzi_image_mappings import DEFAULT_MAPPING_PATH


DEFAULT_PDF_DIR = Path("data/source_pdfs")
DEFAULT_DPI = 200


@dataclass(frozen=True)
class CandidatePlan:
    source_slug: str
    official_question_number: int
    pdf_path: Path | None
    output_path: Path
    pdf_page: int | None
    skip_reason: str | None = None
    output_exists: bool = False

    @property
    def label(self) -> str:
        return f"{self.source_slug} Q{self.official_question_number}"


@dataclass
class RunSummary:
    selected: int = 0
    written: int = 0
    would_write: int = 0
    skipped_existing: int = 0
    missing_pages: int = 0
    missing_pdfs: int = 0
    errors: int = 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render candidate PNG images from official DZI PDFs for confirmed "
            "image-dependent question mappings."
        )
    )
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument("--dry-run", action="store_true", help="List planned outputs without writing files.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing PNG candidates.")
    parser.add_argument("--source-slug", help="Only extract mappings for this DZI source slug.")
    parser.add_argument("--question", type=int, help="Only extract this official question number.")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="Render resolution for PDF pages.")
    return parser.parse_args(argv)


def load_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"mapping file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("mapping JSON root must be an object")
    return payload


def safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def mapping_pdf_page(entry: dict[str, object]) -> int | None:
    value = safe_int(entry.get("pdf_page"))
    if value is not None and value > 0:
        return value
    return None


def canonical_filename(source_slug: str, official_question_number: int) -> str:
    return f"{source_slug}_q{official_question_number}.png"


def locate_pdf(pdf_dir: Path, source_slug: str) -> tuple[Path | None, str | None]:
    pdf_path = pdf_dir / f"{source_slug}_exam.pdf"
    if pdf_path.is_file():
        return pdf_path, None
    return None, f"PDF not found: {pdf_path}"


def iter_filtered_entries(
    payload: dict[str, object],
    *,
    source_slug: str | None,
    question: int | None,
) -> list[dict[str, object]]:
    entries = []
    for entry in ready_mapping_entries(payload):
        entry_source = entry.get("source_slug")
        entry_number = safe_int(entry.get("official_question_number"))
        if source_slug and entry_source != source_slug:
            continue
        if question is not None and entry_number != question:
            continue
        entries.append(entry)
    return entries


def collect_extraction_plans(
    payload: dict[str, object],
    *,
    pdf_dir: Path,
    image_dir: Path,
    source_slug: str | None = None,
    question: int | None = None,
) -> list[CandidatePlan]:
    plans: list[CandidatePlan] = []
    for entry in iter_filtered_entries(payload, source_slug=source_slug, question=question):
        entry_source = str(entry["source_slug"])
        entry_number = int(entry["official_question_number"])

        pdf_path, pdf_issue = locate_pdf(pdf_dir, entry_source)
        pdf_page = mapping_pdf_page(entry)
        skip_reason = None
        if pdf_page is None:
            skip_reason = "missing pdf_page in confirmed mapping"
            pdf_path = None
        elif pdf_issue is not None:
            skip_reason = pdf_issue

        output_path = image_dir / canonical_filename(entry_source, entry_number)
        plans.append(
            CandidatePlan(
                source_slug=entry_source,
                official_question_number=entry_number,
                pdf_path=pdf_path,
                output_path=output_path,
                pdf_page=pdf_page,
                skip_reason=skip_reason,
                output_exists=output_path.exists(),
            )
        )
    return plans


def render_candidate(plan: CandidatePlan, *, overwrite: bool, dpi: int) -> str:
    if plan.output_path.exists() and not overwrite:
        return "skipped_existing"
    if plan.pdf_path is None:
        raise FileNotFoundError(plan.skip_reason or f"PDF missing for {plan.label}")
    if plan.pdf_page is None:
        raise ValueError(f"{plan.label}: missing pdf_page in confirmed mapping")

    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for extraction; install requirements.txt") from exc

    with fitz.open(str(plan.pdf_path)) as document:
        page_index = plan.pdf_page - 1
        if page_index < 0 or page_index >= len(document):
            raise ValueError(f"{plan.label}: pdf_page {plan.pdf_page} is outside {plan.pdf_path} page range")

        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pixmap = document[page_index].get_pixmap(matrix=matrix, alpha=False)
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(plan.output_path))
    return "written"


def format_plan_line(plan: CandidatePlan, *, dry_run: bool, overwrite: bool) -> str:
    if plan.output_exists and not overwrite:
        return f"skip existing: {plan.label} -> {plan.output_path}"
    if plan.skip_reason:
        return f"error, skip: {plan.label}: {plan.skip_reason}"
    action = "would render" if dry_run else "render"
    return f"{action}: {plan.label} page {plan.pdf_page}: {plan.pdf_path} -> {plan.output_path}"


def print_run_header(args: argparse.Namespace, plans: list[CandidatePlan]) -> None:
    print("DZI image candidate extraction")
    print("==============================")
    print(f"mapping: {args.mapping}")
    print(f"pdf_dir: {args.pdf_dir}")
    print(f"image_dir: {args.image_dir}")
    print(f"dry_run: {'yes' if args.dry_run else 'no'}")
    print(f"overwrite: {'yes' if args.overwrite else 'no'}")
    if args.source_slug:
        print(f"source_slug: {args.source_slug}")
    if args.question is not None:
        print(f"question: {args.question}")
    print(f"dpi: {args.dpi}")
    print(f"selected mappings: {len(plans)}")


def run(args: argparse.Namespace) -> int:
    image_dir_errors = validate_image_dir(args.image_dir)
    if args.dpi <= 0:
        image_dir_errors.append("dpi must be positive")
    if image_dir_errors:
        for error in image_dir_errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    try:
        payload = load_mapping(args.mapping)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    plans = collect_extraction_plans(
        payload,
        pdf_dir=args.pdf_dir,
        image_dir=args.image_dir,
        source_slug=args.source_slug,
        question=args.question,
    )
    summary = RunSummary(selected=len(plans))

    print_run_header(args, plans)
    print()
    print("Planned candidates")
    print("------------------")
    if not plans:
        print("- no matching ready image mappings")
    for plan in plans:
        print(f"- {format_plan_line(plan, dry_run=args.dry_run, overwrite=args.overwrite)}")
        if plan.output_exists and not args.overwrite:
            summary.skipped_existing += 1
        elif plan.skip_reason:
            if "missing pdf_page" in plan.skip_reason:
                summary.missing_pages += 1
            else:
                summary.missing_pdfs += 1
        elif args.dry_run:
            summary.would_write += 1

    if not args.dry_run:
        print()
        print("Extraction results")
        print("------------------")
        for plan in plans:
            if plan.output_exists and not args.overwrite:
                continue
            if plan.skip_reason:
                continue
            try:
                result = render_candidate(plan, overwrite=args.overwrite, dpi=args.dpi)
            except (OSError, RuntimeError, ValueError) as exc:
                summary.errors += 1
                print(f"- error: {plan.label}: {exc}")
                continue
            if result == "written":
                summary.written += 1
                print(f"- wrote: {plan.output_path}")
            elif result == "skipped_existing":
                summary.skipped_existing += 1
                print(f"- skip existing: {plan.output_path}")

    print()
    print("Summary")
    print("-------")
    print(f"selected: {summary.selected}")
    print(f"written: {summary.written}")
    print(f"would_write: {summary.would_write}")
    print(f"skipped_existing: {summary.skipped_existing}")
    print(f"missing_pages: {summary.missing_pages}")
    print(f"missing_pdfs: {summary.missing_pdfs}")
    print(f"errors: {summary.errors}")
    has_blockers = summary.missing_pages or summary.missing_pdfs or summary.errors
    print(f"extract_exit={0 if args.dry_run or not has_blockers else 1}")

    if args.dry_run:
        return 0
    return 1 if has_blockers else 0


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
