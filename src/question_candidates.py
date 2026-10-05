"""Question-candidate datasets: format, validation, duplicate and quality checks.

A dataset is one JSON file for one collection run: the run manifest, the source
records and the candidate questions with their review state. Nothing in this
module fetches from the network or writes a database. The optional database
checks open the file read-only.

``validate_dataset`` separates two outcomes:

- errors: the dataset breaks the format, a hard limit or the source policy;
- flags: something a human reviewer must look at (possible duplicates, quality
  doubts). Flags never delete or reject a candidate by themselves.

The checks are deterministic text rules. They do not establish that a question
is correct, well posed or pedagogically sound; that is the reviewer's decision.
See docs/QUESTION_COLLECTION_PIPELINE.md.
"""

from __future__ import annotations

import copy
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlsplit

from src.question_collection_limits import (
    STOP_REASONS, CollectionLimitError, CollectionLimits,
)
from src.subject_browsing import STORED_SLUG, is_namespaced_slug, namespaced_slug_prefix
from src.subject_content import canonical_subject, section_ownership, topic_ownership
from src.subject_registry import get_subject, subject_supports_grade

FORMAT_VERSION = 1
KIND = "question_candidates"


@dataclass(frozen=True)
class SourcePolicy:
    """How wording from a source category may be reused.

    These are conservative review controls, not legal conclusions.
    """
    verbatim_allowed: bool
    research_required: bool


# A new source category is one more entry here; the file format does not change.
SOURCE_TYPES = {
    "official": SourcePolicy(verbatim_allowed=True, research_required=False),
    "third_party": SourcePolicy(verbatim_allowed=False, research_required=True),
}

EXTRACTED = "extracted"
ADAPTED = "adapted"
ORIGINAL = "original_from_research"
TRANSFORMATIONS = (EXTRACTED, ADAPTED, ORIGINAL)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
NEEDS_REVISION = "needs_revision"
REVIEW_STATES = (PENDING, APPROVED, REJECTED, NEEDS_REVISION)

MULTIPLE_CHOICE = "multiple_choice"
TRUE_FALSE = "true_false"
SHORT_ANSWER = "short_answer"
QUESTION_TYPES = (MULTIPLE_CHOICE, TRUE_FALSE, SHORT_ANSWER)

DIFFICULTIES = ("easy", "medium", "hard")
OPTION_LETTERS = "ABCDEF"
MIN_OPTIONS = 2
EXPECTED_OPTIONS = 4               # what the reviewed importer accepts
PLACEHOLDER_OPTIONS = frozenset({"-", "—", "[]"})   # same set the importer rejects

NEAR_DUPLICATE_RATIO = 0.90
NEAR_DUPLICATE_OPTION_OVERLAP = 0.5
MIN_PROMPT_CHARS = 12
MIN_PROMPT_WORDS = 3
MAX_PROMPT_CHARS = 1500
MAX_RESEARCH_NOTES_CHARS = 500
MIN_LEAK_CHARS = 4
MAX_ID_CHARS = 120

# An approved candidate must not carry one of these flags.
BLOCKING_FLAGS = frozenset({"duplicate_exact", "duplicate_existing_exact"})

_SENTENCE_PUNCTUATION = frozenset(".,;:!?…\"'`«»„“”‘’()[]{}")
_HTML_RESIDUE = re.compile(
    r"</?(?:p|br|div|span|b|i|u|strong|em|a|img|script|style|table|tr|td|th|ul|ol|li|sub|sup"
    r"|h[1-6]|font|input|label|option|select|form|iframe)(?:\s[^<>]*)?/?>"
    r"|&(?:[a-zA-Z]{2,8}|#x?[0-9a-fA-F]{1,6});"
    r"|javascript:",
    re.IGNORECASE,
)

_RUN_KEYS = ("id", "started_at", "finished_at", "subject", "grade", "curriculum",
             "targets", "limits", "pages_inspected", "stop_reason")
_SOURCE_KEYS = ("id", "type", "url", "title", "retrieved_at")
_CANDIDATE_KEYS = ("id", "section_slug", "topic_slug", "source_id", "transformation",
                   "type", "prompt", "review")
_CANDIDATE_OPTIONAL = ("source_ref", "research", "reuse_exception", "options", "correct",
                       "accepted_answers", "explanation", "difficulty", "flags")
_SOURCE_REF_KEYS = ("document_title", "exam_session", "question_number", "page")
_REVIEW_KEYS = ("status", "reviewer", "reviewed_at", "notes")


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    candidate_id: str | None = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.candidate_id is not None:
            data["candidate_id"] = self.candidate_id
        return data


