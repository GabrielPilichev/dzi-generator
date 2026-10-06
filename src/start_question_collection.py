"""Start a bounded question-collection run: write its empty candidate dataset.

This command only creates a local JSON file. It fetches nothing and never
touches a database. The subject, grade, target topics and the page and
candidate limits must all be given explicitly; every limit is checked against
the absolute ceilings in ``src.question_collection_limits``.

Target topics are named by their stored slugs and resolved against a reviewed
structure file (docs/SUBJECT_STRUCTURE_IMPORT.md), so the run records the exact
section and topic titles the reviewed importer will later expect.

See docs/QUESTION_COLLECTION_PIPELINE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.import_subject_structure import validate_structure
from src.question_candidates import new_dataset
from src.question_collection_limits import DEFAULT_LIMITS, CollectionLimits


def targets_from_structure(structure: dict, subject: str, grade: int, topic_slugs: list) -> tuple:
    """Return ``(curriculum, targets)`` for the requested topics, in structure order."""
    if validate_structure(structure) != subject or structure["grade"] != grade:
        raise ValueError("the structure file is for a different subject or grade")
    if len(set(topic_slugs)) != len(topic_slugs):
        raise ValueError("a topic is listed twice")
    wanted = set(topic_slugs)
    targets = []
    for section in structure["sections"]:
        topics = [{"slug": topic["slug"], "title": topic["title"]}
                  for topic in section["topics"] if topic["slug"] in wanted]
        if topics:
            targets.append({"section": {"slug": section["slug"], "title": section["title"]},
                            "topics": topics})
            wanted -= {topic["slug"] for topic in topics}
    if wanted:
        raise ValueError("not in the structure file: " + ", ".join(sorted(wanted)))
    curriculum = {"education_type": structure["curriculum"]["education_type"],
                  "title": structure["curriculum"]["title"]}
    return curriculum, targets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True, help="canonical subject id")
    parser.add_argument("--grade", type=int, required=True)
    parser.add_argument("--topics", required=True,
                        help="comma-separated stored topic slugs to target")
    parser.add_argument("--structure", type=Path, required=True,
                        help="reviewed structure file that lists those topics")
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-candidates", type=int, required=True)
    parser.add_argument("--max-per-source", type=int, required=True)
    parser.add_argument("--max-topics", type=int, default=DEFAULT_LIMITS["max_topics"])
    parser.add_argument("--max-runtime-minutes", type=int,
                        default=DEFAULT_LIMITS["max_runtime_minutes"])
    parser.add_argument("--run-id", help="lowercase hyphenated id; default is derived from "
                                         "subject, grade and the current UTC time")
    parser.add_argument("--collector", help="who or what runs the collection")
    parser.add_argument("--skills", help="optional comma-separated skill/coverage tags the run "
                                         "may use in research.skill (at most 12)")
    parser.add_argument("--output", type=Path, required=True,
                        help="candidate dataset to create; must not exist")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        limits = CollectionLimits(
            max_topics=args.max_topics, max_pages=args.max_pages,
            max_candidates=args.max_candidates,
            max_candidates_per_source=args.max_per_source,
            max_runtime_minutes=args.max_runtime_minutes,
        )
        topic_slugs = [slug.strip() for slug in args.topics.split(",") if slug.strip()]
        if not topic_slugs:
            raise ValueError("--topics must name at least one topic")
        structure = json.loads(args.structure.read_text(encoding="utf-8"))
        curriculum, targets = targets_from_structure(
            structure, args.subject, args.grade, topic_slugs)
        dataset = new_dataset(
            run_id=args.run_id or f"{args.subject}-g{args.grade}-{now:%Y%m%d-%H%M%S}",
            started_at=now.isoformat().replace("+00:00", "Z"),
            subject=args.subject, grade=args.grade, curriculum=curriculum,
            targets=targets, limits=limits, collector=args.collector,
            skill_vocabulary=([tag.strip() for tag in args.skills.split(",")]
                              if args.skills is not None else None),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(dataset, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Collection run rejected: {exc}\n")
    print(json.dumps({"created": str(args.output), "run": dataset["run"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
