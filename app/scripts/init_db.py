"""Idempotently apply the checked-in schema before the web worker starts."""
import configparser
import os
import sys
import time
from pathlib import Path

import psycopg2

BASE_DIR = Path(__file__).resolve().parents[1]
config = configparser.ConfigParser()
config.read(BASE_DIR / "config" / "app.ini")


def main():
    attempts = int(os.environ.get("DB_INIT_RETRIES", "30"))
    last_error = None
    for attempt in range(attempts):
        try:
            conn = psycopg2.connect(
                host=os.environ.get("DB_HOST", config.get("database", "host")),
                port=int(os.environ.get("DB_PORT", config.get("database", "port"))),
                dbname=os.environ.get("DB_NAME", config.get("database", "name")),
                user=os.environ.get("DB_USER", config.get("database", "user")),
                password=os.environ.get("DB_PASSWORD", config.get("database", "password")),
                connect_timeout=5,
                application_name="sewasetu-schema-init",
            )
            try:
                with conn.cursor() as cursor:
                    cursor.execute((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
                conn.commit()
            finally:
                conn.close()
            print("Sewa Setu schema is ready.")
            return 0
        except psycopg2.OperationalError as exc:
            last_error = exc
            time.sleep(min(2 + attempt, 10))
    print("Database schema initialization failed: %s" % last_error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