@dataclass
class ValidationReport:
    errors: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code, message, candidate_id=None):
        self.errors.append(Finding(code, message, candidate_id))

    def flag(self, code, message, candidate_id=None):
        self.flags.append(Finding(code, message, candidate_id))

    def flags_for(self, candidate_id) -> list:
        return [item for item in self.flags if item.candidate_id == candidate_id]

    def codes(self, kind="errors") -> set:
        return {item.code for item in getattr(self, kind)}

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "counts": self.counts,
            "errors": [item.as_dict() for item in self.errors],
            "flags": [item.as_dict() for item in self.flags],
        }


class CandidateDatasetError(ValueError):
    """A dataset was rejected; ``report`` holds every error found."""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("; ".join(
            (f"[{item.candidate_id}] " if item.candidate_id else "") + item.message
            for item in report.errors))


# --------------------------------------------------------------------------- text helpers

def normalize_text(value: str) -> str:
    """Comparison form of a prompt or option: NFKC, case-folded, single spaces.

    Sentence punctuation and quotes become spaces. Operators and signs such as
    ``+ - * / = < >`` are kept, so "5 + 3" and "5 - 3" stay different.
    """
    text = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(" " if char in _SENTENCE_PUNCTUATION else char for char in text)
    return " ".join(text.split())


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def parse_timestamp(value):
    """An ISO 8601 timestamp with a UTC offset (``Z`` allowed), or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_http_url(value) -> bool:
    if not _is_text(value) or any(char.isspace() for char in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and "." in parts.hostname


def _url_key(value: str) -> str:
    parts = urlsplit(value)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}?{parts.query}"


def _shape(value, label, required, optional, report, code, candidate_id=None) -> bool:
    if not isinstance(value, dict):
        report.error(code, f"{label} must be an object", candidate_id)
        return False
    missing = [key for key in required if key not in value]
    unknown = sorted(set(value) - set(required) - set(optional))
    if missing:
        report.error(code, f"{label} is missing {', '.join(missing)}", candidate_id)
    if unknown:
        report.error(code, f"{label} has unexpected {', '.join(unknown)}", candidate_id)
    return not missing and not unknown


# --------------------------------------------------------------------------- run manifest

def _validate_run(run, report):
    """Validate the manifest; return the context later checks need, or None."""
    before = len(report.errors)
    if not _shape(run, "run", _RUN_KEYS, ("collector", "notes"), report, "run"):
        return None

    run_id = run["id"]
    if not (isinstance(run_id, str) and STORED_SLUG.fullmatch(run_id) and len(run_id) <= 80):
        report.error("run", "run id must be a lowercase hyphenated slug of at most 80 characters")
        run_id = None

    subject = run["subject"]
    if get_subject(subject) is None:
        report.error("unknown_subject", f"Unknown subject: {subject!r} (use the canonical id)")
        subject = None
    grade = run["grade"]
    if subject is not None and not subject_supports_grade(subject, grade):
        supported = ", ".join(str(value) for value in get_subject(subject).grades)
        report.error("invalid_grade",
                     f"grade must be an integer in the grades supported by {subject}: {supported}")
        grade = None

    curriculum = run["curriculum"]
    if _shape(curriculum, "curriculum", ("education_type", "title"), (), report, "run"):
        for key in ("education_type", "title"):
            if not _is_text(curriculum[key]):
                report.error("run", f"curriculum {key} must be non-empty, trimmed text")

    limits = None
    try:
        limits = CollectionLimits.from_mapping(run["limits"])
    except CollectionLimitError as exc:
        report.error("limit", str(exc))

    started = parse_timestamp(run["started_at"])
    if started is None:
        report.error("run", "started_at must be an ISO 8601 timestamp with a UTC offset")
    finished = None
    if run["finished_at"] is not None:
        finished = parse_timestamp(run["finished_at"])
        if finished is None:
            report.error("run", "finished_at must be null or an ISO 8601 timestamp with a UTC offset")
        elif started is not None and finished < started:
            report.error("run", "finished_at is earlier than started_at")
    stop_reason = run["stop_reason"]
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        report.error("run", f"stop_reason must be null or one of: {', '.join(STOP_REASONS)}")
    if (run["finished_at"] is None) != (stop_reason is None):
        report.error("run", "finished_at and stop_reason must be set together")

    pages = run["pages_inspected"]
    if type(pages) is not int or pages < 0:
        report.error("run", "pages_inspected must be a non-negative integer")
        pages = None
    elif limits is not None and pages > limits.max_pages:
        report.error("limit", f"pages_inspected {pages} exceeds max_pages {limits.max_pages}")

    for key in ("collector", "notes"):
        if key in run and not _is_text(run[key]):
            report.error("run", f"run {key} must be non-empty, trimmed text when present")

    topics = _validate_targets(run["targets"], subject, grade, limits, report)

    if len(report.errors) != before:
        return None
    if finished is not None and (finished - started).total_seconds() > limits.max_runtime_minutes * 60:
        # Recorded after the fact; the live guard is CollectionBudget.
        report.flag("runtime_limit_exceeded",
                    f"the run lasted longer than max_runtime_minutes {limits.max_runtime_minutes}")
    return {
        "run_id": run_id, "subject": subject, "grade": grade, "limits": limits,
        "started": started, "finished": finished, "pages": pages, "topics": topics,
        "collector": run.get("collector"),
    }


def _validate_targets(targets, subject, grade, limits, report) -> dict:
    """Return ``{topic_slug: section_slug}`` for well-formed targets."""
    topics: dict = {}
    if not isinstance(targets, list) or not targets:
        report.error("targets", "targets must be a non-empty array of sections with topics")
        return topics
    sections: set = set()

    def slug_ok(item, label):
        if not _shape(item, label, ("slug", "title"), (), report, "targets"):
            return False
        ok = True
        if subject is not None and grade is not None and not is_namespaced_slug(
                subject, grade, item["slug"]):
            report.error("slug", f"{label} slug {item['slug']!r} must be lowercase, hyphenated "
                                 f"and namespaced {namespaced_slug_prefix(subject, grade)}...")
            ok = False
        if not _is_text(item["title"]):
            report.error("targets", f"{label} title must be non-empty, trimmed text")
            ok = False
        return ok

    for position, target in enumerate(targets, 1):
        label = f"target {position}"
        if not _shape(target, label, ("section", "topics"), (), report, "targets"):
            continue
        section_ok = slug_ok(target["section"], f"{label} section")
        section_slug = target["section"].get("slug") if isinstance(target["section"], dict) else None
        if section_ok:
            if section_slug in sections:
                report.error("targets", f"Duplicate target section: {section_slug!r}")
            sections.add(section_slug)
        if not isinstance(target["topics"], list) or not target["topics"]:
            report.error("targets", f"{label} topics must be a non-empty array")
            continue
        for topic_position, topic in enumerate(target["topics"], 1):
            if not slug_ok(topic, f"{label} topic {topic_position}"):
                continue
            if topic["slug"] in topics:
                report.error("targets", f"Duplicate target topic: {topic['slug']!r}")
            elif section_ok:
                topics[topic["slug"]] = section_slug
    if limits is not None and len(topics) > limits.max_topics:
        report.error("limit", f"{len(topics)} target topics exceed max_topics {limits.max_topics}")
    return topics


# --------------------------------------------------------------------------- sources

def _validate_sources(sources, context, report) -> dict:
    """Return ``{source_id: source}`` for well-formed source records."""
    valid: dict = {}
    if not isinstance(sources, list):
        report.error("provenance", "sources must be an array")
        return valid
    urls: dict = {}
    for position, source in enumerate(sources, 1):
        label = f"source {position}"
        before = len(report.errors)
        if not _shape(source, label, _SOURCE_KEYS, ("publisher", "notes"), report, "provenance"):
            continue
        source_id = source["id"]
        if not (isinstance(source_id, str) and STORED_SLUG.fullmatch(source_id)
                and len(source_id) <= MAX_ID_CHARS):
            report.error("provenance", f"{label} id must be a lowercase hyphenated slug")
        elif source_id in valid:
            report.error("provenance", f"Duplicate source id: {source_id!r}")
        if not isinstance(source["type"], str) or source["type"] not in SOURCE_TYPES:
            report.error("source_type", f"{label} type must be one of: {', '.join(SOURCE_TYPES)}")
        if not _is_http_url(source["url"]):
            report.error("url", f"{label} url must be an absolute http(s) URL")
        else:
            key = _url_key(source["url"])
            if key in urls:
                report.error("provenance", f"{label} repeats the url of source {urls[key]!r}; "
                                           "one page is one source")
            elif isinstance(source_id, str):
                urls[key] = source_id
        if not _is_text(source["title"]):
            report.error("provenance", f"{label} title must be non-empty, trimmed text")
        retrieved = parse_timestamp(source["retrieved_at"])
        if retrieved is None:
            report.error("provenance",
                         f"{label} retrieved_at must be an ISO 8601 timestamp with a UTC offset")
        for key in ("publisher", "notes"):
            if key in source and not _is_text(source[key]):
                report.error("provenance", f"{label} {key} must be non-empty, trimmed text when present")
        if len(report.errors) == before:
            valid[source_id] = source
    if context is not None and context["pages"] is not None and len(sources) > context["pages"]:
        report.error("limit", f"{len(sources)} sources recorded but pages_inspected is "
                              f"{context['pages']}; every source is an inspected page")
    return valid


# --------------------------------------------------------------------------- candidates

def _validate_provenance(candidate, source, report, cid) -> None:
    transformation = candidate["transformation"]
    if not isinstance(transformation, str) or transformation not in TRANSFORMATIONS:
        report.error("transformation",
                     f"transformation must be one of: {', '.join(TRANSFORMATIONS)}", cid)
        transformation = None

    if "source_ref" in candidate:
        ref = candidate["source_ref"]
        if _shape(ref, "source_ref", (), _SOURCE_REF_KEYS, report, "provenance", cid):
            if not ref:
                report.error("provenance", "source_ref must not be empty when present", cid)
            for key, value in ref.items():
                if not (_is_text(value) or (type(value) is int and value > 0)):
                    report.error("provenance",
                                 f"source_ref {key} must be trimmed text or a positive integer", cid)

    research = candidate.get("research")
    if "research" in candidate and _shape(research, "research", ("concept",), ("skill", "notes"),
                                          report, "provenance", cid):
        for key, value in research.items():
            if not _is_text(value):
                report.error("provenance", f"research {key} must be non-empty, trimmed text", cid)
        notes = research.get("notes")
        if isinstance(notes, str) and len(notes) > MAX_RESEARCH_NOTES_CHARS:
            report.flag("research_notes_long",
                        "research notes are long; they must describe the concept, not hold "
                        "copied source wording", cid)

    exception = candidate.get("reuse_exception")
    if "reuse_exception" in candidate and _shape(
            exception, "reuse_exception", ("basis", "approved_by"), (), report, "provenance", cid):
        for key in ("basis", "approved_by"):
            if not _is_text(exception[key]):
                report.error("provenance", f"reuse_exception {key} must be non-empty, trimmed text", cid)

    if source is None or transformation is None:
        return
    policy = SOURCE_TYPES[source["type"]]
    verbatim_case = transformation == EXTRACTED and not policy.verbatim_allowed
    if "reuse_exception" in candidate and not verbatim_case:
        report.error("provenance", "reuse_exception only applies to an extracted candidate from "
                                   "a source type that does not allow verbatim reuse", cid)
    if verbatim_case:
        if isinstance(exception, dict) and _is_text(exception.get("basis")) \
                and _is_text(exception.get("approved_by")):
            report.flag("third_party_reuse_exception",
                        f"verbatim {source['type']} wording kept under a recorded exception "
                        f"approved by {exception['approved_by']}", cid)
        else:
            report.error("third_party_verbatim",
                         f"a {source['type']} source does not allow extracted (verbatim) wording; "
                         f"write an original question and use {ORIGINAL}", cid)
    elif transformation == ADAPTED and not policy.verbatim_allowed:
        report.flag("third_party_adapted",
                    f"adapted from a {source['type']} source: check the wording is not a close "
                    "paraphrase", cid)
    if transformation == ORIGINAL and policy.research_required and "research" not in candidate:
        report.error("provenance", f"a {source['type']} {ORIGINAL} candidate needs research "
                                   "metadata (at least the concept)", cid)
    if transformation == EXTRACTED and policy.verbatim_allowed and "source_ref" not in candidate:
        report.flag("extract_without_locator",
                    "extracted wording has no source_ref (document, session, question or page)", cid)


def _validate_answer(candidate, report, cid):
    """Type-specific structure; return the normalized option texts or None."""
    kind = candidate["type"]
    if not isinstance(kind, str) or kind not in QUESTION_TYPES:
        report.error("unsupported_type", f"type must be one of: {', '.join(QUESTION_TYPES)}", cid)
        return None
    allowed = {MULTIPLE_CHOICE: ("options", "correct"), TRUE_FALSE: ("correct",),
               SHORT_ANSWER: ("accepted_answers",)}[kind]
    for key in ("options", "correct", "accepted_answers"):
        if key in candidate and key not in allowed:
            report.error("answer", f"{key} does not apply to a {kind} question", cid)
        elif key not in candidate and key in allowed:
            if key == "correct":
                report.error("missing_correct", "the correct answer is missing", cid)
            else:
                report.error("answer", f"a {kind} question needs {key}", cid)

    if kind == TRUE_FALSE:
        if "correct" in candidate and type(candidate["correct"]) is not bool:
            report.error("invalid_correct", "correct must be true or false", cid)
        return None

    if kind == SHORT_ANSWER:
        answers = candidate.get("accepted_answers")
        if "accepted_answers" in candidate:
            if (not isinstance(answers, list) or not answers
                    or not all(_is_text(answer) for answer in answers)):
                report.error("answer", "accepted_answers must be a non-empty array of trimmed text", cid)
            elif len({normalize_text(answer) for answer in answers}) != len(answers):
                report.error("answer", "accepted_answers repeats an answer", cid)
        return None

    options = candidate.get("options")
    if "options" not in candidate:
        return None
    if not isinstance(options, dict) or not all(isinstance(key, str) for key in options):
        report.error("mc_options", "options must be an object of letter: text", cid)
        return None
    letters = sorted(options)
    if (len(letters) < MIN_OPTIONS or len(letters) > len(OPTION_LETTERS)
            or "".join(letters) != OPTION_LETTERS[:len(letters)]):
        report.error("mc_options",
                     f"options must use consecutive Latin letters from A, between {MIN_OPTIONS} "
                     f"and {len(OPTION_LETTERS)} of them", cid)
        return None
    if "correct" in candidate and (not isinstance(candidate["correct"], str)
                                   or candidate["correct"] not in options):
        report.error("invalid_correct", "correct must be the letter of one of the options", cid)
    texts_ok = True
    for letter in letters:
        if not _is_text(options[letter]):
            report.error("mc_options", f"option {letter} must be non-empty, trimmed text", cid)
            texts_ok = False
        elif options[letter] in PLACEHOLDER_OPTIONS:
            report.error("mc_options", f"option {letter} is a placeholder, not an answer", cid)
            texts_ok = False
    if not texts_ok:
        return None
    normalized = [normalize_text(options[letter]) for letter in letters]
    if len(set(normalized)) != len(normalized):
        report.error("mc_duplicate_options", "two answer choices are the same", cid)
    if len(letters) != EXPECTED_OPTIONS:
        report.flag("mc_option_count",
                    f"{len(letters)} answer choices; the reviewed importer accepts exactly "
                    f"{EXPECTED_OPTIONS}", cid)
    return normalized


def _validate_review(candidate, context, report, cid):
    review = candidate["review"]
    if not _shape(review, "review", _REVIEW_KEYS, (), report, "review_state", cid):
        return None
    status = review["status"]
    if not isinstance(status, str) or status not in REVIEW_STATES:
        report.error("review_state", f"review status must be one of: {', '.join(REVIEW_STATES)}", cid)
        status = None
    reviewer = review["reviewer"]
    if reviewer is not None and not _is_text(reviewer):
        report.error("review_state", "reviewer must be null or non-empty, trimmed text", cid)
    reviewed_at = None
    if review["reviewed_at"] is not None:
        reviewed_at = parse_timestamp(review["reviewed_at"])
        if reviewed_at is None:
            report.error("review_state",
                         "reviewed_at must be null or an ISO 8601 timestamp with a UTC offset", cid)
    if not isinstance(review["notes"], str):
        report.error("review_state", "review notes must be text (it may be empty)", cid)
    if status == APPROVED:
        # Collection is never approval: an approval names a person and a time
        # after the run, and the collector cannot approve its own candidates.
        if not _is_text(reviewer) or reviewed_at is None:
            report.error("review_state", "an approved candidate needs reviewer and reviewed_at", cid)
        else:
            if reviewed_at < context["started"]:
                report.error("review_state", "reviewed_at is earlier than the start of the run", cid)
            collector = context["collector"]
            if isinstance(collector, str) and collector.casefold() == reviewer.casefold():
                report.error("review_state",
                             "the collector cannot approve its own candidates", cid)
    return status


def _quality_flags(candidate, options, report, cid) -> None:
    prompt = candidate["prompt"]
    normalized = normalize_text(prompt)
    if len(normalized) < MIN_PROMPT_CHARS or len(normalized.split()) < MIN_PROMPT_WORDS:
        report.flag("prompt_too_short", "the question text is very short", cid)
    if len(prompt) > MAX_PROMPT_CHARS:
        report.flag("prompt_too_long", f"the question text is longer than {MAX_PROMPT_CHARS} characters", cid)

    texts = [prompt, candidate.get("explanation")]
    if isinstance(candidate.get("options"), dict):
        texts.extend(candidate["options"].values())
    if isinstance(candidate.get("accepted_answers"), list):
        texts.extend(candidate["accepted_answers"])
    if any(isinstance(text, str) and _HTML_RESIDUE.search(text) for text in texts):
        report.flag("html_residue", "the text contains HTML tags, entities or script residue", cid)

    padded = f" {normalized} "

    def in_prompt(text):
        return len(text) >= MIN_LEAK_CHARS and f" {text} " in padded

    if options is not None and isinstance(candidate.get("correct"), str) \
            and candidate["correct"] in candidate["options"]:
        correct = options[sorted(candidate["options"]).index(candidate["correct"])]
        if in_prompt(correct) and sum(in_prompt(option) for option in options) == 1:
            report.flag("answer_leak", "the correct answer, and no other choice, appears in the "
                                       "question text", cid)
    elif candidate.get("type") == SHORT_ANSWER and isinstance(candidate.get("accepted_answers"), list):
        if any(isinstance(answer, str) and in_prompt(normalize_text(answer))
               for answer in candidate["accepted_answers"]):
            report.flag("answer_leak", "an accepted answer appears in the question text", cid)


def _validate_candidates(candidates, context, sources, report) -> list:
    """Validate every candidate; return comparison records for the well-formed ones."""
    records: list = []
    if not isinstance(candidates, list):
        report.error("candidate", "candidates must be an array")
        return records
    limits = context["limits"]
    if len(candidates) > limits.max_candidates:
        report.error("limit", f"{len(candidates)} candidates exceed max_candidates "
                              f"{limits.max_candidates}")
    seen: set = set()
    per_source: dict = {}
    prefix = f"{context['run_id']}-"
    for position, candidate in enumerate(candidates, 1):
        label = f"candidate {position}"
        cid = candidate.get("id") if isinstance(candidate, dict) else None
        cid = cid if isinstance(cid, str) else None
        before = len(report.errors)
        if not _shape(candidate, label, _CANDIDATE_KEYS, _CANDIDATE_OPTIONAL, report,
                      "candidate", cid):
            continue
        if cid is None or not STORED_SLUG.fullmatch(cid) or len(cid) > MAX_ID_CHARS \
                or not cid.startswith(prefix) or len(cid) == len(prefix):
            report.error("candidate_id", f"{label} id must be a lowercase hyphenated slug "
                                         f"starting with {prefix}", cid)
        elif cid in seen:
            report.error("duplicate_candidate_id", f"Duplicate candidate id: {cid!r}", cid)
        if cid is not None:
            seen.add(cid)

        topic_slug, section_slug = candidate["topic_slug"], candidate["section_slug"]
        if not isinstance(topic_slug, str) or topic_slug not in context["topics"]:
            report.error("topic", f"topic_slug {topic_slug!r} is not a target topic of this run", cid)
        elif context["topics"][topic_slug] != section_slug:
            report.error("topic", f"topic {topic_slug!r} belongs to target section "
                                  f"{context['topics'][topic_slug]!r}, not {section_slug!r}", cid)

        source = sources.get(candidate["source_id"]) if isinstance(candidate["source_id"], str) else None
        if source is None:
            report.error("provenance", f"source_id {candidate['source_id']!r} is not a valid "
                                       "source record of this dataset", cid)
        else:
            per_source[source["id"]] = per_source.get(source["id"], 0) + 1
        _validate_provenance(candidate, source, report, cid)

        prompt_ok = _is_text(candidate["prompt"])
        if not prompt_ok:
            report.error("prompt", "prompt must be non-empty, trimmed text", cid)
        options = _validate_answer(candidate, report, cid)
        if "explanation" in candidate and not _is_text(candidate["explanation"]):
            report.error("candidate", "explanation must be non-empty, trimmed text when present", cid)
        if "difficulty" in candidate and candidate["difficulty"] not in DIFFICULTIES:
            report.error("candidate", f"difficulty must be one of: {', '.join(DIFFICULTIES)}", cid)
        if "flags" in candidate and not (
                isinstance(candidate["flags"], list) and all(
                    isinstance(item, dict) and set(item) == {"code", "message"}
                    and all(isinstance(value, str) for value in item.values())
                    for item in candidate["flags"])):
            report.error("candidate", "flags must be an array of {code, message}", cid)
        status = _validate_review(candidate, context, report, cid)

        if prompt_ok:
            _quality_flags(candidate, options, report, cid)
        if len(report.errors) == before:
            records.append({
                "id": cid, "status": status, "prompt": normalize_text(candidate["prompt"]),
                "options": frozenset(options) if options is not None else None,
            })
    for source_id, count in sorted(per_source.items()):
        if count > limits.max_candidates_per_source:
            report.error("limit", f"source {source_id!r} has {count} candidates, more than "
                                  f"max_candidates_per_source {limits.max_candidates_per_source}")
    return records


# --------------------------------------------------------------------------- duplicates

def similarity(left: dict, right: dict) -> str | None:
    """``"exact"``, ``"near"`` or None for two comparison records.

    Exact: equal normalized prompts and equal sets of normalized answer choices
    (two questions without choices compare by prompt alone). Near: prompt
    similarity of at least ``NEAR_DUPLICATE_RATIO`` and, when both have choices,
    at least ``NEAR_DUPLICATE_OPTION_OVERLAP`` of them in common.
    """
    a, b = left["prompt"], right["prompt"]
    if a == b and left["options"] == right["options"]:
        return "exact"
    if not a or not b:
        return None
    # 2*min/(a+b) bounds the ratio from above; skip hopeless pairs cheaply.
    if 2 * min(len(a), len(b)) / (len(a) + len(b)) < NEAR_DUPLICATE_RATIO:
        return None
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.quick_ratio() < NEAR_DUPLICATE_RATIO or matcher.ratio() < NEAR_DUPLICATE_RATIO:
        return None
    if left["options"] and right["options"]:
        union = left["options"] | right["options"]
        if len(left["options"] & right["options"]) / len(union) < NEAR_DUPLICATE_OPTION_OVERLAP:
            return None
    return "near"


def _flag_duplicates(records, report) -> None:
    """Flag duplicates among candidates that are still in play (not rejected)."""
    active = [record for record in records if record["status"] != REJECTED]
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            kind = similarity(left, right)
            if kind is None:
                continue
            for one, other in ((left, right), (right, left)):
                report.flag(f"duplicate_{kind}",
                            f"{kind} duplicate of candidate {other['id']}", one["id"])


# --------------------------------------------------------------------------- read-only database checks

def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing database strictly read-only (no sidecar files are created).

    ``immutable=1`` keeps SQLite from touching the ``-shm``/``-wal`` sidecars, so
    changes still waiting in a write-ahead log are not visible; see ``has_pending_wal``.
    """
    path = Path(db_path).resolve()
    if not path.is_file():
        raise sqlite3.OperationalError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def has_pending_wal(db_path: Path) -> bool:
    """True when a write-ahead log holds changes ``open_readonly`` cannot see."""
    wal = Path(str(Path(db_path).resolve()) + "-wal")
    return wal.is_file() and wal.stat().st_size > 0


