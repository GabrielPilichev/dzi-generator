# Confirmed DZI Image Asset Extraction Plan

This is the next review-first step after:

- `docs/reviews/confirmed_dzi_image_question_mappings.json`
- `src/map_confirmed_dzi_image_questions.py`
- `src/validate_confirmed_dzi_image_mappings.py`

This plan does not import assets, write the DB, run migrations, or add image
files. Official question numbers below are PDF task numbers, not LearnPilot DB
question IDs.

## Inputs

Use the reviewed mapping file as the source of truth:

```bash
python3 src/validate_confirmed_dzi_image_mappings.py
```

The current mapping has 37 ready image mappings and 2 explicit no-image notes.
Only entries with `needs_image: true` and `confidence` of `exact` or `likely`
should be extracted.

## Official PDF Location

Keep official PDF files under:

```text
data/reference/dzi/official_pdfs/
```

Expected PDF names:

```text
aug_2022_v2_exam.pdf
aug_2023_v2_exam.pdf
aug_2024_v2_exam.pdf
may_2022_v1_exam.pdf
may_2023_v2_exam.pdf
may_2024_v1_exam.pdf
may_2025_v2_exam.pdf
```

Legacy/original downloads may remain under `data/reference/<Bulgarian folder>/`,
but extraction work should read from the normalized `official_pdfs` paths above.

## Target Asset Directory

Store reviewed extracted question images under:

```text
web/static/img/questions/dzi/
```

Do not place generated files in `vault/Generated/Quizzes/`.

Do not add files in this planning PR. The future asset-file PR should add only
reviewed PNG files in this directory.

## Filename Convention

Every image file must use:

```text
<source_slug>_q<official_question_number>.png
```

Examples:

```text
web/static/img/questions/dzi/aug_2022_v2_q2.png
web/static/img/questions/dzi/may_2025_v2_q22.png
```

The filename must exactly match `expected_asset_filename` in
`confirmed_dzi_image_question_mappings.json`.

## Manual Extraction Workflow

For each ready mapping:

1. Open `data/reference/dzi/official_pdfs/<source_slug>_exam.pdf`.
2. Find the official PDF task number from `official_question_number`.
3. Crop the visual region needed to answer the task. Include labels, headers,
   axes, table headings, answer-choice figures, and any visible context needed
   for the prompt to be answerable.
4. Export the crop as PNG using the exact expected filename.
5. Place it under `web/static/img/questions/dzi/`.
6. Visually review the PNG against the official PDF.
7. Run the file-presence validator in non-strict mode while work is in progress:

   ```bash
   python3 src/validate_confirmed_dzi_asset_files.py
   ```

8. Before an asset-file PR is ready for review, run strict mode:

   ```bash
   python3 src/validate_confirmed_dzi_asset_files.py --strict
   ```

Manual tools that are acceptable for extraction include Preview, Acrobat,
Okular, screenshots, or a local PDF renderer. Semi-automated extraction is also
acceptable if every crop is manually verified against the official PDF before
commit.

## Semi-Automated Extraction Notes

If using a local PDF tool, generate temporary crops outside the repo or under a
throwaway ignored location, then copy only reviewed PNGs into
`web/static/img/questions/dzi/`.

Possible local commands, depending on installed tools:

```bash
pdftoppm -png -r 200 data/reference/dzi/official_pdfs/<source_slug>_exam.pdf /tmp/<source_slug>
```

or:

```bash
magick -density 200 data/reference/dzi/official_pdfs/<source_slug>_exam.pdf[PAGE] -crop WIDTHxHEIGHT+X+Y /tmp/crop.png
```

Do not rely on automated crop coordinates without manual review. The final PNG
must contain the whole visual dependency and no unrelated answer-key material.

## File Validation

The optional file validator checks that every ready mapping has a safe expected
filename and reports whether the target PNG exists under
`web/static/img/questions/dzi/`.

Default mode is non-blocking for missing files so this planning PR can pass
without adding images:

```bash
python3 src/validate_confirmed_dzi_asset_files.py
```

Strict mode should be used in the future asset-file PR:

```bash
python3 src/validate_confirmed_dzi_asset_files.py --strict
```

Expected output includes:

- total ready mappings
- assets found
- assets missing
- unexpected files in the target directory
- validation errors

## Future Asset-File PR

The next PR after this plan should:

- add reviewed PNG files under `web/static/img/questions/dzi/`
- keep filenames exactly aligned with the mapping file
- run `src/validate_confirmed_dzi_image_mappings.py`
- run `src/validate_confirmed_dzi_asset_files.py --strict`
- avoid DB writes, migrations, and importer changes

## Future DB-Linking PR

A later DB-linking PR should:

- resolve each mapping by `source_slug + official_question_number`
- confirm it still points to the mapped `db_question_id`
- create or update asset metadata for `web/static/img/questions/dzi/<filename>`
- link each asset to the corresponding question and official exam task where
  possible
- avoid changing prompt text, answers, options, scoring, or historical quiz data
- run `pragma_foreign_key_check` after any intentional DB update

The linking PR should prefer the existing `assets` and `asset_links` model. If
runtime rendering needs legacy compatibility, any `questions.image_path` updates
must be deliberate and documented in that PR.
