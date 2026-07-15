"""Database connectivity probes that never own server process liveness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Best-effort database status used for diagnostics and startup probes."""

    configured: bool
    ok: bool
    error_type: str | None = None
    error: str | None = None
    postgres: str | None = None
    vector: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"configured": self.configured, "ok": self.ok}
        if self.error_type:
            result["error_type"] = self.error_type
        if self.error:
            result["error"] = self.error
        if self.postgres:
            result["postgres"] = self.postgres
        if self.vector:
            result["vector"] = self.vector
        return result


def database_url_from_config(config: Mapping[str, Any] | None) -> str | None:
    """Return a configured database URL without assuming a complete config tree."""

    if not isinstance(config, Mapping):
        return None
    database = config.get("database")
    if not isinstance(database, Mapping):
        return None
    url = database.get("url")
    return str(url) if url else None


def check_database_health(
    url: str | None,
    *,
    connect_timeout: int = 3,
) -> DatabaseHealth:
    """Probe PostgreSQL/pgvector and return failure as data instead of raising."""

    if not url:
        return DatabaseHealth(configured=False, ok=False, error_type="not_configured")

    engine = None
    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": connect_timeout},
        )
        with engine.connect() as connection:
            postgres = connection.execute(text("select version()")).scalar_one()
            vector = connection.execute(
                text("select extversion from pg_extension where extname = 'vector'")
            ).scalar_one_or_none()
        return DatabaseHealth(
            configured=True,
            ok=True,
            postgres=str(postgres).split(",", 1)[0],
            vector=str(vector) if vector is not None else None,
        )
    except (SQLAlchemyTimeoutError, TimeoutError, OSError, SQLAlchemyError) as exc:
        return DatabaseHealth(
            configured=True,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    except Exception as exc:
        return DatabaseHealth(
            configured=True,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if engine is not None:
            engine.dispose()


__all__ = ["DatabaseHealth", "check_database_health", "database_url_from_config"]
