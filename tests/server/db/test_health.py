"""Database health probes return diagnostics instead of raising."""

from __future__ import annotations

from sqlalchemy.exc import OperationalError

from doc_store_server.db.health import check_database_health, database_url_from_config


def test_database_url_from_config_handles_missing_sections() -> None:
    assert database_url_from_config(None) is None
    assert database_url_from_config({}) is None
    assert database_url_from_config({"database": {}}) is None
    assert database_url_from_config({"database": {"url": "postgresql://db"}}) == "postgresql://db"


def test_check_database_health_reports_not_configured() -> None:
    status = check_database_health(None)
    assert status.as_dict() == {
        "configured": False,
        "ok": False,
        "error_type": "not_configured",
    }


def test_check_database_health_converts_connection_errors_to_status(monkeypatch) -> None:
    class BrokenEngine:
        def connect(self) -> object:
            raise OperationalError("select 1", {}, RuntimeError("db timeout"))

        def dispose(self) -> None:
            pass

    monkeypatch.setattr("doc_store_server.db.health.create_engine", lambda *a, **k: BrokenEngine())
    status = check_database_health("postgresql+psycopg://host/db", connect_timeout=1)

    assert status.configured is True
    assert status.ok is False
    assert status.error_type == "OperationalError"
    assert "db timeout" in (status.error or "")
