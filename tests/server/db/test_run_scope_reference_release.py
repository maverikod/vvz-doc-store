"""A terminal run must be able to release what its own foreign keys null out.

Migration 0019 declared ``processing_runs.document_id``, ``chapter_id`` and
``chunk_id`` as ``ON DELETE SET NULL`` and, in the same revision, installed a
trigger refusing every update to a terminal run. Deleting a Document makes
PostgreSQL issue exactly that update, so the two rules contradicted each other
and no document with a completed run could be deleted. Observed live as
``restrict_violation`` raised by ``doc_store_processing_runs_immutable``.

0021 replaces only the function body. These regressions read the rendered SQL,
which is what actually reaches the database, rather than the Python that
produced it.
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from doc_store_server.db.schema import ProcessingRun


REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "migrations" / "versions" / "0021_run_scope_reference_release.py"

SCOPE_REFERENCES = ("document_id", "chapter_id", "chunk_id")
AUDIT_COLUMNS = (
    "run_id",
    "primary_source_id",
    "run_kind",
    "comparison_scope",
    "actor",
    "configuration_hash",
    "normalization_profile_id",
    "normalization_profile_version",
    "normalization_parameters",
    "created_at",
)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0021", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(name: str) -> str:
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        getattr(module, name)()
    return buffer.getvalue()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("upgrade")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("downgrade")


def test_the_revision_continues_the_applied_chain() -> None:
    module = _module()
    assert module.revision == "0021_run_scope_reference_release"
    assert module.down_revision == "0020_source_span_diagnostics"


def test_only_the_function_is_replaced(upgrade_sql: str) -> None:
    """0019's tables, foreign keys and trigger stay exactly as they are.

    This is the inverse check: the parity tests over 0019's rendered DDL must
    remain valid, which they cannot if this revision reshapes a table.
    """

    assert "CREATE OR REPLACE FUNCTION doc_store_processing_runs_immutable" in upgrade_sql
    assert "CREATE TABLE" not in upgrade_sql
    assert "ALTER TABLE" not in upgrade_sql
    assert "CREATE TRIGGER" not in upgrade_sql
    assert "DROP" not in upgrade_sql


def test_releasing_a_scope_reference_is_recognised(upgrade_sql: str) -> None:
    """Each of the three references may go from a value to NULL, or stay put."""

    for column in SCOPE_REFERENCES:
        assert (
            f"NEW.{column} IS NOT DISTINCT FROM OLD.{column}" in upgrade_sql
        ), column
        assert (
            f"OLD.{column} IS NOT NULL AND NEW.{column} IS NULL" in upgrade_sql
        ), column


def test_the_terminal_refusal_now_yields_to_a_pure_release(upgrade_sql: str) -> None:
    """The central claim: a terminal run is refused unless it is only releasing."""

    assert "AND NOT releasing_scope_only" in upgrade_sql
    terminal_check = re.search(
        r"IF OLD\.status IN \([^)]*\)([^\n]*)THEN", upgrade_sql
    )
    assert terminal_check is not None
    assert "releasing_scope_only" in terminal_check.group(1)


def test_a_reference_may_be_released_but_never_acquired(upgrade_sql: str) -> None:
    """A run must not start claiming rows it did not produce.

    The permitted transition is one-way by construction: NULL to a value is not
    among the accepted shapes, so it falls through to the terminal refusal.
    """

    for column in SCOPE_REFERENCES:
        assert f"OLD.{column} IS NULL AND NEW.{column} IS NOT NULL" not in upgrade_sql


def test_every_non_reference_column_is_guarded(upgrade_sql: str) -> None:
    """A column added to the table later must not be silently rewritable.

    The guard ladder is built from the ORM declaration rather than a restated
    list, so this asserts the whole current table, not a snapshot of it.
    """

    declared = {column.name for column in ProcessingRun.__table__.columns}
    guarded = set(re.findall(r"NEW\.(\w+) IS DISTINCT FROM OLD\.\1", upgrade_sql))
    missing = declared - set(SCOPE_REFERENCES) - guarded
    assert missing == set(), sorted(missing)


def test_the_audit_identity_is_still_refused(upgrade_sql: str) -> None:
    """The inverse check: nothing this revision does may loosen the audit guard."""

    assert "audit identity is immutable" in upgrade_sql
    for column in AUDIT_COLUMNS:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in upgrade_sql
    assert "NEW.status IS DISTINCT FROM OLD.status" in upgrade_sql


def test_the_refusal_message_and_hint_survive(upgrade_sql: str) -> None:
    assert "is immutable once terminal" in upgrade_sql
    assert "Record a new ProcessingRun" in upgrade_sql
    assert "Releasing document_id, chapter_id or chunk_id to NULL is" in upgrade_sql


def test_downgrade_restores_the_0019_body(downgrade_sql: str) -> None:
    assert "CREATE OR REPLACE FUNCTION doc_store_processing_runs_immutable" in downgrade_sql
    assert "releasing_scope_only" not in downgrade_sql
    assert "is immutable once terminal" in downgrade_sql
    assert "audit identity is immutable" in downgrade_sql
