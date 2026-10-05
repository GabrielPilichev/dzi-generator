"""Import a reviewed curriculum structure: one curriculum, its ordered sections and topics.

No questions are created or changed. Dry run is the default: the file is
validated, planned against the target database and rehearsed on an in-memory
copy. ``--apply`` runs the same path on the target in one transaction; any
conflict or error leaves the database unchanged.

Rules, in short (see docs/SUBJECT_STRUCTURE_IMPORT.md):
- array order is the order: it is stored in ``curriculum_sections.display_order``
  and ``topic_section_assignments.display_order``, starting at 1;
- existing titles are never overwritten: a different stored title is a conflict;
- the file is complete for its curriculum: a stored section, or a stored topic
  of a listed section, that the file does not list is a conflict;
- nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from src.subject_browsing import (
    has_topic_order, is_namespaced_slug, namespaced_slug_prefix, public_description,
)
from src.subject_content import canonical_subject, section_ownership, topic_ownership
from src.subject_registry import get_subject, subject_supports_grade


FORMAT_VERSION = 1
SOURCE_TYPE = "subject_structure_import"
SECTION_TYPE = "content"          # the type the reviewed question importer expects
ORDER_KEYS = frozenset({"display_order", "order", "position", "number", "index"})


class StructureImportError(ValueError):
    """The structure was rejected; ``problems`` lists every reason found."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


# --------------------------------------------------------------------------- file validation

def _keys(value, label, required, optional=()):
    if not isinstance(value, dict):
        raise StructureImportError(f"{label} must be an object")
    stray_order = sorted(ORDER_KEYS & set(value))
    if stray_order:
        raise StructureImportError(
            f"{label} must not set {', '.join(stray_order)}: order is the position in the array"
        )
    missing = [key for key in required if key not in value]
    unknown = sorted(set(value) - set(required) - set(optional))
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unexpected {', '.join(unknown)}")
        raise StructureImportError(f"{label}: {'; '.join(details)}")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise StructureImportError(f"{label} must be non-empty, trimmed text")
    return value


def _slug(value, label, subject, grade, seen):
    if not is_namespaced_slug(subject, grade, value):
        raise StructureImportError(
            f"{label} slug {value!r} must be lowercase, hyphenated and namespaced "
            f"{namespaced_slug_prefix(subject, grade)}..."
        )
    if value in seen:
        raise StructureImportError(f"Duplicate {label} slug: {value!r}")
    seen.add(value)
    return value


def validate_structure(structure) -> str:
    """Validate the whole file before any database work; return the canonical subject."""
    _keys(structure, "structure", ("format_version", "subject", "grade", "curriculum", "sections"))
    version = structure["format_version"]
    if type(version) is not int or version != FORMAT_VERSION:
        raise StructureImportError("Unsupported format_version")
    subject = canonical_subject(structure["subject"])
    if subject is None:
        raise StructureImportError(f"Unknown content subject: {structure['subject']!r}")
    grade = structure["grade"]
    if not subject_supports_grade(subject, grade):
        supported = ", ".join(str(value) for value in get_subject(subject).grades)
        raise StructureImportError(
            f"grade must be an integer in the grades supported by {subject}: {supported}"
        )

    curriculum = structure["curriculum"]
    _keys(curriculum, "curriculum", ("education_type", "title"), ("source_url",))
    _text(curriculum["education_type"], "curriculum education_type")
    _text(curriculum["title"], "curriculum title")
    if "source_url" in curriculum:
        _text(curriculum["source_url"], "curriculum source_url")

    sections = structure["sections"]
    if not isinstance(sections, list) or not sections:
        raise StructureImportError("sections must be a non-empty array")
    section_slugs: set[str] = set()
    topic_slugs: set[str] = set()
    for position, section in enumerate(sections, 1):
        label = f"section {position}"
        _keys(section, label, ("slug", "title", "topics"))
        _slug(section["slug"], "section", subject, grade, section_slugs)
        _text(section["title"], f"{label} title")
        topics = section["topics"]
        if not isinstance(topics, list):
            raise StructureImportError(f"{label} topics must be an array (it may be empty)")
        for topic_position, topic in enumerate(topics, 1):
            topic_label = f"{label} topic {topic_position}"
            _keys(topic, topic_label, ("slug", "title"), ("description",))
            _slug(topic["slug"], "topic", subject, grade, topic_slugs)
            _text(topic["title"], f"{topic_label} title")
            if "description" in topic:
                description = _text(topic["description"], f"{topic_label} description")
                if public_description(description) != description:
                    raise StructureImportError(f"{topic_label} description must be plain prose")
    return subject


