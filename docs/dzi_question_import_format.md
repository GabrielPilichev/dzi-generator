# Official DZI Part 1 Question Import Format

## Purpose

This document defines the v1 JSON format for importing official DZI IT Part 1 tasks into the database.

The format is intentionally narrow:

- One JSON file describes one official exam source.
- Only tasks 1-25 are in scope.
- Tasks 1-15 are `multiple_choice`.
- Tasks 16-25 are `short_answer`.
- Assets may be referenced by file path, but assets are optional.
- Practical tasks 26-28 are out of scope for this format.

The importer must not infer official content. If an answer key is missing or incomplete, the importer should reject the file rather than guess answers.

## Full JSON Schema Explanation

Top-level object:

```json
{
  "source_slug": "may_2025_v2",
  "source_title": "ДЗИ ИТ ПП - май 2025, вариант 2",
  "tasks": []
}
```

Required top-level fields:

- `source_slug`: String. Identifies the official exam source, using the existing source naming convention.
- `tasks`: Array. Contains task objects for official Part 1 tasks.

Optional top-level fields:

- `source_title`: String. Human-readable source title. This is documentation metadata for the import file and does not replace the exam row identity.

Task object fields:

```json
{
  "task_number": 1,
  "task_kind": "multiple_choice",
  "points": 1,
  "topic_slug": "optional-topic-slug",
  "section_slug": "optional-section-slug",
  "prompt": "Task prompt text.",
  "source_page": 3,
  "assets": []
}
```

Required task fields:

- `task_number`: Integer. Must be between 1 and 25 for v1.
- `task_kind`: String. Must be `multiple_choice` or `short_answer`.
- `points`: Integer. Official point value for the task.
- `prompt`: String. Official task prompt text.

Optional task fields:

- `topic_slug`: String. Optional curriculum topic lookup key.
- `section_slug`: String. Optional curriculum section lookup key.
- `source_page`: Integer. Page number in the official source PDF.
- `assets`: Array. Optional file references used by the task.

Multiple-choice task fields:

```json
{
  "options": [
    {"letter": "A", "text": "Option A", "is_correct": false},
    {"letter": "B", "text": "Option B", "is_correct": true},
    {"letter": "C", "text": "Option C", "is_correct": false},
    {"letter": "D", "text": "Option D", "is_correct": false}
  ]
}
```

- `options`: Required array with exactly 4 option objects.
- `letter`: String. Usually `A`, `B`, `C`, `D`.
- `text`: String. Official option text.
- `is_correct`: Boolean. Exactly one option must be `true`.

Short-answer task fields:

```json
{
  "answers": ["42"],
  "answer_alternatives": ["42.", "forty-two"],
  "subquestions": []
}
```

- `answers`: Array of strings. Required unless `subquestions` contains at least one item.
- `answer_alternatives`: Optional array of strings. Accepted answer variants for the whole task.
- `subquestions`: Optional array. Use for multi-part short-answer tasks.

Subquestion object fields:

```json
{
  "label": "a",
  "prompt": "Subquestion prompt.",
  "points": 1,
  "answers": ["Correct answer"],
  "answer_alternatives": ["Accepted alternative"]
}
```

- `label`: String. Official or importer-assigned subquestion label.
- `prompt`: String. Subquestion prompt text.
- `points`: Integer. Point value for this subquestion.
- `answers`: Array of strings. At least one correct answer.
- `answer_alternatives`: Optional array of strings.

Asset object fields:

```json
{
  "file_path": "data/assets/exams/may_2025_v2/images/task_18.png",
  "asset_type": "image",
  "caption_bg": "Фигура към задача 18",
  "source_page": 5,
  "source_bbox_json": "{\"x\": 120, \"y\": 240, \"width\": 300, \"height\": 180}"
}
```

Required asset fields:

- `file_path`: String. Path to the asset file.
- `asset_type`: String. One of `image`, `pdf_crop`, `spreadsheet`, `archive`, `other`.

Optional asset fields:

- `caption_bg`: String. Bulgarian caption.
- `source_page`: Integer. Page number in the official PDF.
- `source_bbox_json`: String. JSON-encoded bounding box from the source PDF.

## Example Multiple-Choice Task