def reviewed_source_prefix(subject: str, run_id: str) -> str:
    """Prefix of every ``questions.source_exam`` value exported from one run."""
    return f"reviewed:{subject}:{run_id}."


def _check_targets_in_db(conn, run, context, report) -> None:
    subject, grade = context["subject"], context["grade"]
    wanted = run["curriculum"]
    matches = [row for row in conn.execute(
        "SELECT id, subject, title_bg FROM curricula WHERE grade = ? AND education_type = ?",
        (grade, wanted["education_type"]),
    ) if canonical_subject(row["subject"]) == subject]
    curriculum_id = None
    if len(matches) > 1:
        report.error("db_conflict", "the curriculum identity is ambiguous in the database")
    elif not matches:
        report.flag("target_not_in_db",
                    "the curriculum is not in the database; import the structure first")
    else:
        curriculum_id = matches[0]["id"]
        if matches[0]["title_bg"] != wanted["title"]:
            report.error("db_conflict", "curriculum title differs from the stored title "
                                        f"{matches[0]['title_bg']!r}")

    for target in run["targets"]:
        section = target["section"]
        row = conn.execute(
            "SELECT id, curriculum_id, title_bg, class, section_type FROM curriculum_sections "
            "WHERE section_slug = ?", (section["slug"],)).fetchone()
        section_id = None
        if row is None:
            report.flag("target_not_in_db", f"section {section['slug']!r} is not in the "
                                            "database; import the structure first")
        else:
            section_id = row["id"]
            owner = section_ownership(conn, section_id)
            if not owner.is_consistent or owner.subject != subject:
                report.error("db_conflict", f"section {section['slug']!r} is not owned by "
                                            f"{subject}: {owner.status}/{owner.detail}")
            elif (row["class"] != grade or row["title_bg"] != section["title"]
                  or row["section_type"] != "content"
                  or (curriculum_id is not None and row["curriculum_id"] != curriculum_id)):
                report.error("db_conflict", f"section {section['slug']!r} differs from the stored "
                                            "section (curriculum, grade, title or type)")
        for topic in target["topics"]:
            stored = conn.execute(
                "SELECT id, section_id, title_bg FROM curriculum_topics WHERE topic_slug = ?",
                (topic["slug"],)).fetchone()
            if stored is None:
                report.flag("target_not_in_db", f"topic {topic['slug']!r} is not in the "
                                                "database; import the structure first")
                continue
            owner = topic_ownership(conn, stored["id"])
            if not owner.is_consistent or owner.subject != subject:
                report.error("db_conflict", f"topic {topic['slug']!r} is not owned by "
                                            f"{subject}: {owner.status}/{owner.detail}")
            elif stored["section_id"] != section_id or stored["title_bg"] != topic["title"]:
                report.error("db_conflict", f"topic {topic['slug']!r} differs from the stored "
                                            "topic (section or title)")


