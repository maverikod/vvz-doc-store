"""Architecture contracts for external adapters and package boundaries."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
SERVER_ROOT = SOURCE_ROOT / "doc_store_server"

EXTERNAL_ROOTS = {
    "chunk_metadata_adapter",
    "embed_client",
    "mcp_proxy_adapter",
    "svo_client",
}
SQL_ROOTS = {"alembic", "asyncpg", "psycopg", "sqlalchemy"}
FORBIDDEN_NAMES = {
    "ChunkDeserializer",
    "ChunkQuery",
    "ChunkQueryFactory",
    "ChunkSerializer",
    "DocumentAST",
    "DocumentAst",
    "Embedding",
    "EmbeddingClient",
    "LocalSplitter",
    "LocalVectorizer",
    "QueryParser",
    "QuerySpec",
    "SemanticChunk",
    "SemanticChunkFactory",
    "SvoChunker",
    "SvoChunkerClient",
    "Vectorizer",
}
FORBIDDEN_PATH_PARTS = {
    "document_ast",
    "query_language",
    "queryspec",
    "splitter",
    "vectorizer",
}

def _python_files() -> tuple[Path, ...]:
    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _defined_names(path: Path, tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if path == SERVER_ROOT / "db" / "schema.py" and node.name == "SemanticChunk":
                continue
            names.add(node.name)
    return names


def _all_source_text(paths: Iterable[Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_external_models_and_clients_are_consumed_from_public_interfaces() -> None:
    from chunk_metadata_adapter import ChunkQuery, SemanticChunk
    from embed_client import EmbeddingClient
    from mcp_proxy_adapter import Command, create_app
    from svo_client import SvoChunkerClient

    assert SemanticChunk.__module__ == "chunk_metadata_adapter.semantic_chunk"
    assert ChunkQuery.__module__ == "chunk_metadata_adapter.chunk_query"
    assert EmbeddingClient.__module__.startswith("embed_client.")
    assert SvoChunkerClient.__module__.startswith("svo_client.")
    assert callable(create_app)
    assert isinstance(Command, type)

    for path in _python_files():
        tree = _module_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in EXTERNAL_ROOTS:
                    assert all(not alias.name.startswith("_") for alias in node.names), path
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in EXTERNAL_ROOTS:
                        assert not alias.name.split(".")[-1].startswith("_"), path


def test_local_copies_and_competing_query_or_vectorization_mechanisms_are_absent() -> None:
    paths = _python_files()
    source = _all_source_text(paths)
    defined = set().union(*(_defined_names(path, _module_tree(path)) for path in paths))

    assert not FORBIDDEN_NAMES & defined
    assert "lark" not in source.lower()
    assert not any(path.suffix == ".lark" for path in SERVER_ROOT.rglob("*"))
    assert not any(
        part in path.relative_to(SOURCE_ROOT).as_posix().lower()
        for path in (*paths, *SERVER_ROOT.rglob("*"))
        for part in FORBIDDEN_PATH_PARTS
    )
    assert not any(
        token in source
        for token in ("from lark", "import lark", "FilterParser(", "QueryParser(")
    )


def test_transport_domain_and_sql_persistence_responsibilities_do_not_mix() -> None:
    paths = _python_files()
    assert (SERVER_ROOT / "domain").is_dir()
    assert (SERVER_ROOT / "db").is_dir()

    transport_modules: list[Path] = []
    for path in paths:
        imports = _import_roots(_module_tree(path))
        relative = path.relative_to(SERVER_ROOT)
        if "mcp_proxy_adapter" in imports:
            transport_modules.append(path)
            assert "domain" not in relative.parts
            assert "db" not in relative.parts
            assert not imports & SQL_ROOTS
        if "domain" in relative.parts:
            assert not imports & (SQL_ROOTS | {"mcp_proxy_adapter"})
        if "db" in relative.parts:
            assert not imports & {"mcp_proxy_adapter"}

    assert transport_modules
