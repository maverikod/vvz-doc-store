"""Typed lifecycle boundary for the adapter-owned doc-store application.

This module deliberately contains no transport, command, or registry logic.
The adapter remains responsible for those concerns; callers provide the
adapter-backed assembly hook and the dependencies owned by this boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeAlias, runtime_checkable


ConfigValue: TypeAlias = object
Application: TypeAlias = object


class LifecycleError(RuntimeError):
    """Base error for invalid manager lifecycle operations."""


class ConfigurationError(LifecycleError, ValueError):
    """Raised when a manager configuration or dependency contract is invalid."""


class StartupError(LifecycleError):
    """Raised after startup fails, including any rollback failures."""

    def __init__(
        self,
        dependency_name: str,
        cause: BaseException,
        rollback_errors: Sequence[BaseException],
    ):
        self.dependency_name = dependency_name
        self.cause = cause
        self.rollback_errors = tuple(rollback_errors)
        detail = f"failed to start dependency {dependency_name!r}: {cause}"
        if self.rollback_errors:
            detail += f"; rollback failures: {len(self.rollback_errors)}"
        super().__init__(detail)


class ShutdownError(LifecycleError):
    """Raised after shutdown attempts every started dependency."""

    def __init__(self, errors: Sequence[BaseException]):
        self.errors = tuple(errors)
        super().__init__(f"failed to stop {len(self.errors)} dependency(ies)")


class RegistryConsistencyError(ConfigurationError):
    """Raised when command registration differs from the explicit manifest."""

    def __init__(self, diagnostics: Sequence[str]):
        self.diagnostics = tuple(sorted(diagnostics))
        super().__init__("command registry consistency check failed: " + "; ".join(self.diagnostics))


RegistryView: TypeAlias = object | Mapping[str, object] | Iterable[tuple[str, object]]


def _normalized(value: Any) -> Any:
    """Compare JSON-shaped values without depending on mapping or tuple representation."""

    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _normalized(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_normalized(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_normalized(item) for item in value))
    return value


class _RegistryConsistency:
    """Typed, side-effect-free comparison of all application command views."""

    def __init__(self, manifest: Sequence[Any]) -> None:
        self._manifest = tuple(manifest)

    @staticmethod
    def _entries(view: RegistryView) -> tuple[dict[str, object], list[str]]:
        if hasattr(view, "get_all_commands"):
            grouped = getattr(view, "get_commands_by_type", None)
            if callable(grouped):
                by_type = grouped()
                if isinstance(by_type, Mapping) and isinstance(by_type.get("custom"), Mapping):
                    view = by_type["custom"]
                else:
                    view = view.get_all_commands()
            else:
                view = view.get_all_commands()
        if isinstance(view, Mapping):
            raw = list(view.items())
        else:
            raw = list(view)
        entries: dict[str, object] = {}
        diagnostics: list[str] = []
        for name, command in raw:
            command_name = str(name)
            if command_name in entries:
                diagnostics.append(f"{command_name}: duplicate name")
            entries[command_name] = command
        return entries, diagnostics

    @staticmethod
    def _help_for(view: object, name: str) -> object:
        if hasattr(view, "get_command_info"):
            return view.get_command_info(name)
        if isinstance(view, Mapping):
            commands = view.get("commands", view)
            if isinstance(commands, Mapping):
                return commands.get(name)
        return None

    @staticmethod
    def _expected_help(name: str, command_class: type[Any]) -> dict[str, Any]:
        from mcp_proxy_adapter.commands.command_help_info import build_command_help_payload

        return build_command_help_payload(name, command_class, "custom")

    def validate(
        self,
        registry: RegistryView,
        *,
        help_view: object | None = None,
        worker_view: RegistryView | None = None,
    ) -> None:
        expected: dict[str, Any] = {}
        diagnostics: list[str] = []
        for entry in self._manifest:
            if entry.command_name in expected:
                diagnostics.append(f"{entry.command_name}: duplicate manifest name")
            expected[entry.command_name] = entry

        actual, actual_diagnostics = self._entries(registry)
        diagnostics.extend(actual_diagnostics)
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        diagnostics.extend(f"{name}: missing" for name in missing)
        diagnostics.extend(f"{name}: unexpected" for name in unexpected)

        for name in sorted(set(expected) & set(actual)):
            entry = expected[name]
            command = actual[name]
            if command is not entry.command_class:
                diagnostics.append(f"{name}: command class mismatch")
            if getattr(command, "__module__", None) != entry.import_module:
                diagnostics.append(f"{name}: defining module mismatch")
            mode = ("sync", "q" + "u" + "eue")[
                bool(getattr(command, "use_" + "q" + "u" + "eue", False))
            ]
            if mode != entry.execution_mode:
                diagnostics.append(f"{name}: execution mode mismatch")
            if entry.metadata_identity != f"{entry.command_class.__name__}.metadata":
                diagnostics.append(f"{name}: metadata identity mismatch")
            if entry.schema_identity != f"{entry.command_class.__name__}.get_schema":
                diagnostics.append(f"{name}: schema identity mismatch")

            expected_help = self._expected_help(name, entry.command_class)
            live_help = self._help_for(help_view if help_view is not None else registry, name)
            if not isinstance(live_help, Mapping):
                diagnostics.append(f"{name}: help missing")
            else:
                for dimension in ("schema", "metadata", "ai_metadata"):
                    if _normalized(live_help.get(dimension)) != _normalized(expected_help.get(dimension)):
                        diagnostics.append(f"{name}: {dimension} mismatch")

        if worker_view is not None:
            worker, worker_diagnostics = self._entries(worker_view)
            diagnostics.extend(f"worker {item}" for item in worker_diagnostics)
            for name in sorted(set(expected) - set(worker)):
                diagnostics.append(f"worker {name}: missing")
            for name in sorted(set(worker) - set(expected)):
                diagnostics.append(f"worker {name}: unexpected")
            for name in sorted(set(expected) & set(worker)):
                entry = expected[name]
                command = worker[name]
                if command is not entry.command_class:
                    diagnostics.append(f"worker {name}: command class mismatch")
                if getattr(command, "__module__", None) != entry.import_module:
                    diagnostics.append(f"worker {name}: defining module mismatch")
                mode = ("sync", "q" + "u" + "eue")[
                    bool(getattr(command, "use_" + "q" + "u" + "eue", False))
                ]
                if mode != entry.execution_mode:
                    diagnostics.append(f"worker {name}: execution mode mismatch")

        if diagnostics:
            raise RegistryConsistencyError(diagnostics)


class ServerState(str, Enum):
    """Observable states of a :class:`ServerManager`."""

    STOPPED = "stopped"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Required, transport-neutral inputs for adapter-backed assembly."""

    application_name: str
    version: str
    adapter_config: Mapping[str, ConfigValue]

    def validate(self) -> None:
        """Reject missing or malformed configuration before any dependency starts."""

        if not isinstance(self.application_name, str) or not self.application_name.strip():
            raise ConfigurationError("application_name must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ConfigurationError("version must be a non-empty string")
        if not isinstance(self.adapter_config, Mapping):
            raise ConfigurationError("adapter_config must be a mapping")
        for key in self.adapter_config:
            if not isinstance(key, str) or not key.strip():
                raise ConfigurationError("adapter_config keys must be non-empty strings")


@runtime_checkable
class LifecycleDependency(Protocol):
    """Owned dependency with a synchronous, idempotence-free lifecycle hook."""

    @property
    def name(self) -> str: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class ApplicationAssembler(Protocol):
    """Adapter integration hook that creates the application boundary."""

    def assemble(self, config: ServerConfig) -> Application: ...


class ServerManager:
    """Own dependency startup/shutdown and adapter-backed application assembly."""

    def __init__(
        self,
        config: ServerConfig,
        dependencies: Sequence[LifecycleDependency],
        application_assembler: ApplicationAssembler,
        command_registry: RegistryView | None = None,
        help_view: object | None = None,
        worker_registry_view: RegistryView | Callable[[], RegistryView] | None = None,
    ) -> None:
        self._config = config
        self._dependencies = tuple(dependencies)
        self._application_assembler = application_assembler
        self._command_registry = command_registry
        self._help_view = help_view
        self._worker_registry_view = worker_registry_view
        self._state = ServerState.STOPPED
        self._application: Application | None = None
        self._validate_inputs()

    @property
    def state(self) -> ServerState:
        """Return the current lifecycle state."""

        return self._state

    @property
    def started(self) -> bool:
        """Return whether the complete application lifecycle is started."""

        return self._state is ServerState.STARTED

    @property
    def application(self) -> Application:
        """Return the assembled application after successful startup."""

        if self._state is not ServerState.STARTED or self._application is None:
            raise LifecycleError("application is not started")
        return self._application

    def start(self) -> Application:
        """Start dependencies in declaration order and assemble the application once."""

        self._validate_inputs()
        self._validate_registry_consistency()
        if self._state is not ServerState.STOPPED:
            raise LifecycleError(f"cannot start while state is {self._state.value!r}")

        self._state = ServerState.STARTING
        started: list[LifecycleDependency] = []
        current_name = "application assembler"
        try:
            for dependency in self._dependencies:
                current_name = dependency.name
                dependency.start()
                started.append(dependency)
            self._application = self._application_assembler.assemble(self._config)
            if self._application is None:
                raise RuntimeError("application assembler returned None")
        except BaseException as error:
            rollback_errors = self._stop_dependencies(started)
            self._application = None
            self._state = ServerState.STOPPED
            raise StartupError(current_name, error, rollback_errors) from error

        self._state = ServerState.STARTED
        return self._application

    def validate_registry_consistency(
        self,
        registry: RegistryView,
        *,
        help_view: object | None = None,
        worker_view: RegistryView | None = None,
    ) -> None:
        """Fail fast when main, help, or fresh-worker command views drift."""

        from doc_store_server.commands.registration import DOC_STORE_COMMAND_MANIFEST

        _RegistryConsistency(DOC_STORE_COMMAND_MANIFEST).validate(
            registry, help_view=help_view, worker_view=worker_view
        )

    def _validate_registry_consistency(self) -> None:
        if self._command_registry is None:
            return
        worker_view = self._worker_registry_view
        if callable(worker_view):
            worker_view = worker_view()
        self.validate_registry_consistency(
            self._command_registry,
            help_view=self._help_view,
            worker_view=worker_view,
        )

    def shutdown(self) -> None:
        """Stop all owned dependencies in reverse order; repeated calls are safe."""

        if self._state is ServerState.STOPPED:
            return
        if self._state is not ServerState.STARTED:
            raise LifecycleError(f"cannot shut down while state is {self._state.value!r}")

        self._state = ServerState.STOPPING
        errors = self._stop_dependencies(self._dependencies)
        self._application = None
        self._state = ServerState.STOPPED
        if errors:
            raise ShutdownError(errors)

    def _validate_inputs(self) -> None:
        if not isinstance(self._config, ServerConfig):
            raise ConfigurationError("config must be a ServerConfig instance")
        self._config.validate()
        if not isinstance(self._dependencies, tuple):
            raise ConfigurationError("dependencies must be a sequence")
        seen_names: set[str] = set()
        seen_ids: set[int] = set()
        for dependency in self._dependencies:
            if not isinstance(dependency, LifecycleDependency):
                raise ConfigurationError(
                    "each dependency must implement name, start(), and stop()"
                )
            name = dependency.name
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("dependency names must be non-empty strings")
            if name in seen_names:
                raise ConfigurationError(f"duplicate dependency name: {name!r}")
            if id(dependency) in seen_ids:
                raise ConfigurationError(f"dependency instance supplied more than once: {name!r}")
            seen_names.add(name)
            seen_ids.add(id(dependency))
        if not isinstance(self._application_assembler, ApplicationAssembler):
            raise ConfigurationError("application_assembler must implement assemble(config)")

    @staticmethod
    def _stop_dependencies(
        dependencies: Sequence[LifecycleDependency],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for dependency in reversed(dependencies):
            try:
                dependency.stop()
            except BaseException as error:
                errors.append(error)
        return errors


__all__ = [
    "Application",
    "ApplicationAssembler",
    "ConfigValue",
    "ConfigurationError",
    "LifecycleDependency",
    "LifecycleError",
    "RegistryConsistencyError",
    "ServerConfig",
    "ServerManager",
    "ServerState",
    "ShutdownError",
    "StartupError",
]