def _flag_existing_duplicates(conn, records, context, report) -> None:
    """Compare candidates with the stored questions of the same subject."""
    own = reviewed_source_prefix(context["subject"], context["run_id"])
    options: dict = {}
    for row in conn.execute("SELECT question_id, option_text FROM multiple_choice_options"):
        options.setdefault(row["question_id"], set()).add(normalize_text(row["option_text"] or ""))
    existing = []
    for row in conn.execute("SELECT id, subject, source_exam, prompt FROM questions"):
        if canonical_subject(row["subject"]) != context["subject"]:
            continue
        if (row["source_exam"] or "").startswith(own):
            continue        # this run's own, already imported export
        existing.append({"id": row["id"], "prompt": normalize_text(row["prompt"] or ""),
                         "options": frozenset(options[row["id"]]) if row["id"] in options else None})
    for record in records:
        if record["status"] == REJECTED:
            continue
        for stored in existing:
            kind = similarity(record, stored)
            if kind is not None:
                report.flag(f"duplicate_existing_{kind}",
                            f"{kind} duplicate of stored question {stored['id']}", record["id"])


# --------------------------------------------------------------------------- public API

def validate_dataset(dataset, *, db_path=None, check_duplicates: bool = True) -> ValidationReport:
    """Validate a candidate dataset. Never writes; ``db_path`` is opened read-only."""
    report = ValidationReport()
    if not _shape(dataset, "dataset", ("format_version", "kind", "run", "sources", "candidates"),
                  (), report, "format"):
        if isinstance(dataset, dict) and "format_version" in dataset and (
                type(dataset["format_version"]) is not int
                or dataset["format_version"] != FORMAT_VERSION):
            report.error("format_version", "Unsupported format_version")
        return report
    if type(dataset["format_version"]) is not int or dataset["format_version"] != FORMAT_VERSION:
        report.error("format_version", "Unsupported format_version")
        return report
    if dataset["kind"] != KIND:
        report.error("format", f"kind must be {KIND!r}")
        return report

    context = _validate_run(dataset["run"], report)
    sources = _validate_sources(dataset["sources"], context, report)
    if context is None:
        return report
    records = _validate_candidates(dataset["candidates"], context, sources, report)
    if check_duplicates:
        _flag_duplicates(records, report)

    if db_path is not None:
        try:
            conn = open_readonly(db_path)
            if has_pending_wal(db_path):
                report.flag("db_wal_pending",
                            "the database has changes waiting in its write-ahead log that a "
                            "strictly read-only check cannot see; check a src.backup_sqlite "
                            "copy for a current result")
            try:
                _check_targets_in_db(conn, dataset["run"], context, report)
                if check_duplicates:
                    _flag_existing_duplicates(conn, records, context, report)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            report.error("db", f"database check failed: {exc}")

    statuses = {record["id"]: record["status"] for record in records}
    for item in report.flags:
        if item.code in BLOCKING_FLAGS and statuses.get(item.candidate_id) == APPROVED:
            report.error("approved_blocked",
                         f"approved despite an unresolved flag: {item.message}", item.candidate_id)

    counts = {state: 0 for state in REVIEW_STATES}
    for candidate in dataset["candidates"] if isinstance(dataset["candidates"], list) else ():
        status = candidate.get("review", {}).get("status") if isinstance(candidate, dict) and \
            isinstance(candidate.get("review"), dict) else None
        if isinstance(status, str) and status in counts:
            counts[status] += 1
    report.counts = {
        "candidates": len(dataset["candidates"]) if isinstance(dataset["candidates"], list) else 0,
        "sources": len(sources), "by_status": counts,
        "errors": len(report.errors), "flags": len(report.flags),
    }
    return report


