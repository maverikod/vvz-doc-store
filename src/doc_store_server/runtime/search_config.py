"""Runtime search configuration and validation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


SEMANTIC_REFINEMENT_ENABLED_DEFAULT = False
SEMANTIC_REFINEMENT_THRESHOLD_DEFAULT = 0.0
SEMANTIC_REFINEMENT_CANDIDATE_LIMIT_DEFAULT = 50
SEMANTIC_REFINEMENT_RESULT_LIMIT_DEFAULT = 10
SEMANTIC_REFINEMENT_DIAGNOSTICS_DEFAULT = False

SEMANTIC_REFINEMENT_DEFAULTS: dict[str, bool | float | int] = {
    "enabled": SEMANTIC_REFINEMENT_ENABLED_DEFAULT,
    "threshold": SEMANTIC_REFINEMENT_THRESHOLD_DEFAULT,
    "candidate_limit": SEMANTIC_REFINEMENT_CANDIDATE_LIMIT_DEFAULT,
    "result_limit": SEMANTIC_REFINEMENT_RESULT_LIMIT_DEFAULT,
    "diagnostics": SEMANTIC_REFINEMENT_DIAGNOSTICS_DEFAULT,
}


@dataclass(frozen=True, slots=True)
class RuntimeSemanticRefinementConfig:
    """Default hierarchy-aware semantic search controls."""

    enabled: bool = SEMANTIC_REFINEMENT_ENABLED_DEFAULT
    threshold: float = SEMANTIC_REFINEMENT_THRESHOLD_DEFAULT
    candidate_limit: int = SEMANTIC_REFINEMENT_CANDIDATE_LIMIT_DEFAULT
    result_limit: int = SEMANTIC_REFINEMENT_RESULT_LIMIT_DEFAULT
    diagnostics: bool = SEMANTIC_REFINEMENT_DIAGNOSTICS_DEFAULT

    def as_dict(self) -> dict[str, bool | float | int]:
        """Return a JSON-serializable config mapping."""

        return {
            "enabled": self.enabled,
            "threshold": self.threshold,
            "candidate_limit": self.candidate_limit,
            "result_limit": self.result_limit,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True, slots=True)
class RuntimeSearchConfig:
    """Runtime search config resolved from constants and installed config."""

    semantic_refinement: RuntimeSemanticRefinementConfig

    def as_config_section(self) -> dict[str, Any]:
        """Return the normalized config section."""

        return {"semantic_refinement": self.semantic_refinement.as_dict()}


def default_search_config() -> dict[str, Any]:
    """Generate the default search config section from constants."""

    return {"semantic_refinement": dict(SEMANTIC_REFINEMENT_DEFAULTS)}


def runtime_search_config(config: Mapping[str, Any] | None = None) -> RuntimeSearchConfig:
    """Resolve search settings from installed config, falling back to constants."""

    section = config.get("search") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}
    return validate_search_config(section)


def validate_search_config(section: Mapping[str, Any] | None) -> RuntimeSearchConfig:
    """Validate and normalize one search config section."""

    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError("search config must be an object")
    refinement = section.get("semantic_refinement", {})
    if refinement is None:
        refinement = {}
    if not isinstance(refinement, Mapping):
        raise ValueError("search.semantic_refinement must be an object")
    return RuntimeSearchConfig(
        semantic_refinement=RuntimeSemanticRefinementConfig(
            enabled=_bool_option(
                refinement.get("enabled", SEMANTIC_REFINEMENT_ENABLED_DEFAULT),
                "search.semantic_refinement.enabled",
            ),
            threshold=_bounded_float(
                refinement.get("threshold", SEMANTIC_REFINEMENT_THRESHOLD_DEFAULT),
                "search.semantic_refinement.threshold",
                0.0,
                1.0,
            ),
            candidate_limit=_bounded_int(
                refinement.get(
                    "candidate_limit",
                    SEMANTIC_REFINEMENT_CANDIDATE_LIMIT_DEFAULT,
                ),
                "search.semantic_refinement.candidate_limit",
                1,
                1000,
            ),
            result_limit=_bounded_int(
                refinement.get("result_limit", SEMANTIC_REFINEMENT_RESULT_LIMIT_DEFAULT),
                "search.semantic_refinement.result_limit",
                1,
                1000,
            ),
            diagnostics=_bool_option(
                refinement.get("diagnostics", SEMANTIC_REFINEMENT_DIAGNOSTICS_DEFAULT),
                "search.semantic_refinement.diagnostics",
            ),
        )
    )


def normalize_search_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a config copy with a generated, validated search section."""

    normalized = dict(config)
    normalized["search"] = runtime_search_config(normalized).as_config_section()
    return normalized


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _bool_option(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


__all__ = [
    "RuntimeSearchConfig",
    "RuntimeSemanticRefinementConfig",
    "SEMANTIC_REFINEMENT_DEFAULTS",
    "default_search_config",
    "normalize_search_config",
    "runtime_search_config",
    "validate_search_config",
]
