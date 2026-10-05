"""Synthetic question-candidate datasets for tests. No real source or question."""

from __future__ import annotations

import hashlib
from pathlib import Path

RUN_ID = "physics-g8-test-run"
SECTION = "physics-g8-synthetic-section"
TOPIC = "physics-g8-synthetic-topic"
OTHER_TOPIC = "physics-g8-other-topic"

PROMPTS = (
    "Which synthetic statement about the first test object is correct?",
    "How many synthetic units does the second measuring device show today?",
    "What happens to the third synthetic sample when the lamp is switched off?",
    "Where does the fourth synthetic experiment keep its reference weight?",
)


def review(status="pending", **changes):
    data = {"status": status, "reviewer": None, "reviewed_at": None, "notes": ""}
    if status != "pending":
        data.update(reviewer="Test Reviewer", reviewed_at="2026-01-11T08:00:00Z")
    data.update(changes)
    return data


def candidate(number=1, *, status="pending", source_id="official-source",
              transformation="extracted", topic=TOPIC, **changes):
    data = {
        "id": f"{RUN_ID}-{number:03d}",
        "section_slug": SECTION, "topic_slug": topic,
        "source_id": source_id, "transformation": transformation,
        "type": "multiple_choice",
        "prompt": PROMPTS[(number - 1) % len(PROMPTS)] if number <= len(PROMPTS)
        else f"Synthetic filler question number {number} about nothing in particular?",
        "options": {"A": f"First {number}", "B": f"Second {number}",
                    "C": f"Third {number}", "D": f"Fourth {number}"},
        "correct": "B",
        "review": review(status),
    }
    if transformation == "extracted":
        data["source_ref"] = {"exam_session": "synthetic-2025", "question_number": number}
    if source_id == "third-party-source" and transformation == "original_from_research":
        data["research"] = {"concept": "Synthetic concept"}
    data.update(changes)
    return data


def dataset(*candidates, finished=True):
    return {
        "format_version": 1, "kind": "question_candidates",
        "run": {
            "id": RUN_ID,
            "started_at": "2026-01-10T09:00:00Z",
            "finished_at": "2026-01-10T09:20:00Z" if finished else None,
            "collector": "test-collector",
            "subject": "physics", "grade": 8,
            "curriculum": {"education_type": "synthetic-f4-test",
                           "title": "physics test curriculum"},
            "targets": [{
                "section": {"slug": SECTION, "title": "physics test section"},
                "topics": [{"slug": TOPIC, "title": "physics test topic"},
                           {"slug": OTHER_TOPIC, "title": "physics other topic"}],
            }],
            "limits": {"max_topics": 3, "max_pages": 40, "max_candidates": 100,
                       "max_candidates_per_source": 20, "max_runtime_minutes": 30},
            "pages_inspected": 2,
            "stop_reason": "topic_complete" if finished else None,
        },
        "sources": [
            {"id": "official-source", "type": "official",
             "url": "https://authority.example.invalid/paper.pdf",
             "title": "Synthetic official paper", "publisher": "Synthetic authority",
             "retrieved_at": "2026-01-10T09:02:00Z"},
            {"id": "third-party-source", "type": "third_party",
             "url": "https://lessons.example.invalid/topic",
             "title": "Synthetic third-party page",
             "retrieved_at": "2026-01-10T09:10:00Z"},
        ],
        "candidates": list(candidates) if candidates else [candidate(1)],
    }


def directory_state(directory: Path) -> dict:
    """Name -> SHA-256 of every file, to prove nothing was written or created."""
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(directory).iterdir()) if path.is_file()}
