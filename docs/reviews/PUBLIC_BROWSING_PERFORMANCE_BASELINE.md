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

Record the changed files and focused tests, then run broader regression tests proportionate to the change. Compare baseline and optimized results on identical hardware and workload, noting run-to-run variation. Authenticated quiz/SQLite write testing remains a **separate** follow-up.
