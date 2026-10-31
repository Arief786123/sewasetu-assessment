# Supplied citizen seed data

The original assessment handout supplies `seed.sql`. It is intentionally excluded from Git
because it contains citizen personal and banking data. Copy the original file into this
folder on the deployment host, then import it into a newly initialized database:

```bash
docker compose up -d db
docker compose exec -T db psql -U sewasetu -d sewasetu < seed/seed.sql
```

Do not import the seed repeatedly into an existing database. Back up the database before
any restore or migration. The schema and indexes are initialized by `app/schema.sql`.
