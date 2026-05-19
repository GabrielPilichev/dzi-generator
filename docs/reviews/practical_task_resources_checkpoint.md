# Practical Task Resources Checkpoint

Status as of this checkpoint:

- All 7 DZI sources now have Part 1 imported: each current `dzi_it_pp_2025_format` DZI source has 25 linked Part 1 task rows.
- Practical resources are imported for all prepared practical-task sources. In this checkout, the prepared practical batches are `aug_2022_v2`, `may_2022_v1`, `aug_2023_v2`, `may_2023_v2`, `may_2024_v1`, and `may_2025_v2`.
- `aug_2024_v2` is included in the 7-source Part 1 set, but has no prepared practical batch and therefore has 0 practical resource rows.
- ZIP-internal resource references are supported through `zip_path::member_path` resource paths.
- Practical scoring integration and combined final score handling are not complete yet.
- Next work: manual smoke test of resource downloads, student uploads, and teacher review.

Read-only SQLite query used for the resource count table:

```sql
SELECT
  lower(substr(e.session, 1, 3)) || '_' || e.year || '_v' || e.variant AS source_slug,
  et.task_number,
  COUNT(ptr.id) AS resource_count
FROM exams e
JOIN exam_tasks et ON et.exam_id = e.id
LEFT JOIN practical_task_resources ptr ON ptr.exam_task_id = et.id
WHERE e.subject = 'informatika_it'
  AND e.level = 'DZI'
  AND e.format_version = 'dzi_it_pp_2025_format'
  AND et.task_number BETWEEN 26 AND 28
GROUP BY e.id, et.task_number
ORDER BY e.year, e.session, e.variant, et.task_number;
```

Resource counts by source and practical task:

| Source | Task 26 | Task 27 | Task 28 | Total |
| --- | ---: | ---: | ---: | ---: |
| `aug_2022_v2` | 1 | 0 | 3 | 4 |
| `may_2022_v1` | 1 | 2 | 2 | 5 |
| `aug_2023_v2` | 1 | 6 | 4 | 11 |
| `may_2023_v2` | 1 | 10 | 5 | 16 |
| `aug_2024_v2` | 0 | 0 | 0 | 0 |
| `may_2024_v1` | 1 | 17 | 6 | 24 |
| `may_2025_v2` | 1 | 9 | 7 | 17 |

Part 1 import check:

| Source | Linked Part 1 Tasks | Practical Task Slots |
| --- | ---: | ---: |
| `aug_2022_v2` | 25 | 3 |
| `may_2022_v1` | 25 | 3 |
| `aug_2023_v2` | 25 | 3 |
| `may_2023_v2` | 25 | 3 |
| `aug_2024_v2` | 25 | 3 |
| `may_2024_v1` | 25 | 3 |
| `may_2025_v2` | 25 | 3 |
