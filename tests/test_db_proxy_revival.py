"""Dev proxy self-healing in db.connect() (2026-08-11).

The recurring failure: ADC lapses ~weekly and the long-running
cloud-sql-proxy keeps cached credentials, so every connect dies with
"server closed the connection unexpectedly" until the proxy is restarted.
connect() now performs one revival + one retry, and only for proxy-shaped
failures on a proxy-socket host — service/deploy error paths are unchanged.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import psycopg
import pytest

from x1_advisor import db

SOCKET_HOST = "/home/u/cloudsql/proj:region:instance"
STALE = "connection failed: ... server closed the connection unexpectedly"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DB_PASS", "test")
    monkeypatch.setenv("ADVISOR_PGHOST", SOCKET_HOST)
    monkeypatch.delenv("ADVISOR_PROXY_AUTOSTART", raising=False)
    monkeypatch.setattr(db, "_proxy_binary", lambda: "/fake/cloud-sql-proxy")


def test_proxy_shaped_failure_revives_and_retries(monkeypatch):
    calls = {"connect": 0, "revive": 0}
    sentinel = object()

    def fake_connect(**kwargs):
        calls["connect"] += 1
        if calls["connect"] == 1:
            raise psycopg.OperationalError(STALE)
        return sentinel

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    monkeypatch.setattr(db, "_revive_proxy",
                        lambda host: calls.__setitem__("revive", calls["revive"] + 1))
    assert db.connect() is sentinel
    assert calls == {"connect": 2, "revive": 1}


def test_non_proxy_error_raises_without_revival(monkeypatch):
    # wrong password is an OperationalError too — it must NOT bounce the proxy
    def fake_connect(**kwargs):
        raise psycopg.OperationalError(
            'password authentication failed for user "postgres"')

    revived = []
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    monkeypatch.setattr(db, "_revive_proxy", lambda host: revived.append(host))
    with pytest.raises(psycopg.OperationalError, match="password"):
        db.connect()
    assert not revived


def test_failure_that_survives_the_retry_raises(monkeypatch):
    monkeypatch.setattr(db.psycopg, "connect",
                        lambda **kw: (_ for _ in ()).throw(
                            psycopg.OperationalError(STALE)))
    monkeypatch.setattr(db, "_revive_proxy", lambda host: None)
    with pytest.raises(psycopg.OperationalError):
        db.connect()                      # one revival, one retry, then raise


def test_kill_switch_disables_revival(monkeypatch):
    monkeypatch.setenv("ADVISOR_PROXY_AUTOSTART", "0")
    assert not db._proxy_recoverable(SOCKET_HOST,
                                     psycopg.OperationalError(STALE))


def test_non_socket_host_is_never_revived():
    # TCP/connector hosts (deploy) don't look like project:region:instance
    assert not db._proxy_recoverable("127.0.0.1",
                                     psycopg.OperationalError(STALE))


def test_missing_binary_means_no_revival(monkeypatch):
    monkeypatch.setattr(db, "_proxy_binary", lambda: None)
    assert not db._proxy_recoverable(SOCKET_HOST,
                                     psycopg.OperationalError(STALE))


def test_expired_adc_reports_the_actual_fix(monkeypatch):
    monkeypatch.setattr(db.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=1, stderr="invalid_rapt", stdout=""))
    with pytest.raises(RuntimeError, match="application-default login"):
        db._check_adc()


def test_unavailable_gcloud_does_not_block_revival(monkeypatch):
    def no_gcloud(*a, **k):
        raise FileNotFoundError("gcloud")
    monkeypatch.setattr(db.subprocess, "run", no_gcloud)
    db._check_adc()                       # no raise: the proxy is the judge
