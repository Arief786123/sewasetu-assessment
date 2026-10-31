"""Regression tests for citizen privacy and high-impact inherited defects."""
import hashlib
import sys
from pathlib import Path
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as portal


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows
        self.rowcount = 1
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        if self.rows is not None:
            return self.rows
        sql, _ = self.calls[-1]
        # An ID-only lookup simulates another citizen's record being present.
        if "FROM applications WHERE id = %s" in sql and "mobile = %s" not in sql:
            return ("SSP26000001", "Citizen Example", "9999999999", None,
                    "Village", "Sonari", "1234", "ABCD0123456", "PENDING",
                    datetime(2026, 1, 1), None)
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor=None):
        self.cur = cursor or FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


def test_citizen_cannot_read_another_mobile_application(monkeypatch):
    fake = FakeConnection()
    monkeypatch.setattr(portal, "get_db", lambda: fake)
    client = portal.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True  # legacy generic flag must not grant access
        sess["citizen_authenticated"] = True
        sess["portal_mobile"] = "8888888888"
    response = client.get("/application/1")
    assert response.status_code == 404
    sql, params = fake.cur.calls[-1]
    assert "mobile = %s" in sql
    assert params == (1, "8888888888")


def test_citizen_session_cannot_approve_application(monkeypatch):
    client = portal.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["citizen_authenticated"] = True
        sess["portal_mobile"] = "8888888888"
    response = client.post("/admin/approve/1")
    assert response.status_code == 403


def test_apply_requires_the_generated_captcha(monkeypatch):
    monkeypatch.setattr(portal, "deadline_remaining", lambda: 60)
    get_db_called = []
    monkeypatch.setattr(portal, "get_db", lambda: get_db_called.append(True))
    client = portal.app.test_client()
    client.get("/apply")
    response = client.post("/apply", data={"mobile": "9876543210"})
    assert response.status_code == 200
    assert b"Security check failed" in response.data
    assert get_db_called == []


def test_password_reset_rejects_sql_injection_before_database(monkeypatch):
    get_db_called = []
    monkeypatch.setattr(portal, "get_db", lambda: get_db_called.append(True))
    client = portal.app.test_client()
    response = client.post("/status/reset", data={"mobile": "' OR '1'='1"})
    assert response.status_code == 200
    assert get_db_called == []


def test_legacy_sha256_password_is_verified_and_upgraded():
    legacy = hashlib.sha256("old-password".encode()).hexdigest()
    valid, upgraded = portal.verify_portal_password(legacy, "old-password")
    assert valid is True
    assert upgraded and upgraded != legacy
    assert portal.check_password_hash(upgraded, "old-password")


def test_citizen_names_are_not_truncated_to_twenty_bytes():
    name = "শ্রীমতী লক্ষ্মী দেৱী"
    assert portal.normalize_citizen_name(name) == name


def test_db_connections_are_reused_from_a_bounded_pool(monkeypatch):
    created = []

    class Pool:
        def __init__(self, **kwargs):
            created.append(kwargs)
        def getconn(self):
            return FakeConnection()
        def putconn(self, connection, close=False):
            pass

    monkeypatch.setattr(portal, "ThreadedConnectionPool", Pool)
    monkeypatch.setattr(portal, "_db_pool", None)
    with portal.app.test_request_context("/"):
        first = portal.get_db()
        first.close()
        second = portal.get_db()
        second.close()
    assert len(created) == 1
    assert created[0]["maxconn"] <= 4
