# Sewa Setu — evidence, prioritization, and handover notes

## Observed production evidence (January–June 2026)

| Signal | Observed value | Interpretation |
|---|---:|---|
| Access-log requests parsed | 1,515,987 | Six-month traffic sample |
| HTTP 500 responses | 6,886 | All observed on `/declaration` |
| HTTP 504 responses | 6,873 | All observed on `/declaration` |
| PostgreSQL `too many clients already` events | 13,759 | Direct evidence of database connection exhaustion |
| SMS gateway read timeouts | 62 | OTP/SMS delivery reliability issue |
| 500 response latency, median / p95 | 20.388 s / 28.132 s | Requests often waited close to the worker timeout |
| 504 response latency | 30.001 s | Matches the inherited Gunicorn 30-second timeout |

The combined 500 and 504 count is 13,759, matching the PostgreSQL connection-exhaustion event count. Every 500/504 in the parsed access log was on `/declaration`. This is strong correlation and a high-confidence priority, but it does not prove every request had an identical causal chain.

The seed file contains approximately 100,000 application rows. It is excluded from the repository because it contains personal and banking information. No citizen rows were deleted or rewritten by the code changes.

## Top five fixes, ranked by citizen impact

### 1. Stop declaration failures caused by database connection exhaustion

**Evidence:** 13,759 PostgreSQL max-client events; 13,759 combined 500/504 responses, all on `/declaration`; 504s hit 30.001 seconds. The old application opened a fresh PostgreSQL connection per operation and had no bounded reuse strategy.

**Change:** bounded `ThreadedConnectionPool` per Gunicorn worker, request-teardown release even on exceptions, 4 connections per worker (16 maximum across four workers), connect timeout, and indexes for mobile/status/OTP query paths.

**Regression test:** `test_db_connections_are_reused_from_a_bounded_pool`.

### 2. Prevent citizens from reading another applicant's records

**Evidence:** the old application status query selected by application ID only. A citizen session was not bound to the mobile number on the requested record. The acknowledgment PDF route likewise did not verify ownership.

**Change:** status reads and acknowledgment downloads are scoped to the authenticated mobile number. Staff authorization uses a separate `admin_authenticated` session flag; a citizen's legacy generic login flag cannot authorize staff actions. Approve/reject transitions are limited to pending applications.

**Regression tests:** `test_citizen_cannot_read_another_mobile_application` and `test_citizen_session_cannot_approve_application`.

### 3. Remove weak credential and password-reset paths

**Evidence:** hardcoded administrator credentials, unsalted fast SHA-256 citizen password hashes, and a reset flow that interpolated mobile input into SQL and reset passwords to date of birth.

**Change:** administrator credentials and Flask session secret are environment-provided in production; citizen hashes use Werkzeug's slow KDF with transparent upgrade for existing SHA-256 hashes after a successful login; reset requires a time-limited SMS OTP and a new password of at least 10 characters; SQL uses parameterized statements.

**Regression tests:** `test_password_reset_rejects_sql_injection_before_database` and `test_legacy_sha256_password_is_verified_and_upgraded`.

### 4. Make mobile verification meaningful and resistant to guessing

**Evidence:** the CAPTCHA field was read but never checked; OTPs were stored in plaintext and compared directly; there was no attempt limit or resend throttle; SMS HTTP errors were ignored.

**Change:** server-side CAPTCHA validation, cryptographically generated six-digit OTP, SHA-256 digest at rest, one-minute resend throttle, five failed attempts per session, one-time OTP consumption, and explicit SMS delivery failure handling.

**Regression test:** `test_apply_requires_the_generated_captcha`.

### 5. Preserve citizen data and make document handling safer

**Evidence:** applicant names were truncated to 20 UTF-8 bytes, which can corrupt Indian-language names; US-style date parsing was tried before Indian day/month order; uploads trusted only the `.pdf` extension and reused the supplied filename; PDF generation drew thousands of tiny lines.

**Change:** preserve up to the schema's 100-character name limit without byte truncation, parse ISO and Indian day/month dates before the US fallback, check PDF magic bytes and size, use randomized upload filenames, and replace the PDF line loop with one rectangle.

**Regression test:** `test_citizen_names_are_not_truncated_to_twenty_bytes`.

## Changes intentionally not made

- **No bulk data cleanup or deletion:** the seed is production-like citizen data; only schema metadata/indexes are added.
- **No visual redesign:** the brief prioritizes reliability, privacy, and citizen journeys over cosmetic changes.
- **No replacement of the fake SMS provider:** it is part of the assessment harness; the app still exposes its required `/__gateway/` path. That proxy must be restricted or removed before any real public deployment.
- **No unilateral change to statutory pension eligibility or deemed-approval rules:** these require the department's legal/administrative confirmation.
- **No claim that the local test suite passed:** this execution environment has no Docker binary and cannot reach package indexes to install Flask/psycopg2/pytest dependencies. Python syntax compilation and Git integrity checks are available; GitHub Actions is configured to run the regression suite after push.

## Remaining release gates

1. Review and transcribe `dc-briefing.mp3`; incorporate any additional citizen complaints before locking the top-five ranking. The audio is about 3 minutes 15 seconds long.
2. Run `docker compose up --build`, restore the supplied `seed/seed.sql` exactly once, and execute `pytest -q` in an environment with Docker and Python dependencies.
3. Deploy to a persistent VM/VPS or map the services into a cloud platform. Keep PostgreSQL and the SMS gateway private.
4. Run the live application probe manually: health check, citizen application flow, OTP read via `/__gateway/`, admin login, record ownership isolation, admin approve/reject, and backup restore.
5. Add `harshnisar` as a read-only collaborator on the private GitHub repository.
6. Record the Loom only after the deployed URL, tests, and assessor credentials are verified.

## Three-minute Loom script (draft)

**0:00–0:25 — Context and evidence**

"I inherited Sewa Setu with six months of access and application logs. The clearest operational signal was 13,759 PostgreSQL max-client errors. The access log had 6,886 HTTP 500s and 6,873 HTTP 504s; every one was on the declaration endpoint, and the 504s reached the 30-second Gunicorn timeout. I treated that as the first citizen-impact issue."

**0:25–2:20 — Ranked fixes**

"First, I bounded and reused database connections and added request-teardown cleanup and query indexes. Second, I closed the citizen record-ownership gap by binding status and acknowledgment access to the logged-in mobile number, and separated staff authorization. Third, I removed the hardcoded admin credential, upgraded citizen password hashing, and replaced the SQL-interpolated date-of-birth reset with OTP verification. Fourth, I made CAPTCHA validation real and hardened OTP generation, storage, expiry, attempt limits, and resend behavior. Fifth, I preserved Unicode names, corrected Indian date parsing precedence, validated PDF signatures and sizes, randomized filenames, and reduced PDF rendering work. Each area has a regression test committed before the corresponding fix."

**2:20–2:50 — What I did not change**

"I did not delete or bulk-edit citizen records, redesign the interface, replace the assessment SMS simulator, or reinterpret statutory eligibility and deemed approval. Those choices either risked citizen data or needed departmental authorization."

**2:50–3:00 — Status**

"The repository includes the fix history, tests, deployment configuration, and operational notes. I will submit the public URL and assessor credentials only after the deployed service and its data restore have been verified."

*This script must be updated after reviewing the DC briefing and after all release gates pass.*
