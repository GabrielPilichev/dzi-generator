# Public browsing performance baseline (2026-10-09)

This document records the measured baseline and guardrails for the public-browsing optimization PR.

## Environment and limitations

- Production VPS: 1 vCPU, 2 GiB RAM; production Gunicorn remains at one sync worker.
- Load tests were run against an isolated benchmark instance on localhost:8001 through an SSH tunnel using a copied SQLite database, not the production database.
- k6 test: 750 virtual users, GET /, /subjects/physics, and /login with 1–3 second think time.
- Observed benchmark: 24,578 HTTP 200 responses, 228.49 requests/second, p95 1.1 seconds, zero request failures. HTTP throughput includes SSH tunnel overhead.
- During the run, benchmark Gunicorn reached 84% of one CPU, while an sshd process reached 27.5% in their respective samples. Together they can saturate the single core.
- This test does not represent authenticated quiz read/write load or 2,000 simultaneously active students.

## In-process Flask profiler baseline

100 repeated GET requests per route after five warm-ups, using a Flask test client:

| Route | Mean elapsed time per request | SQLite execute calls / 100 requests |
| --- | ---: | ---: |
| / | 6.74 ms | 300 |
| /subjects/physics | 3.95 ms | 200 |
| /login | 3.36 ms | 200 |

The home route exhibited substantial defensive copying within the subject browse cache; the catalog is already cached per Flask request using `g.subject_catalog` but its availability SQL query executes for each new request. These profile results are diagnostic, not reproducible performance guarantees.

## Scope and guardrails

- Investigate safe cross-request reuse of subject availability, preserving current SQLite database and WAL identity invalidation semantics.
- Investigate whether homepage cache copy overhead can be reduced **without** exposing shared mutable structures, leaking request-specific state, or changing page output.
- Prefer a small, provable change over speculative optimizations; retain defensive copying where required.
- Preserve correct behavior for DB replacement, WAL changes, uncommitted transactions, errors, concurrency, and request isolation.
- Add focused tests for semantics and invalidation; measure unchanged baselines against candidate optimization in equivalent conditions.
- No schema migrations, database writes/imports, production deployment/config changes, or unrelated files.
- Do not modify local user data, vault files, credentials, server/tunnel processes, or production DB.

## Validation

Authenticated quiz/SQLite write testing remains a **separate** follow-up.

## Result of the optimization (local, in-process)

**VPS throughput has NOT been benchmarked after this optimization.** Everything below is a Flask test client on a macOS laptop with temporary databases built from migrations and canonical data. It shows CPU saved per request in one process; it is not a throughput or latency prediction for the 1 vCPU VPS, and the SSH-tunnel k6 run above has not been repeated.

### What changed

- Anonymous requests that have not yet opened a request connection read subject availability from the existing versioned browse cache (`web/services/subject_catalog.py`). A warm anonymous `/login` therefore runs 0 SQL statements and opens 0 connections (was 2 and 1).
- Requests that already hold a connection, and every signed-in request, still query their own connection (uncommitted data stays visible to them, nothing is published to the shared entry).
- The home page caches one DZI section plus the totals instead of every section, so each warm copy is smaller.

### Median milliseconds per request

Four alternating base/head rounds in separate processes, WAL database, 300 warm and 60 cold requests per route per round. Round-to-round spread was under 1.5%.

| Route | Warm before | Warm after | Cold before | Cold after |
| --- | ---: | ---: | ---: | ---: |
| `/` | 0.960 | 0.429 (-55%) | 19.47 | 19.84 (+2%) |
| `/subjects/physics` | 0.696 | 0.308 (-56%) | 2.88 | 3.19 (+11%) |
| `/login` | 0.620 | 0.198 (-68%) | 0.645 | 0.748 (+16%) |
| `/grade/8` | 0.761 | 0.371 (-51%) | 7.85 | 8.43 (+7%) |
| `/subjects/physics/8` | 0.681 | 0.296 (-57%) | 1.78 | 2.09 (+17%) |

With a rollback-journal database the warm gain is smaller (-36% to -54%) and the cold change ranges from -1% to +20%.

### Warm versus cold

"Cold" means a committed write happened just before the request, which invalidates the shared entry. The first anonymous request after a write opens one extra query-only connection for the availability read and runs about two extra statements (`/login`: 2 to 3 statements; `/subjects/physics/8`: 2 to 3 connections). The PR is a net win unless more than roughly half of anonymous requests directly follow a write. Quiz and login writes share the browsing database, so write-heavy periods narrow the gain.

### Signed-in and other limitations

- Signed-in requests keep the availability query (6 statements for `/` on a warm cache). Only the smaller home overview copy helps them: `/` about 13-15% faster, other routes unchanged. This is the traffic of the intended student load.
- Routes that open the request connection before reading the catalog (for example `/section/<slug>`, which the home DZI card links to) still run the availability query for anonymous users.
- The public site needs about 216 cache keys against a capacity of 128. A full crawl evicts the home overview once; mixed browsing did not (0 of 300 samples).
- The mean times above are not comparable with the 6.74 ms profiler baseline (a profiled run); only the ratios are.

### Existing stat-based invalidation risks (unchanged by this PR, now covering availability too)

Invalidation compares file stamps (device, inode, size, mtime, ctime) of the database and a non-empty WAL.

- Coarse file timestamps can hide a commit. Simulating timestamps floored to 4 ms, a read-commit-read loop returned a stale value in 196 of 400 cycles (146 of 400 at 1 ms). On APFS with real timestamps there were 0 of 400. Whether the VPS kernel has coarse timestamps was not checked. In production WAL each closing connection checkpoints and rewrites the main file, which narrows the window; in rollback-journal databases a stale value can last until the next write. A possible follow-up is to refuse to publish a result whose newest stamp is very recent.
- Leftover committed WAL frames with no open connection are read correctly, but the first reader's close may checkpoint, so that first result is not published (the second is).
- Several Gunicorn workers each hold a separate cache; production currently runs one worker, so cross-worker staleness is bounded by the same file-stamp check per worker.

### Reproducing the comparison

1. Export the base and head commits with `git archive` into two separate directories.
2. In each, from a Python 3.10+ environment with the test dependencies, build a temporary database with `tests.support.build_test_database`, point `web.app.app.config["DB_PATH"]` at it (as `tests.support.isolated_app` does) and enable WAL for the WAL case.
3. Time `client.get(path)` with `time.perf_counter_ns` for the routes above: warm after 20 warm-ups; cold with a committed write to an unrelated table before each request.
4. Run one process per tree, alternate base and head for at least four rounds, and compare medians of the per-round medians. Count statements and connections by patching `sqlite3.connect` with a tracing wrapper (see `traced_get` in `tests/test_subject_catalog_cache.py`).

## Compatibility of `CachedSubjectBrowser.overview()`

`overview(grades)` now takes a keyword `section_slug=None` and returns `{"section", "question_count", "search_content"}` instead of `{"sections", "question_count", "search_content"}`; the cache key includes the slug.

The only caller in the repository is `web/blueprints/browsing.py` `index()` (the home page). It reads `question_count` and `search_content` as before and takes the DZI section from `overview["section"]` (requested only for the subject marked `dzi`, with `DZI_SECTION_SLUG`). `section` is `None` when the section is not among the grades, which renders the home page without the DZI question count, as before. No template, other route, teacher controller or service calls it, and `question_count`/`search_content` values are unchanged. Any future caller that needs all sections must use `sections_for_grade()`.
