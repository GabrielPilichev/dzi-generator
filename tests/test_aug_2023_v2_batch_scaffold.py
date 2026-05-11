import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORT_DIR = ROOT / "data" / "import_batches"
EXPECTED_BATCHES = (
    ("aug_2023_v2_part1_tasks_1_5.json", 1, 5),
    ("aug_2023_v2_part1_tasks_6_10.json", 6, 10),
    ("aug_2023_v2_part1_tasks_11_15.json", 11, 15),
    ("aug_2023_v2_part1_tasks_16_20.json", 16, 20),
    ("aug_2023_v2_part1_tasks_21_25.json", 21, 25),
)


def expected_task_kind(task_number):
    if 1 <= task_number <= 10:
        return "multiple_choice"
    if 11 <= task_number <= 13:
        return "short_answer"
    if 14 <= task_number <= 18:
        return "multiple_choice"
    return "short_answer"


class Aug2023V2BatchScaffoldTest(unittest.TestCase):
    def test_future_aug_2023_v2_batch_file_shapes(self):
        any_present = False
        for filename, start, end in EXPECTED_BATCHES:
            path = IMPORT_DIR / filename
            if not path.exists():
                continue
            any_present = True
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)

            self.assertEqual(payload["source_slug"], "aug_2023_v2", filename)
            self.assertIn("tasks", payload, filename)
            self.assertEqual(len(payload["tasks"]), 5, filename)

            task_numbers = [task["task_number"] for task in payload["tasks"]]
            self.assertEqual(task_numbers, list(range(start, end + 1)), filename)

            for task in payload["tasks"]:
                self.assertEqual(
                    task["task_kind"],
                    expected_task_kind(task["task_number"]),
                    filename,
                )

        if not any_present:
            self.skipTest(
                "No aug_2023_v2 batch JSON files exist yet under data/import_batches/; "
                "nothing to validate yet."
            )


if __name__ == "__main__":
    unittest.main()
