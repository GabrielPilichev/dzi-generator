from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB = Path("data/questions.db")

BLUEPRINT = {
    "blueprint_slug": "dzi_it_pp_2025_format",
    "title_bg": "ДЗИ по информационни технологии — профилирана подготовка, формат 2025",
    "total_points": 100,
    "part1_minutes": 90,
    "part2_minutes": 150,
    "is_active": 1,
    "notes": "Формат: Част 1 — 15 тестови задачи по 1 т. и 10 задачи със свободен отговор по 3 т.; Част 2 — практически задачи 26–28.",
}


def build_slots() -> list[dict]:
    slots: list[dict] = []

    for slot_number in range(1, 16):
        slots.append({
            "slot_number": slot_number,
            "exam_part": 1,
            "task_kind": "multiple_choice",
            "points": 1,
            "topic_area": None,
            "required_asset_type": None,
        })

    for slot_number in range(16, 26):
        slots.append({
            "slot_number": slot_number,
            "exam_part": 1,
            "task_kind": "short_answer",
            "points": 3,
            "topic_area": None,
            "required_asset_type": None,
        })

    slots.extend([
        {
            "slot_number": 26,
            "exam_part": 2,
            "task_kind": "practical_spreadsheet",
            "points": 15,
            "topic_area": "spreadsheets",
            "required_asset_type": "spreadsheet",
        },
        {
            "slot_number": 27,
            "exam_part": 2,
            "task_kind": "practical_graphics",
            "points": 20,
            "topic_area": "graphics",
            "required_asset_type": "image",
        },
        {
            "slot_number": 28,
            "exam_part": 2,
            "task_kind": "practical_web",
            "points": 20,
            "topic_area": "web",
            "required_asset_type": "archive",
        },
    ])

    return slots


def upsert_blueprint(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO dzi_blueprints (
            blueprint_slug, title_bg, total_points, part1_minutes,
            part2_minutes, is_active, notes
        )
        VALUES (
            :blueprint_slug, :title_bg, :total_points, :part1_minutes,
            :part2_minutes, :is_active, :notes
        )
        ON CONFLICT(blueprint_slug) DO UPDATE SET
            title_bg = excluded.title_bg,
            total_points = excluded.total_points,
            part1_minutes = excluded.part1_minutes,
            part2_minutes = excluded.part2_minutes,
            is_active = excluded.is_active,
            notes = excluded.notes
        """,
        BLUEPRINT,
    )

    row = conn.execute(
        "SELECT id FROM dzi_blueprints WHERE blueprint_slug = ?",
        (BLUEPRINT["blueprint_slug"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("Blueprint upsert did not return an id.")
    return int(row[0])


def slot_exists(conn: sqlite3.Connection, blueprint_id: int, slot_number: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM dzi_blueprint_slots
        WHERE blueprint_id = ? AND slot_number = ?
        """,
        (blueprint_id, slot_number),
    ).fetchone()
    return row is not None


def upsert_slots(conn: sqlite3.Connection, blueprint_id: int) -> tuple[int, int]:
    inserted = 0
    updated = 0

    for slot in build_slots():
        existed = slot_exists(conn, blueprint_id, int(slot["slot_number"]))
        params = {"blueprint_id": blueprint_id, **slot}
        conn.execute(
            """
            INSERT INTO dzi_blueprint_slots (
                blueprint_id, slot_number, exam_part, task_kind, points,
                topic_area, required_asset_type
            )
            VALUES (
                :blueprint_id, :slot_number, :exam_part, :task_kind, :points,
                :topic_area, :required_asset_type
            )
            ON CONFLICT(blueprint_id, slot_number) DO UPDATE SET
                exam_part = excluded.exam_part,
                task_kind = excluded.task_kind,
                points = excluded.points,
                topic_area = excluded.topic_area,
                required_asset_type = excluded.required_asset_type
            """,
            params,
        )
        if existed:
            updated += 1
        else:
            inserted += 1

    return inserted, updated


def totals(conn: sqlite3.Connection, blueprint_id: int) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total_slots, COALESCE(SUM(points), 0) AS total_points
        FROM dzi_blueprint_slots
        WHERE blueprint_id = ?
        """,
        (blueprint_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def seed(db_path: Path) -> int:
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        with conn:
            blueprint_id = upsert_blueprint(conn)
            inserted, updated = upsert_slots(conn, blueprint_id)
            total_slots, total_points = totals(conn, blueprint_id)
    finally:
        conn.close()

    print(f"blueprint_id: {blueprint_id}")
    print(f"slots inserted: {inserted}")
    print(f"slots updated: {updated}")
    print(f"total slots: {total_slots}")
    print(f"total points: {total_points}")

    if total_slots != 28 or total_points != 100:
        print(
            f"Invalid blueprint totals: slots={total_slots}, points={total_points}",
            file=sys.stderr,
        )
        return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed DZI IT profiled-preparation blueprint.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    raise SystemExit(seed(args.db))


if __name__ == "__main__":
    main()