# --------------------------------------------------------------------------- planning (reads only)

def _owner_label(owner, grade) -> str:
    subject = owner.subject if owner.is_consistent else f"{owner.status}/{owner.detail}"
    return f"{subject}, grade {grade}"


def _item(slug, title, order, exists):
    return {"slug": slug, "title": title, "order": order,
            "action": "unchanged" if exists else "create", "changes": []}


def _plan(conn: sqlite3.Connection, structure: dict, subject: str):
    """Compare the structure with the database. Returns (report, ids, conflicts)."""
    grade = structure["grade"]
    prefix = namespaced_slug_prefix(subject, grade)
    conflicts: list[str] = []
    ids = {"curriculum": None, "sections": {}, "topics": {}}

    wanted = structure["curriculum"]
    curriculum = {"education_type": wanted["education_type"], "title": wanted["title"],
                  "action": "create", "changes": []}
    matches = [row for row in conn.execute(
        "SELECT id, subject, title_bg, source_url FROM curricula "
        "WHERE grade = ? AND education_type = ?", (grade, wanted["education_type"]),
    ) if canonical_subject(row["subject"]) == subject]
    if len(matches) > 1:
        conflicts.append(
            f"curriculum {subject} grade {grade} {wanted['education_type']!r} is ambiguous: "
            f"{len(matches)} stored curricula match"
        )
    elif matches:
        stored = matches[0]
        ids["curriculum"] = int(stored["id"])
        curriculum["action"] = "unchanged"
        if stored["title_bg"] != wanted["title"]:
            conflicts.append(
                f"curriculum title differs from the stored title {stored['title_bg']!r}"
            )
        stored_url = (stored["source_url"] or "").strip()
        if "source_url" in wanted and stored_url != wanted["source_url"]:
            if stored_url:
                conflicts.append(f"curriculum source_url differs from the stored {stored_url!r}")
            else:
                curriculum["changes"].append("source_url added")
    curriculum_id = ids["curriculum"]

    listed_sections = {section["slug"] for section in structure["sections"]}
    if curriculum_id is not None:
        for row in conn.execute(
            "SELECT section_slug FROM curriculum_sections WHERE curriculum_id = ? "
            "ORDER BY section_slug", (curriculum_id,),
        ):
            if row["section_slug"] not in listed_sections:
                conflicts.append(
                    f"section {row['section_slug']!r} is stored in this curriculum but missing "
                    "from the structure; list every section so the order is complete"
                )

    sections = []
    for order, wanted_section in enumerate(structure["sections"], 1):
        slug = wanted_section["slug"]
        stored = conn.execute(
            "SELECT id, curriculum_id, title_bg, class, section_type, display_order "
            "FROM curriculum_sections WHERE section_slug = ?", (slug,),
        ).fetchone()
        section = _item(slug, wanted_section["title"], order, stored is not None)
        section["topics"] = []
        sections.append(section)
        section_id = None
        if stored is None:
            shadow = conn.execute(
                "SELECT id, class FROM curriculum_sections WHERE section_slug = ?",
                (slug[len(prefix):],),
            ).fetchone()
            if shadow is not None and shadow["class"] == grade:
                owner = section_ownership(conn, int(shadow["id"]))
                if owner.is_consistent and owner.subject == subject:
                    conflicts.append(
                        f"section {slug!r} would shadow the stored section "
                        f"{slug[len(prefix):]!r} of the same subject and grade in URLs"
                    )
        elif curriculum_id is None or stored["curriculum_id"] != curriculum_id:
            owner = section_ownership(conn, int(stored["id"]))
            conflicts.append(
                f"section slug {slug!r} already belongs to another curriculum "
                f"({_owner_label(owner, stored['class'])})"
            )
        elif stored["class"] != grade or stored["section_type"] != SECTION_TYPE:
            conflicts.append(
                f"section {slug!r} is stored with grade {stored['class']} and type "
                f"{stored['section_type']!r}, not grade {grade} and type {SECTION_TYPE!r}"
            )
        elif stored["title_bg"] != wanted_section["title"]:
            conflicts.append(
                f"section {slug!r} title differs from the stored title {stored['title_bg']!r}"
            )
        else:
            section_id = int(stored["id"])
            ids["sections"][slug] = section_id
            if stored["display_order"] != order:
                section["changes"].append(f"order {stored['display_order']} -> {order}")

        if section_id is not None:
            listed_topics = {topic["slug"] for topic in wanted_section["topics"]}
            for row in conn.execute(
                "SELECT topic_slug FROM curriculum_topics WHERE section_id = ? "
                "ORDER BY topic_slug", (section_id,),
            ):
                if row["topic_slug"] not in listed_topics:
                    conflicts.append(
                        f"topic {row['topic_slug']!r} is stored in section {slug!r} but missing "
                        "from the structure; list every topic so the order is complete"
                    )

        for topic_order, wanted_topic in enumerate(wanted_section["topics"], 1):
            section["topics"].append(_plan_topic(
                conn, subject, grade, prefix, slug, section_id, stored is not None,
                wanted_topic, topic_order, ids, conflicts,
            ))
        if section["changes"]:
            section["action"] = "update"

    if curriculum["changes"]:
        curriculum["action"] = "update"
    report = {"subject": subject, "grade": grade, "curriculum": curriculum, "sections": sections}
    return report, ids, conflicts


