"""Validate a question-candidate dataset. Read-only: there is no write-to-database mode.

Exit status is 0 when the dataset has no errors (it may still carry flags for
the reviewer) and 1 otherwise. ``--db`` opens the given database read-only to
check that the target sections and topics belong to the subject and to compare
candidates with the questions already stored for that subject.

``--annotate`` writes a copy of the dataset with the computed flags stored on
each candidate, for the reviewer. The input file is never changed.

See docs/QUESTION_COLLECTION_PIPELINE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.question_candidates import annotated_copy, validate_dataset


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="candidate dataset JSON")
    parser.add_argument("--db", type=Path,
                        help="optional database, opened read-only, for ownership and "
                             "existing-question duplicate checks")
    parser.add_argument("--annotate", type=Path, metavar="OUTPUT",
                        help="write a copy of the dataset with computed flags; must not exist")
    args = parser.parse_args(argv)
    try:
        dataset = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Validation rejected: {exc}\n")
    report = validate_dataset(dataset, db_path=args.db)
    result = report.as_dict()
    if args.annotate is not None:
        if not report.ok:
            result["annotated"] = None
        else:
            try:
                with args.annotate.open("x", encoding="utf-8") as handle:
                    json.dump(annotated_copy(dataset, report), handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except OSError as exc:
                parser.exit(1, f"Validation rejected: cannot write {args.annotate}: {exc}\n")
            result["annotated"] = str(args.annotate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
