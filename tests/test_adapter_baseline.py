"""Executable baseline checks for the adapter/server boundary."""

from __future__ import annotations

import ast
import multiprocessing
from pathlib import Path
from typing import Any, Iterable, cast


ROOT: Path = Path(__file__).resolve().parents[1]
SOURCE_ROOT: Path = ROOT / "src"


def _python_files() -> tuple[Path, ...]:
    """Return project Python files, excluding this evidence module."""
    return tuple(
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if path.name != "test_adapter_baseline.py"
    )


def _source_text(paths: Iterable[Path]) -> str:
    """Read source files as one searchable, newline-delimited string."""
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _imports(paths: Iterable[Path]) -> tuple[str, ...]:
    """Collect import names from project modules."""
    names: list[str] = []
    for path in paths:
        tree: ast.Module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names.append(node.module)
    return tuple(names)


def _command_names(registry: Any) -> frozenset[str]:
    """Read the adapter registry's command names without relying on private app state."""
    commands: Any = getattr(registry, "_commands", None)
    assert isinstance(commands, dict), "mcp-proxy-adapter registry must expose command names"
    return frozenset(str(name) for name in commands)


def _spawn_registration_worker(result: Any) -> None:
    """Rebuild the adapter registry in a real spawn-mode queue worker."""
    from mcp_proxy_adapter.commands.command_registry import CommandRegistry

    from doc_store_server.main import initialize_spawned_worker

    worker_registry: Any = CommandRegistry()
    lifecycle_result = initialize_spawned_worker(worker_registry)
    result.put((lifecycle_result, sorted(_command_names(worker_registry))))


def test_adapter_is_the_exclusive_transport_boundary() -> None:
    """The server depends on one adapter boundary and owns no competing HTTP API."""
    project_config: str = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    source: str = _source_text(_python_files())
    imports: tuple[str, ...] = _imports(_python_files())

    assert "mcp-proxy-adapter" in project_config
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)
    assert "APIRouter" not in source
    assert not any(token in source for token in ("@app.route", "@app.get", "@app.post"))
    assert "mcp_proxy_adapter" in source or "register_custom_commands_hook" in source


def test_legacy_authority_is_absent() -> None:
    """Legacy transport, client, splitting, vectorization, and query authority stay out."""
    forbidden_path_parts: tuple[str, ...] = (
        "queryspec",
        "query_language",
        "document_ast",
        "splitter",
        "vectorizer",
        "http_client",
    )
    forbidden_symbols: tuple[str, ...] = (
        "QuerySpec",
        "DocumentAST",
        "DocumentAst",
        "LocalSplitter",
        "LocalVectorizer",
    )
    paths: tuple[Path, ...] = tuple(path for path in ROOT.rglob("*") if path.is_file())
    source: str = _source_text(_python_files())

    legacy_client_package: Path = SOURCE_ROOT / "doc_store_client"
    assert not any(path.is_file() for path in legacy_client_package.rglob("*"))
    vectorization_package: Path = SOURCE_ROOT / "doc_store_server" / "vectorization"
    assert not any(path.is_file() for path in vectorization_package.rglob("*"))
    assert not any(
        any(part in path.as_posix().lower() for part in forbidden_path_parts)
        for path in paths
    )
    assert not any(symbol in source for symbol in forbidden_symbols)
    assert not any(
        name in {"requests", "httpx", "urllib3"} or name.startswith("http.client")
        for name in _imports(_python_files())
    )


def test_module_responsibilities_are_explicit() -> None:
    """Current server, persistence, filtering, ingestion, migration, and test roles are split."""
    required_paths: tuple[Path, ...] = (
        SOURCE_ROOT / "doc_store_server",
        SOURCE_ROOT / "doc_store_server" / "core",
        SOURCE_ROOT / "doc_store_server" / "db",
        SOURCE_ROOT / "doc_store_server" / "filters",
        SOURCE_ROOT / "doc_store_server" / "ingestion",
        ROOT / "migrations",
        ROOT / "tests",
    )
    missing: list[str] = [str(path.relative_to(ROOT)) for path in required_paths if not path.exists()]
    assert not missing, f"missing explicit responsibility boundaries: {missing}"


def test_main_and_spawn_worker_register_the_same_adapter_commands() -> None:
    """The adapter registration hook must produce identical main/worker registries."""
    import mcp_proxy_adapter
    from doc_store_server.main import (
        initialize_command_registry,
        register_doc_store_commands,
    )

    registry_type: Any = getattr(
        __import__("mcp_proxy_adapter.commands.command_registry", fromlist=["CommandRegistry"]),
        "CommandRegistry",
    )
    assert mcp_proxy_adapter is not None
    assert callable(register_doc_store_commands)

    main_registry: Any = registry_type()
    main_result: int = initialize_command_registry(main_registry)
    main_names: frozenset[str] = _command_names(main_registry)

    context: multiprocessing.context.SpawnContext = multiprocessing.get_context("spawn")
    queue: Any = context.Queue()
    worker: multiprocessing.Process = context.Process(
        target=_spawn_registration_worker,
        args=(queue,),
        name="doc-store-baseline-queue-worker",
    )
    worker.start()
    worker_result: tuple[int, list[str]] = cast(tuple[int, list[str]], queue.get(timeout=15))
    worker.join(timeout=15)
    assert worker.exitcode == 0
    worker_result_code, worker_names = worker_result
    assert main_result == worker_result_code
    assert frozenset(worker_names) == main_names