def annotated_copy(dataset: dict, report: ValidationReport) -> dict:
    """A copy whose candidates carry the computed ``flags``; the input is untouched."""
    result = copy.deepcopy(dataset)
    for candidate in result["candidates"]:
        if isinstance(candidate, dict):
            candidate["flags"] = [
                {"code": item.code, "message": item.message}
                for item in report.flags_for(candidate.get("id"))
            ] if isinstance(candidate.get("id"), str) else []
    return result


def new_dataset(*, run_id: str, started_at: str, subject: str, grade: int, curriculum: dict,
                targets: list, limits: CollectionLimits, collector: str | None = None) -> dict:
    """An empty, validated dataset for a run that has not collected anything yet."""
    run = {
        "id": run_id, "started_at": started_at, "finished_at": None,
        "subject": subject, "grade": grade, "curriculum": copy.deepcopy(curriculum),
        "targets": copy.deepcopy(targets), "limits": limits.as_dict(),
        "pages_inspected": 0, "stop_reason": None,
    }
    if collector is not None:
        run["collector"] = collector
    dataset = {"format_version": FORMAT_VERSION, "kind": KIND, "run": run,
               "sources": [], "candidates": []}
    report = validate_dataset(dataset)
    if not report.ok:
        raise CandidateDatasetError(report)
    return dataset