def _plan_topic(conn, subject, grade, prefix, section_slug, section_id, section_stored,
                wanted, order, ids, conflicts):
    slug = wanted["slug"]
    stored = conn.execute(
        "SELECT id, section_id, title_bg, description FROM curriculum_topics WHERE topic_slug = ?",
        (slug,),
    ).fetchone()
    topic = _item(slug, wanted["title"], order, stored is not None)
    if stored is None:
        shadow = conn.execute(
            "SELECT t.id, cs.class FROM curriculum_topics t "
            "LEFT JOIN curriculum_sections cs ON cs.id = t.section_id WHERE t.topic_slug = ?",
            (slug[len(prefix):],),
        ).fetchone()
        if shadow is not None and shadow["class"] == grade:
            owner = topic_ownership(conn, int(shadow["id"]))
            if owner.is_consistent and owner.subject == subject:
                conflicts.append(
                    f"topic {slug!r} would shadow the stored topic {slug[len(prefix):]!r} "
                    "of the same subject and grade in URLs"
                )
        return topic

    topic_id = int(stored["id"])
    if section_id is None or stored["section_id"] != section_id:
        # Either the section is new or itself in conflict, or the topic lives elsewhere.
        if not section_stored or section_id is not None:
            owner = topic_ownership(conn, topic_id)
            home = conn.execute(
                "SELECT section_slug, class FROM curriculum_sections WHERE id = ?",
                (stored["section_id"],),
            ).fetchone()
            where = (f"section {home['section_slug']!r} ({_owner_label(owner, home['class'])})"
                     if home else f"no section ({owner.status}/{owner.detail})")
            conflicts.append(
                f"topic slug {slug!r} already belongs to {where}, not to section {section_slug!r}"
            )
        return topic

    if stored["title_bg"] != wanted["title"]:
        conflicts.append(f"topic {slug!r} title differs from the stored title {stored['title_bg']!r}")
    membership = conn.execute(
        "SELECT relationship_type, is_primary, display_order FROM topic_section_assignments "
        "WHERE topic_id = ? AND section_id = ?", (topic_id, section_id),
    ).fetchone()
    classes = {row[0] for row in conn.execute(
        "SELECT class FROM topic_classes WHERE topic_id = ?", (topic_id,))}
    if membership is None or (membership["relationship_type"], membership["is_primary"]) != ("covers", 1):
        conflicts.append(
            f"topic {slug!r} is not stored as the primary 'covers' member of section {section_slug!r}"
        )
    elif classes != {grade}:
        conflicts.append(
            f"topic {slug!r} is stored for grades {sorted(classes)}, not only grade {grade}"
        )
    else:
        owner = topic_ownership(conn, topic_id)
        if not owner.is_consistent or owner.subject != subject:
            conflicts.append(
                f"topic {slug!r} has conflicting ownership: {owner.status}/{owner.detail}"
            )
        elif membership["display_order"] != order:
            topic["changes"].append(f"order {membership['display_order']} -> {order}")

    stored_description = (stored["description"] or "").strip()
    if "description" in wanted and stored_description != wanted["description"]:
        if stored_description:
            conflicts.append(f"topic {slug!r} description differs from the stored description")
        else:
            topic["changes"].append("description added")
    ids["topics"][slug] = topic_id
    if topic["changes"]:
        topic["action"] = "update"
    return topic


