# Import Samples

Files in this directory are sample fixtures for validating importer behavior.

Use them for dry-run validation only:

```bash
python3 src/import_dzi_questions_json.py \
  --json data/import_samples/dzi_sample_part1.json \
  --dry-run \
  --allow-missing-assets
```

Do not run sample imports against production `data/questions.db` without a disposable copy of the database. Sample prompts, answers, and options are fake and may overwrite real official rows when they use a real `source_slug` and task number.

Sample JSON files should include `_sample_only: true`. The importer rejects those files unless `--dry-run` is used.
