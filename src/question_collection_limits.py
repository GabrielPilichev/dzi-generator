"""Hard limits of one question-collection run.

A run covers exactly one subject and one grade: the candidate file has a single
``subject`` and a single ``grade`` field, so that limit is structural. Every
other limit is a positive integer that must be stated explicitly and may never
exceed its absolute ceiling. Nothing here means "unlimited": zero, negative,
missing, null, boolean and non-integer values are all rejected.

This module performs no network or database access. ``CollectionBudget`` is
the counter a future collector must consult before every page and candidate.
See docs/QUESTION_COLLECTION_PIPELINE.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, fields
from typing import Callable

SUBJECTS_PER_RUN = 1
GRADES_PER_RUN = 1

DEFAULT_LIMITS = {
    "max_topics": 3,
    "max_pages": 40,
    "max_candidates": 100,
    "max_candidates_per_source": 20,
    "max_runtime_minutes": 30,
}

# Ceilings are enforced by ``CollectionLimits`` itself; no caller, CLI flag or
# candidate file can raise them.
ABSOLUTE_CEILINGS = {
    "max_topics": 10,
    "max_pages": 100,
    "max_candidates": 250,
    "max_candidates_per_source": 50,
    "max_runtime_minutes": 60,
}

STOP_REASONS = (
    "candidate_limit",
    "page_limit",
    "topic_complete",
    "runtime_limit",
    "manual_stop",
    "no_more_sources",
)


class CollectionLimitError(ValueError):
    """A limit is missing, malformed, non-positive or above its ceiling."""


class CollectionLimitReached(RuntimeError):
    """A running collection hit a hard limit; ``stop_reason`` says which."""

    def __init__(self, stop_reason: str, message: str):
        super().__init__(message)
        self.stop_reason = stop_reason


@dataclass(frozen=True)
class CollectionLimits:
    max_topics: int
    max_pages: int
    max_candidates: int
    max_candidates_per_source: int
    max_runtime_minutes: int

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            ceiling = ABSOLUTE_CEILINGS[item.name]
            # bool is an int subclass; True must not pass as 1.
            if type(value) is not int:
                raise CollectionLimitError(f"{item.name} must be an integer, not {value!r}")
            if value < 1:
                raise CollectionLimitError(
                    f"{item.name} must be at least 1 (got {value}); there is no unlimited mode"
                )
            if value > ceiling:
                raise CollectionLimitError(
                    f"{item.name} {value} exceeds the absolute ceiling {ceiling}"
                )
        if self.max_candidates_per_source > self.max_candidates:
            raise CollectionLimitError(
                "max_candidates_per_source must not exceed max_candidates"
            )

    @classmethod
    def from_mapping(cls, value: object) -> "CollectionLimits":
        """Build from a stored ``limits`` object; every key is required, none extra."""
        names = [item.name for item in fields(cls)]
        if not isinstance(value, dict):
            raise CollectionLimitError("limits must be an object")
        missing = [name for name in names if name not in value]
        unknown = sorted(set(value) - set(names))
        if missing:
            raise CollectionLimitError(
                f"limits is missing {', '.join(missing)}; a missing limit is never unlimited"
            )
        if unknown:
            raise CollectionLimitError(f"limits has unexpected {', '.join(unknown)}")
        return cls(**{name: value[name] for name in names})

    def as_dict(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}


class CollectionBudget:
    """Counts pages, candidates and elapsed time against ``CollectionLimits``.

    A collector calls ``record_page`` before fetching a page and
    ``record_candidate`` before keeping a candidate. Either raises
    ``CollectionLimitReached`` once the corresponding limit is exhausted, and
    the budget stays stopped afterwards.
    """

    def __init__(self, limits: CollectionLimits,
                 clock: Callable[[], float] = time.monotonic):
        if not isinstance(limits, CollectionLimits):
            raise CollectionLimitError("a budget needs validated CollectionLimits")
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.pages_inspected = 0
        self.candidate_count = 0
        self.candidates_per_source: dict[str, int] = {}
        self.stop_reason: str | None = None

    def _stop(self, reason: str, message: str):
        self.stop_reason = reason
        raise CollectionLimitReached(reason, message)

    def _check_open(self):
        if self.stop_reason is not None:
            raise CollectionLimitReached(
                self.stop_reason, f"collection already stopped: {self.stop_reason}"
            )
        if self._clock() - self._started >= self.limits.max_runtime_minutes * 60:
            self._stop("runtime_limit",
                       f"runtime limit of {self.limits.max_runtime_minutes} minutes reached")

    def record_page(self) -> None:
        self._check_open()
        if self.pages_inspected >= self.limits.max_pages:
            self._stop("page_limit", f"page limit of {self.limits.max_pages} reached")
        self.pages_inspected += 1

    def source_has_room(self, source_id: str) -> bool:
        used = self.candidates_per_source.get(source_id, 0)
        return used < self.limits.max_candidates_per_source

    def record_candidate(self, source_id: str) -> None:
        self._check_open()
        if self.candidate_count >= self.limits.max_candidates:
            self._stop("candidate_limit",
                       f"candidate limit of {self.limits.max_candidates} reached")
        if not self.source_has_room(source_id):
            # One exhausted source does not end the run; the collector moves on.
            raise CollectionLimitError(
                f"source {source_id!r} already has "
                f"{self.limits.max_candidates_per_source} candidates"
            )
        self.candidate_count += 1
        self.candidates_per_source[source_id] = self.candidates_per_source.get(source_id, 0) + 1

    def finish(self, reason: str) -> str:
        """Close the run with an explicit reason unless a limit already closed it."""
        if reason not in STOP_REASONS:
            raise CollectionLimitError(f"unknown stop reason: {reason!r}")
        if self.stop_reason is None:
            self.stop_reason = reason
        return self.stop_reason
