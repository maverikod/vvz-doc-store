"""Executable lifecycle and responsibility-boundary checks for ServerManager."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from doc_store_server.server_manager import (
    ConfigurationError,
    LifecycleError,
    ServerConfig,
    ServerManager,
    ServerState,
    ShutdownError,
    StartupError,
)


@dataclass
class FakeDependency:
    name: str
    events: list[str]
    fail_on_start: bool = False
    fail_on_stop: bool = False

    def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_on_start:
            raise RuntimeError(f"start failed: {self.name}")

    def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_on_stop:
            raise RuntimeError(f"stop failed: {self.name}")


@dataclass
class FakeAssembler:
    events: list[str]
    application: object = field(default_factory=object)
    fail: bool = False

    def assemble(self, config: ServerConfig) -> object:
        self.events.append(f"assemble:{config.application_name}")
        if self.fail:
            raise RuntimeError("assembly failed")
        return self.application


def _config(**changes: Any) -> ServerConfig:
    values: dict[str, Any] = {
        "application_name": "doc-store",
        "version": "1.0.0",
        "adapter_config": {"registration": "adapter"},
    }
    values.update(changes)
    return ServerConfig(**values)


def _manager(
    dependencies: tuple[FakeDependency, ...] = (),
    *,
    events: list[str] | None = None,
    config: ServerConfig | object | None = None,
    assembler: FakeAssembler | object | None = None,
) -> tuple[ServerManager, list[str], FakeAssembler]:
    log = [] if events is None else events
    real_assembler = FakeAssembler(log) if assembler is None else assembler
    manager = ServerManager(
        _config() if config is None else config,  # type: ignore[arg-type]
        dependencies,
        real_assembler,  # type: ignore[arg-type]
    )
    assert isinstance(real_assembler, FakeAssembler)
    return manager, log, real_assembler


def test_typed_inputs_are_validated_before_any_lifecycle_side_effect() -> None:
    events: list[str] = []
    dependency = FakeDependency("database", events)

    invalid_inputs = (
        _config(application_name=""),
        _config(version=""),
        _config(adapter_config=[]),
        _config(adapter_config={1: "invalid"}),
        _config(),
    )
    for config in invalid_inputs[:-1]:
        with pytest.raises(ConfigurationError):
            _manager((dependency,), events=events, config=config)
    with pytest.raises(ConfigurationError):
        _manager((dependency, dependency), events=events)
    with pytest.raises(ConfigurationError):
        _manager((object(),), events=events)
    with pytest.raises(ConfigurationError):
        _manager((dependency,), events=events, assembler=object())
    with pytest.raises(ConfigurationError):
        _manager((dependency,), events=events, config=object())

    assert events == []


def test_success_starts_dependencies_once_in_declared_order_and_exposes_state() -> None:
    events: list[str] = []
    dependencies = (FakeDependency("database", events), FakeDependency("worker", events))
    manager, _, assembler = _manager(dependencies, events=events)

    assert manager.state is ServerState.STOPPED
    with pytest.raises(LifecycleError):
        _ = manager.application
    application = manager.start()

    assert application is assembler.application
    assert manager.state is ServerState.STARTED
    assert manager.started
    assert events == ["start:database", "start:worker", "assemble:doc-store"]
    with pytest.raises(LifecycleError):
        manager.start()


def test_partial_start_failure_rolls_back_only_started_dependencies_in_reverse_order() -> None:
    events: list[str] = []
    dependencies = (
        FakeDependency("database", events),
        FakeDependency("worker", events, fail_on_start=True),
        FakeDependency("scheduler", events),
    )
    manager, _, _ = _manager(dependencies, events=events)

    with pytest.raises(StartupError) as raised:
        manager.start()

    assert raised.value.dependency_name == "worker"
    assert manager.state is ServerState.STOPPED
    assert not manager.started
    assert events == ["start:database", "start:worker", "stop:database"]


def test_assembly_failure_rolls_back_all_started_dependencies_and_preserves_cause() -> None:
    events: list[str] = []
    dependencies = (FakeDependency("database", events), FakeDependency("worker", events))
    manager, _, _ = _manager(dependencies, events=events, assembler=FakeAssembler(events, fail=True))

    with pytest.raises(StartupError) as raised:
        manager.start()

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert raised.value.dependency_name == "worker"
    assert manager.state is ServerState.STOPPED
    assert events == [
        "start:database",
        "start:worker",
        "assemble:doc-store",
        "stop:worker",
        "stop:database",
    ]


def test_shutdown_is_reverse_ordered_idempotent_and_clears_application() -> None:
    events: list[str] = []
    dependencies = (FakeDependency("database", events), FakeDependency("worker", events))
    manager, _, _ = _manager(dependencies, events=events)
    manager.start()

    manager.shutdown()
    manager.shutdown()

    assert manager.state is ServerState.STOPPED
    assert not manager.started
    assert events == [
        "start:database",
        "start:worker",
        "assemble:doc-store",
        "stop:worker",
        "stop:database",
    ]
    with pytest.raises(LifecycleError):
        _ = manager.application


def test_shutdown_attempts_every_dependency_preserves_errors_and_final_state() -> None:
    events: list[str] = []
    dependencies = (
        FakeDependency("database", events, fail_on_stop=True),
        FakeDependency("worker", events, fail_on_stop=True),
    )
    manager, _, _ = _manager(dependencies, events=events)
    manager.start()

    with pytest.raises(ShutdownError) as raised:
        manager.shutdown()

    assert len(raised.value.errors) == 2
    assert manager.state is ServerState.STOPPED
    assert not manager.started
    assert events[-2:] == ["stop:worker", "stop:database"]
    manager.shutdown()
    assert events[-2:] == ["stop:worker", "stop:database"]


def test_lifecycle_module_contains_no_transport_or_command_handler_authority() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src" / "doc_store_server" / "server_manager.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    defined_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forbidden_tokens = (
        "FastAPI",
        "APIRouter",
        "REST",
        "JSON-RPC",
        "WebSocket",
        "authenticate",
        "register_proxy",
        "TLS",
        "mTLS",
        "queue",
        "transfer",
    )

    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)
    assert "ServerManager" in defined_names
    assert not any(token in source for token in forbidden_tokens)
    assert not any("command" in name.lower() or "handler" in name.lower() for name in defined_names)
    assert "assemble" in source
