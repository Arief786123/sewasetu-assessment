# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.
"""

import os
import sys
import configparser
from datetime import datetime, timedelta

import psycopg2
import requests
import logging

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sewasetu.scheduler")

config = configparser.ConfigParser()
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
config.read(CONFIG_PATH)

SLA_DAYS = int(os.environ.get("SLA_DAYS", config.get("pension", "sla_days")))
SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL", config.get("app", "sms_gateway_url"))


def main():
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", config.get("database", "host")),
        port=int(os.environ.get("DB_PORT", config.get("database", "port"))),
        dbname=os.environ.get("DB_NAME", config.get("database", "name")),
        user=os.environ.get("DB_USER", config.get("database", "user")),
        password=os.environ.get("DB_PASSWORD", config.get("database", "password")),
        connect_timeout=5,
        application_name="sewasetu-scheduler",
    )
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    cur.execute(
        """UPDATE applications
           SET status = 'DEEMED_APPROVED', decided_at = %s, decided_by = 'RTPS-AUTO'
           WHERE status = 'PENDING' AND submitted_at < %s
           RETURNING application_no, mobile""",
        (datetime.now(), cutoff))
    rows = cur.fetchall()
    conn.commit()
    for app_no, mobile in rows:
        try:
            response = requests.post(SMS_GATEWAY_URL + "/api/send", json={
                "to": mobile,
                "text": "Sewa Setu: your pension application %s stands approved "
                        "under the RTPS Act." % app_no}, timeout=(2, 4))
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("SMS notification failed for application %s: %s", app_no, exc)
    print("%s deemed approval: %d applications approved"
          % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(rows)))
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