def _append_validated(dataset: dict, key: str, item: dict) -> None:
    trial = copy.deepcopy(dataset)
    trial[key].append(copy.deepcopy(item))
    # Duplicates are a review matter; submission only has to be well formed.
    report = validate_dataset(trial, check_duplicates=False)
    if not report.ok:
        raise CandidateDatasetError(report)
    dataset[key].append(copy.deepcopy(item))


def add_source(dataset: dict, source: dict) -> None:
    """Record an inspected page as a source. Raises without changing the dataset.

    Count the page in ``run.pages_inspected`` first: sources may not outnumber pages.
    """
    _append_validated(dataset, "sources", source)


def add_candidate(dataset: dict, candidate: dict) -> None:
    """Submit one candidate from a collector. Raises without changing the dataset.

    A submitted candidate is always ``pending``: collecting is not approving.
    Hard limits (candidates, candidates per source) are enforced here.
    """
    review = candidate.get("review") if isinstance(candidate, dict) else None
    if not (isinstance(review, dict) and review.get("status") == PENDING
            and review.get("reviewer") is None and review.get("reviewed_at") is None):
        report = ValidationReport()
        report.error("review_state", "a collected candidate must be pending and unreviewed",
                     candidate.get("id") if isinstance(candidate, dict) else None)
        raise CandidateDatasetError(report)
    _append_validated(dataset, "candidates", candidate)
