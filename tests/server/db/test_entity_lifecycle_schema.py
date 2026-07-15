"""Contract tests for entity lifecycle registry migration."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> ModuleType:
    path = ROOT / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_sql(module: ModuleType, fn: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        getattr(module, fn)()
    return output.getvalue()


def test_0005_adds_boolean_deleted_marker_registry_and_triggers() -> None:
    migration = _load("0005_entity_lifecycle_registry")
    sql = _offline_sql(migration, "upgrade")

    assert "CREATE TABLE entity_uuid_registry" in sql
    for table in ("documents", "chapters", "paragraphs", "semantic_chunks"):
        assert f"ALTER TABLE {table} ADD COLUMN is_deleted" in sql
        assert f"CREATE INDEX ix_{table}_is_deleted" in sql
        assert f"trg_{table}_register_entity_uuid" in sql
        assert f"trg_{table}_unregister_entity_uuid" in sql

    downgrade = _offline_sql(migration, "downgrade")
    assert "DROP TABLE entity_uuid_registry" in downgrade
    for table in ("documents", "chapters", "paragraphs", "semantic_chunks"):
        assert f"DROP INDEX ix_{table}_is_deleted" in downgrade
        assert f"ALTER TABLE {table} DROP COLUMN is_deleted" in downgrade