# --------------------------------------------------------------------------- applying

def _apply(conn: sqlite3.Connection, structure: dict, subject: str, report: dict, ids: dict) -> None:
    """Write exactly what the report announces. Caller owns the transaction."""
    grade = structure["grade"]
    wanted = structure["curriculum"]
    curriculum_id = ids["curriculum"]
    if report["curriculum"]["action"] == "create":
        curriculum_id = conn.execute(
            "INSERT INTO curricula (grade, subject, education_type, title_bg, source_type, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (grade, subject, wanted["education_type"], wanted["title"], SOURCE_TYPE,
             wanted.get("source_url")),
        ).lastrowid
    elif report["curriculum"]["action"] == "update":
        conn.execute("UPDATE curricula SET source_url = ? WHERE id = ?",
                     (wanted["source_url"], curriculum_id))

    for section, wanted_section in zip(report["sections"], structure["sections"]):
        section_id = ids["sections"].get(section["slug"])
        if section["action"] == "create":
            section_id = conn.execute(
                "INSERT INTO curriculum_sections "
                "(curriculum_id, section_slug, title_bg, class, section_type, display_order, source_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (curriculum_id, section["slug"], section["title"], grade, SECTION_TYPE,
                 section["order"], SOURCE_TYPE),
            ).lastrowid
        elif section["action"] == "update":
            conn.execute("UPDATE curriculum_sections SET display_order = ? WHERE id = ?",
                         (section["order"], section_id))
        _owned(section_ownership(conn, section_id), subject, f"section {section['slug']!r}")

        for topic, wanted_topic in zip(section["topics"], wanted_section["topics"]):
            topic_id = ids["topics"].get(topic["slug"])
            if topic["action"] == "create":
                topic_id = conn.execute(
                    "INSERT INTO curriculum_topics (topic_slug, title_bg, section_id, description) "
                    "VALUES (?, ?, ?, ?)",
                    (topic["slug"], topic["title"], section_id, wanted_topic.get("description")),
                ).lastrowid
                conn.execute(
                    "INSERT INTO topic_section_assignments "
                    "(topic_id, section_id, relationship_type, is_primary, source_type, display_order) "
                    "VALUES (?, ?, 'covers', 1, ?, ?)",
                    (topic_id, section_id, SOURCE_TYPE, topic["order"]),
                )
                conn.execute("INSERT INTO topic_classes (topic_id, class) VALUES (?, ?)",
                             (topic_id, grade))
            elif topic["action"] == "update":
                conn.execute(
                    "UPDATE topic_section_assignments SET display_order = ? "
                    "WHERE topic_id = ? AND section_id = ?",
                    (topic["order"], topic_id, section_id),
                )
                if "description added" in topic["changes"]:
                    conn.execute("UPDATE curriculum_topics SET description = ? WHERE id = ?",
                                 (wanted_topic["description"], topic_id))
            _owned(topic_ownership(conn, topic_id), subject, f"topic {topic['slug']!r}")


def _owned(owner, subject, label):
    if not owner.is_consistent or owner.subject != subject:
        raise StructureImportError(
            f"{label} has conflicting ownership after import: {owner.status}/{owner.detail}"
        )


