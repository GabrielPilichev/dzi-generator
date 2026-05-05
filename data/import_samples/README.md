# Import Samples

Files in this directory are sample fixtures for validating importer behavior.

The sample source slug `sample_2099_v0` is intentionally fictional. It should not resolve to a real exam in production `data/questions.db`.

Use them for dry-run validation only:

```bash
python3 src/import_dzi_questions_json.py \
  --json data/import_samples/dzi_sample_part1.json \
  --dry-run \
  --allow-missing-assets
```

With the production database, dry-run validates the JSON shape only up to source resolution. To validate the full import path, use a disposable test database that contains a matching sample exam and exam task skeleton.

Never run sample files against production `data/questions.db` without `--dry-run`. Do not run sample imports against production data without a disposable copy of the database. Sample prompts, answers, and options are fake and may overwrite real official rows if they ever use a real `source_slug` and task number.

Sample JSON files should include `_sample_only: true`. The importer rejects those files unless `--dry-run` is used.
