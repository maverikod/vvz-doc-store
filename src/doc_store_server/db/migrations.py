"""Small runtime migration runner used by the installed container."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection


ROOT = Path(__file__).resolve().parents[3]


def database_url_from_env() -> str | None:
    """Return the install-time database URL without requiring app config parsing."""

    return os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")


def apply_migrations(
    database_url: str | None = None,
    *,
    migrations_dir: Path | None = None,
) -> tuple[str, ...]:
    """Apply every repository migration missing from ``alembic_version``."""

    url = database_url or database_url_from_env()
    if not url:
        raise RuntimeError("DOC_STORE_DATABASE_URL is not configured")

    directory = migrations_dir or default_migrations_dir()
    modules = tuple(_load_migration(path) for path in sorted(directory.glob("*.py")))
    if not modules:
        raise RuntimeError(f"no migrations found in {directory}")

    engine = create_engine(url, pool_pre_ping=True)
    applied_now: list[str] = []
    try:
        with engine.begin() as connection:
            _ensure_version_table(connection)
            applied = _applied_revisions(connection)
            by_revision = {_revision(module): module for module in modules}
            for module in modules:
                revision = _revision(module)
                if revision in applied:
                    continue
                missing = [
                    dependency
                    for dependency in _dependencies(module)
                    if dependency not in applied and dependency not in by_revision
                ]
                if missing:
                    raise RuntimeError(
                        f"migration {revision} has missing dependencies: {', '.join(missing)}"
                    )
                for dependency in _dependencies(module):
                    if dependency not in applied:
                        dependency_module = by_revision[dependency]
                        _run_upgrade(connection, dependency_module)
                        applied.add(dependency)
                        applied_now.append(dependency)
                        _record_revision(connection, dependency)
                _run_upgrade(connection, module)
                applied.add(revision)
                applied_now.append(revision)
                _record_revision(connection, revision)
    finally:
        engine.dispose()
    return tuple(applied_now)


def _ensure_version_table(connection: Connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(128) NOT NULL PRIMARY KEY)"
        )
    )


def _applied_revisions(connection: Connection) -> set[str]:
    rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
    return {str(row) for row in rows}


def _record_revision(connection: Connection, revision: str) -> None:
    connection.execute(
        text(
            "INSERT INTO alembic_version (version_num) VALUES (:revision) "
            "ON CONFLICT (version_num) DO NOTHING"
        ),
        {"revision": revision},
    )


def _run_upgrade(connection: Connection, module: ModuleType) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revision(module: ModuleType) -> str:
    revision = getattr(module, "revision", None)
    if not isinstance(revision, str) or not revision:
        raise RuntimeError(f"migration {module.__name__} has no revision")
    return revision


def _dependencies(module: ModuleType) -> tuple[str, ...]:
    value = getattr(module, "down_revision", None)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def default_migrations_dir() -> Path:
    """Resolve migrations for both source checkouts and installed containers."""

    configured = os.getenv("DOC_STORE_MIGRATIONS_DIR")
    candidates = [
        Path(configured) if configured else None,
        Path("/app/migrations/versions"),
        Path.cwd() / "migrations" / "versions",
        ROOT / "migrations" / "versions",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return candidates[-1]  # type: ignore[return-value]


def main() -> None:
    applied = apply_migrations()
    if applied:
        print("Applied doc-store migrations: " + ", ".join(applied))
    else:
        print("Doc-store migrations are already up to date")


if __name__ == "__main__":
    main()


__all__ = ["apply_migrations", "database_url_from_env", "default_migrations_dir"]