def _summarize(report: dict, *, apply: bool) -> dict:
    topics = [topic for section in report["sections"] for topic in section["topics"]]
    counts = {}
    for name, items in (("curricula", [report["curriculum"]]),
                        ("sections", report["sections"]), ("topics", topics)):
        counts[name] = {action: sum(1 for item in items if item["action"] == action)
                        for action in ("create", "update", "unchanged")}
    report["counts"] = counts
    report["has_changes"] = any(counts[name][action] for name in counts
                                for action in ("create", "update"))
    report["mode"] = "apply" if apply else "dry-run"
    return report


def import_structure(db_path: Path, structure: dict, *, apply: bool = False) -> dict:
    """Plan on an in-memory copy (default), or apply atomically to the target database.

    Returns the report. Raises StructureImportError for an invalid file or any
    conflict, in both modes, without changing the target.
    """
    subject = validate_structure(structure)
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise StructureImportError(f"Database not found: {db_path}")
    # mode=rw never creates a missing database. A dry run reads through a
    # query-only connection (mode=ro cannot open a WAL database whose sidecar
    # files are absent) and works on an in-memory copy from then on.
    target = sqlite3.connect(f"file:{quote(str(db_path))}?mode=rw", uri=True)
    if apply:
        conn = target
    else:
        try:
            target.execute("PRAGMA query_only=ON")
            conn = sqlite3.connect(":memory:")
            target.backup(conn)
        finally:
            target.close()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not has_topic_order(conn):
            raise StructureImportError(
                "The database has no topic_section_assignments.display_order column; "
                "apply the pending migrations (src.manage_migrations) first"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            report, ids, conflicts = _plan(conn, structure, subject)
            if conflicts:
                raise StructureImportError(conflicts)
            _summarize(report, apply=apply)
            # A dry run rehearses the same writes on its in-memory copy.
            _apply(conn, structure, subject, report, ids)
            if apply:
                conn.commit()
            else:
                conn.rollback()
            return report
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- command line

def format_report(report: dict) -> str:
    def suffix(item):
        details = f" ({'; '.join(item['changes'])})" if item["changes"] else ""
        return f"[{item['action']}]{details}"

    applying = report["mode"] == "apply"
    title = get_subject(report["subject"]).title
    lines = [
        "Mode: APPLY" if applying else "Mode: DRY RUN (nothing is written; add --apply to write)",
        f"Subject: {report['subject']} ({title})",
        f"Grade: {report['grade']}",
        f"Curriculum: {report['curriculum']['education_type']} — {report['curriculum']['title']} "
        f"{suffix(report['curriculum'])}",
        f"Sections ({len(report['sections'])}):",
    ]
    for section in report["sections"]:
        lines.append(f"  {section['order']}. {section['slug']} — {section['title']} {suffix(section)}")
        for topic in section["topics"]:
            lines.append(f"       {topic['order']}. {topic['slug']} — {topic['title']} {suffix(topic)}")
    for name in ("curricula", "sections", "topics"):
        counts = report["counts"][name]
        lines.append(f"{name.capitalize()}: {counts['create']} to create, "
                     f"{counts['update']} to update, {counts['unchanged']} unchanged")
    lines.append("Questions: none are created or changed by this importer")
    if not report["has_changes"]:
        lines.append("Result: no changes; the database already matches this structure")
    elif applying:
        lines.append("Result: changes written")
    else:
        lines.append("Result: changes would be written with --apply")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, required=True, help="reviewed structure JSON file")
    parser.add_argument("--db", type=Path, required=True, help="existing target database")
    parser.add_argument("--apply", action="store_true", help="write atomically; default is dry run")
    args = parser.parse_args(argv)
    try:
        structure = json.loads(args.input.read_text(encoding="utf-8"))
        report = import_structure(args.db, structure, apply=args.apply)
    except StructureImportError as exc:
        details = "".join(f"  - {problem}\n" for problem in exc.problems)
        parser.exit(1, f"Import rejected; the database was not changed:\n{details}")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Import rejected; the database was not changed:\n  - {exc}\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