```json
{
  "task_number": 1,
  "task_kind": "multiple_choice",
  "points": 1,
  "prompt": "Кое от изброените твърдения е вярно?",
  "source_page": 2,
  "options": [
    {"letter": "A", "text": "Първи отговор", "is_correct": false},
    {"letter": "B", "text": "Втори отговор", "is_correct": true},
    {"letter": "C", "text": "Трети отговор", "is_correct": false},
    {"letter": "D", "text": "Четвърти отговор", "is_correct": false}
  ]
}
```

## Example Short-Answer Task

```json
{
  "task_number": 16,
  "task_kind": "short_answer",
  "points": 3,
  "prompt": "Запишете резултата от изпълнението на алгоритъма.",
  "source_page": 4,
  "answers": ["15"],
  "answer_alternatives": ["15."]
}
```

## Example Short-Answer Task With Image Asset

```json
{
  "task_number": 18,
  "task_kind": "short_answer",
  "points": 3,
  "prompt": "Разгледайте изображението и запишете търсената стойност.",
  "source_page": 5,
  "answers": ["RGB"],
  "assets": [
    {
      "file_path": "data/assets/exams/may_2025_v2/images/task_18.png",
      "asset_type": "image",
      "caption_bg": "Изображение към задача 18",
      "source_page": 5,
      "source_bbox_json": "{\"x\": 84, \"y\": 192, \"width\": 420, \"height\": 210}"
    }
  ]
}
```

## Mapping to DB Tables

`questions`:

- One imported official task creates or updates one `questions` row.
- `prompt` maps to the question text field used by the current schema.
- `task_kind` determines the question type.
- Official imported rows must use `is_ai_generated = 0`.
- Official imported rows must use `quality_score = 1.0`.
- `topic_slug` and `section_slug`, when provided, resolve to topic and section foreign keys.

`multiple_choice_options`:

- Used only for `multiple_choice` tasks.
- Each item in `options` creates or updates one option row.
- `letter`, `text`, and `is_correct` map directly.
- Exactly one option must be correct.

`fill_in_subquestions`:

- Used for `short_answer` tasks when the task has multiple parts.
- Each item in `subquestions` creates or updates one subquestion row.
- `label`, `prompt`, `points`, `answers`, and `answer_alternatives` map to the available fields in the schema.

`exam_tasks`:

- `source_slug` resolves to an `exams` row.
- `task_number` must already exist in `exam_tasks` for that exam.
- The importer links imported question content to the existing exam task skeleton.
- The importer must not create new `exam_tasks` rows in this v1 question import flow.

`exam_task_questions`:

- Links each imported `questions` row to its existing `exam_tasks` row.
- This table records which official question belongs to which official exam slot.

`assets`:

- Each task asset creates or updates one `assets` row keyed by `local_path`.
- `file_path` maps to `assets.local_path`.
- `asset_type`, filename, MIME type, file size, and SHA-256 should be stored when available.

`asset_links`:

- Links task assets to the relevant owner.
- For task-level assets, the owner should be the imported question or exam task, depending on the existing importer design.
- `caption_bg`, `source_page`, and `source_bbox_json` map to link metadata when supported.

## Validation Rules

- `source_slug` must resolve to an existing `exams` row using the source slug naming convention.
- `task_number` must already exist in `exam_tasks` for that source.
- `task_kind` must match `exam_tasks.task_kind` unless an explicit override mechanism is added later.
- `multiple_choice` tasks require exactly 4 options.
- `multiple_choice` tasks require exactly 1 correct option.
- `short_answer` tasks require at least 1 answer or at least 1 subquestion.
- Asset file paths should exist unless the importer is run with `--allow-missing-assets`.
- Official imported questions must use `is_ai_generated = 0`.
- Official imported questions must use `quality_score = 1.0`.
- The importer must reject missing answer keys. It must not guess answers.

## Notes About Images

Store image and crop files under:

```text
data/assets/exams/<source_slug>/
```

Recommended subfolders:

- `images/`
- `pdf_crops/`
- `resources/`

Do not store binary blobs in SQLite. Store files on disk and record metadata plus paths in `assets` and `asset_links`.

## Future Extension

Practical tasks 26-28 will use a separate JSON/resource format later.

That future format should cover practical work environments, provided starter files, expected output files, grading rubrics, and resource archives without expanding the Part 1 import format beyond its current scope.
