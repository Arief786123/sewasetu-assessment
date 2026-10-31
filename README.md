# Sewa Setu — Old Age Pension Portal

Hardened engineering handover for the Bharat Fellowship 2027 take-home. The application
preserves the supplied citizen records and keeps the staff review panel functional.

## What changed

1. **Database connection exhaustion:** bounded per-worker PostgreSQL connection pools,
   request-teardown cleanup, connection timeouts, and indexes for the high-volume query paths.
2. **Citizen privacy and staff authorization:** citizen application reads and PDF downloads
   are scoped to the authenticated mobile number; staff actions require a distinct admin
   session and only transition pending applications.
3. **Authentication and reset security:** production admin credentials and session secret are
   environment-only; citizen password hashes use Werkzeug's slow password KDF, legacy SHA-256
   hashes upgrade on successful login, and password reset requires an expiring SMS OTP.
4. **OTP and CAPTCHA controls:** server-side CAPTCHA validation, cryptographically generated
   OTPs, hashed OTP storage, one-minute resend throttling, five-attempt session lockout, OTP
   consumption after successful verification, and visible SMS delivery failures.
5. **Citizen data and uploads:** Unicode names are retained without byte truncation, Indian
   date formats are parsed deliberately, upload paths are randomized, PDF signatures and size
   are checked, and PDF border rendering no longer loops thousands of times.

The scheduler now reads the canonical `app/config/app.ini`, and the deployment includes health
checks, restart policies, persistent volumes, and a GitHub Actions regression-test workflow.

## Local setup

Requires Docker Engine / Docker Desktop and Docker Compose v2.

```bash
cp .env.example .env
# Edit .env: use unique random values for SECRET_KEY, ADMIN_PASSWORD and DB_PASSWORD.
docker compose up --build -d
```

The application is at `http://localhost:8000`. The simulated SMS inbox is proxied at
`http://localhost:8000/__gateway/`. The assessor can read OTPs there during automated testing.

### Restore the supplied data

The assessment's `seed/seed.sql` contains personal and banking data and is deliberately not
tracked by Git. Copy it from the original handout into `seed/seed.sql` on the deployment host,
then import it once into the fresh database:

```bash
docker compose exec -T db psql -U sewasetu -d sewasetu < seed/seed.sql
```

Never run this import against an existing production database. Back up first. Do not edit or
delete existing citizen rows. `app/schema.sql` initializes the schema and supporting indexes.

## Production configuration

Use a VM/VPS with persistent disk and Docker Compose; do not use an ephemeral/free tier that
sleeps or discards the PostgreSQL volume. Configure a firewall to expose only TCP 80/443 (or
the assessment port), terminate TLS with a reverse proxy, and keep PostgreSQL/SMS gateway
private to the Docker network. Set the following in `.env` before deployment:

- `APP_ENV=production`
- `SECRET_KEY`: at least 32 random bytes
- `ADMIN_USERNAME` and `ADMIN_PASSWORD`: unique assessor credentials
- `DB_PASSWORD`: unique database password
- `SCHEME_DEADLINE`: only set an extended assessment window when authorized; the shipped
  business-rule deadline remains in `app/config/app.ini`.

The admin panel is at `/admin`. The assessor credentials are the values in the deployment
`.env`; do not commit `.env` or send it in the repository ZIP. `APP_ENV=production` enables
secure session cookies and refuses to start without the session secret and admin password.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The regression suite covers citizen record isolation, admin authorization, CAPTCHA enforcement,
SQL-injection-resistant password reset, legacy password hash upgrade, Unicode name preservation,
and bounded connection-pool reuse. GitHub Actions runs the same suite on pushes and PRs.

## Operations

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs --tail=200 scheduler
docker compose exec db pg_dump -U sewasetu sewasetu > sewasetu-backup.sql
```

Keep a tested database backup and persistent `pgdata` and `uploads` volumes for the full
assessment window. The scheduler runs daily at 02:00. Review its logs and the database health
before making any operational decision that could affect citizen eligibility.

## Evidence and known limits

The supplied January–June 2026 application log contains 13,759 PostgreSQL
`FATAL: sorry, too many clients already` events. The provided access log contains 6,886 HTTP
500 and 6,873 HTTP 504 responses (13,759 combined), consistent with connection exhaustion.
The logs also contain 62 SMS gateway read timeouts. These are observed correlations, not proof
that every individual 500/504 had the same root cause.

The synthetic SMS gateway stores its messages in memory; it is a test fixture, not a production
telecom integration. The public `/__gateway/` proxy exists for the assessment's OTP-reading
probe and must not be exposed in a real citizen-facing deployment.
