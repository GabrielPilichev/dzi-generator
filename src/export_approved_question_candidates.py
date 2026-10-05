"""Export the approved candidates of a collection run as reviewed import batches.

Only ``approved`` candidates leave the candidate file. The output is one batch
per topic in the format of ``src.import_reviewed_subject_questions`` plus one
provenance file. This command writes JSON files only: it has no database write
mode, and ``--db`` is opened read-only for the same checks the validator runs.
Importing stays a separate, deliberate step with the existing importer.

See docs/QUESTION_COLLECTION_PIPELINE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.import_reviewed_subject_questions import LETTERS, validate_batch
from src.question_candidates import (
    APPROVED, MULTIPLE_CHOICE, NEEDS_REVISION, PENDING, CandidateDatasetError,
    reviewed_source_prefix, validate_dataset,
)
from src.subject_browsing import public_slug

PROVENANCE_KIND = "question_candidate_provenance"

# Candidate data the reviewed import format, and so the database, has no place
# for. The database keeps only the batch ``source`` identifier and the question
# number; the provenance file maps that pair back to everything listed here.
NOT_STORED_IN_DATABASE = (
    "candidate id", "source type", "source url", "source title", "source publisher",
    "source retrieved_at", "transformation", "source_ref", "research", "reuse_exception",
    "explanation", "difficulty", "review (reviewer, reviewed_at, notes)", "run manifest",
)


class ExportError(ValueError):
    """The export was refused; nothing was written."""


def _exportable(candidate) -> str | None:
    """None when the reviewed importer can take the candidate, else the reason."""
    if candidate["type"] != MULTIPLE_CHOICE:
        return f"type {candidate['type']} is not supported by the reviewed importer"
    if set(candidate["options"]) != set(LETTERS):
        return "the reviewed importer needs exactly the options A, B, C, D"
    return None


def build_export(dataset: dict, *, db_path=None, allow_undecided: bool = False) -> dict:
    """Plan the export in memory. Returns ``{"files": {name: content}, "report": ...}``."""
    validation = validate_dataset(dataset, db_path=db_path)
    if not validation.ok:
        raise CandidateDatasetError(validation)
    run = dataset["run"]
    if run["finished_at"] is None:
        raise ExportError("the run is not finished: set finished_at and stop_reason first")
    subject, grade, run_id = run["subject"], run["grade"], run["id"]
    sources = {source["id"]: source for source in dataset["sources"]}

    files: dict = {}
    batches, not_exported, undecided_topics = [], [], []
    for target in run["targets"]:
        for topic in target["topics"]:
            members = [c for c in dataset["candidates"] if c["topic_slug"] == topic["slug"]]
            chosen = []
            for candidate in members:
                status = candidate["review"]["status"]
                reason = _exportable(candidate) if status == APPROVED else f"review status {status}"
                if reason is None:
                    chosen.append(candidate)
                else:
                    not_exported.append({"candidate_id": candidate["id"], "reason": reason})
            if not chosen:
                continue
            if any(c["review"]["status"] in (PENDING, NEEDS_REVISION) for c in members):
                undecided_topics.append(topic["slug"])

            source_id = reviewed_source_prefix(subject, run_id) + public_slug(
                subject, grade, topic["slug"])
            batch = {
                "format_version": 1, "subject": subject, "grade": grade, "source": source_id,
                "curriculum": {"subject": subject, "grade": grade,
                               "education_type": run["curriculum"]["education_type"],
                               "title": run["curriculum"]["title"]},
                "section": {"slug": target["section"]["slug"],
                            "title": target["section"]["title"], "grade": grade},
                "topic": {"slug": topic["slug"], "title": topic["title"], "grade": grade},
                "questions": [{
                    "number": number, "subject": subject, "grade": grade,
                    "topic_slug": topic["slug"], "type": MULTIPLE_CHOICE,
                    "prompt": candidate["prompt"],
                    "options": {letter: candidate["options"][letter] for letter in LETTERS},
                    "correct": candidate["correct"],
                } for number, candidate in enumerate(chosen, 1)],
            }
            validate_batch(batch)       # the existing importer's own contract
            name = f"{run_id}.{public_slug(subject, grade, topic['slug'])}.json"
            files[name] = batch
            questions = []
            for number, candidate in enumerate(chosen, 1):
                entry = {"number": number, "candidate_id": candidate["id"],
                         "transformation": candidate["transformation"],
                         "source": sources[candidate["source_id"]],
                         "review": candidate["review"],
                         "flags": [item.as_dict() for item in validation.flags_for(candidate["id"])]}
                for key in ("source_ref", "research", "reuse_exception", "explanation", "difficulty"):
                    if key in candidate:
                        entry[key] = candidate[key]
                for item in entry["flags"]:
                    item.pop("candidate_id", None)
                questions.append(entry)
            batches.append({"file": name, "source": source_id, "topic_slug": topic["slug"],
                            "questions": questions})

    if undecided_topics and not allow_undecided:
        raise ExportError(
            "pending or needs_revision candidates remain in: " + ", ".join(undecided_topics)
            + ". The importer fixes the set of question numbers of a source once it is "
            "imported, so finish the review first (or pass --allow-undecided and never "
            "re-export this topic from this run)"
        )
    if not batches:
        raise ExportError("no approved candidate can be exported")

    provenance_name = f"{run_id}.provenance.json"
    files[provenance_name] = {
        "format_version": 1, "kind": PROVENANCE_KIND, "run": run,
        "not_stored_in_database": list(NOT_STORED_IN_DATABASE),
        "batches": batches, "not_exported": not_exported,
    }
    return {"files": files, "report": {
        "run": run_id,
        "exported_questions": sum(len(batch["questions"]) for batch in batches),
        "batches": [{"file": batch["file"], "source": batch["source"],
                     "questions": len(batch["questions"])} for batch in batches],
        "provenance_file": provenance_name,
        "not_exported": not_exported,
        "undecided_topics": undecided_topics,
        "provenance_not_stored_in_database": list(NOT_STORED_IN_DATABASE),
        "flags_on_exported": sum(len(q["flags"]) for batch in batches for q in batch["questions"]),
    }}


def write_export(export: dict, output_dir: Path) -> list:
    """Write the planned files; refuse to replace any existing file."""
    output_dir = Path(output_dir)
    existing = [name for name in export["files"] if (output_dir / name).exists()]
    if existing:
        raise ExportError("refusing to overwrite: " + ", ".join(sorted(existing)))
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in export["files"].items():
        path = output_dir / name
        with path.open("x", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="reviewed candidate dataset")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="directory for the batch and provenance files")
    parser.add_argument("--db", type=Path,
                        help="optional database, read-only: ownership and existing-duplicate checks")
    parser.add_argument("--allow-undecided", action="store_true",
                        help="export a topic that still has pending/needs_revision candidates")
    args = parser.parse_args(argv)
    try:
        dataset = json.loads(args.input.read_text(encoding="utf-8"))
        export = build_export(dataset, db_path=args.db, allow_undecided=args.allow_undecided)
        written = write_export(export, args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Export rejected: {exc}\n")
    report = export["report"]
    report["written"] = [str(path) for path in written]
    report["next_step"] = ("dry-run each batch with python3 -B -m "
                           "src.import_reviewed_subject_questions --input <batch> --db <database>")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
