"""Export must name the original its File row is required to cite.

Migration 0019 makes ``files.primary_source_id`` NOT NULL and unique. Ingestion
already satisfied that; ``document_export`` did not, and no test exercised its
SQL at all, which is how the gap survived to the edge of a deployment. These
regressions run the real service against a recording fake connection, so the
whole write path is exercised with no database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from doc_store_server.runtime.document_export import DocumentExportService


DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
FIRST_PARAGRAPH = "First paragraph."
SECOND_PARAGRAPH = "Second paragraph."
BODY = f"{FIRST_PARAGRAPH}\n\n{SECOND_PARAGRAPH}"


class _FakeResult:
    def __init__(self, rows: list[Any], mapping_rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows
        self._mapping_rows = mapping_rows or []

    def scalars(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def mappings(self) -> "_FakeResult":
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping_rows[0] if self._mapping_rows else None


class _FakeConnection:
    """Records every statement and answers the reads the export performs."""

    def __init__(self, *, existing_primary_source: UUID | None = None) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self._existing_primary_source = existing_primary_source

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        self.statements.append((sql, dict(parameters or {})))
        if sql.startswith("SELECT id, title, block_meta FROM documents"):
            return _FakeResult([], [{"id": DOCUMENT_ID, "title": "Doc", "block_meta": {}}])
        if sql.startswith("SELECT text FROM paragraphs"):
            return _FakeResult([FIRST_PARAGRAPH, SECOND_PARAGRAPH])
        if sql.startswith("SELECT primary_source_id FROM files"):
            return _FakeResult([self._existing_primary_source] if self._existing_primary_source else [])
        return _FakeResult([])

    def issued(self, prefix: str) -> list[dict[str, Any]]:
        return [params for sql, params in self.statements if sql.startswith(prefix)]

    def position_of(self, prefix: str) -> int:
        for index, (sql, _) in enumerate(self.statements):
            if sql.startswith(prefix):
                return index
        raise AssertionError(f"no statement starting with {prefix!r}")


class _FakeTransaction:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeConnection:
        return self._connection

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self.disposed = 0

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._connection)

    def dispose(self) -> None:
        self.disposed += 1


def _install(monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection) -> _FakeEngine:
    engine = _FakeEngine(connection)
    monkeypatch.setattr(
        "doc_store_server.runtime.document_export.create_engine",
        lambda *args, **kwargs: engine,
    )
    return engine


def _export(
    connection: _FakeConnection,
    tmp_path: Path,
    *,
    file_id: str | None = None,
) -> dict[str, Any]:
    service = DocumentExportService("postgresql+psycopg://unused/unused")
    return service.export_document(
        document_id=str(DOCUMENT_ID),
        path=str(tmp_path / "export.txt"),
        file_id=file_id,
        overwrite=True,
    )


def test_export_writes_the_file_row_with_a_primary_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The central claim: the File row names an original, or 0019 rejects it."""

    connection = _FakeConnection()
    _install(monkeypatch, connection)

    result = _export(connection, tmp_path)

    files_rows = connection.issued("INSERT INTO files")
    assert len(files_rows) == 1
    assert "primary_source_id" in files_rows[0]
    assert files_rows[0]["primary_source_id"] is not None
    assert str(files_rows[0]["primary_source_id"]) == result["primary_source_id"]

    sql = next(sql for sql, _ in connection.statements if sql.startswith("INSERT INTO files"))
    assert "primary_source_id" in sql, "the column must be named in the statement, not only bound"


def test_the_original_is_inserted_before_the_file_that_references_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The foreign key is RESTRICT, so ordering is a correctness property."""

    connection = _FakeConnection()
    _install(monkeypatch, connection)

    _export(connection, tmp_path)

    assert connection.position_of("INSERT INTO primary_sources") < connection.position_of(
        "INSERT INTO files"
    )


def test_the_original_records_the_exported_bytes_not_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = _FakeConnection()
    _install(monkeypatch, connection)

    result = _export(connection, tmp_path)

    source = connection.issued("INSERT INTO primary_sources")[0]
    content = (tmp_path / "export.txt").read_bytes()
    assert source["original_sha256"] == hashlib.sha256(content).hexdigest()
    assert source["byte_length"] == len(content)
    assert source["source_locator"] == f"file://{tmp_path / 'export.txt'}"
    assert str(DOCUMENT_ID) in source["provenance"]
    assert result["byte_length"] == len(content)


def test_an_export_is_deterministic_in_the_identity_it_mints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same file id and same bytes must not mint a second original."""

    file_id = str(uuid4())
    first = _FakeConnection()
    _install(monkeypatch, first)
    one = _export(first, tmp_path, file_id=file_id)

    second = _FakeConnection()
    _install(monkeypatch, second)
    two = _export(second, tmp_path, file_id=file_id)

    assert one["primary_source_id"] == two["primary_source_id"]


def test_a_file_that_already_names_an_original_keeps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An original is immutable; re-exporting does not rebind the File."""

    bound = uuid4()
    connection = _FakeConnection(existing_primary_source=bound)
    _install(monkeypatch, connection)

    result = _export(connection, tmp_path)

    assert result["primary_source_id"] == str(bound)
    assert connection.issued("INSERT INTO primary_sources") == []
    assert connection.issued("INSERT INTO files")[0]["primary_source_id"] == bound
