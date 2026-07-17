from __future__ import annotations

import pytest

from doc_store_server.runtime.search_config import (
    SEMANTIC_REFINEMENT_DEFAULTS,
    default_search_config,
    normalize_search_config,
    runtime_search_config,
    validate_search_config,
)


def test_default_search_config_is_generated_from_constants() -> None:
    generated = default_search_config()

    assert generated == {"semantic_refinement": SEMANTIC_REFINEMENT_DEFAULTS}
    assert generated["semantic_refinement"] is not SEMANTIC_REFINEMENT_DEFAULTS


def test_runtime_search_config_uses_config_over_constants() -> None:
    config = runtime_search_config(
        {
            "search": {
                "semantic_refinement": {
                    "enabled": True,
                    "threshold": 0.42,
                    "candidate_limit": 77,
                    "result_limit": 8,
                    "diagnostics": True,
                }
            }
        }
    )

    assert config.semantic_refinement.as_dict() == {
        "enabled": True,
        "threshold": 0.42,
        "candidate_limit": 77,
        "result_limit": 8,
        "diagnostics": True,
    }


def test_runtime_search_config_falls_back_to_constants_per_missing_field() -> None:
    config = runtime_search_config({"search": {"semantic_refinement": {"threshold": 0.25}}})

    assert config.semantic_refinement.as_dict() == {
        **SEMANTIC_REFINEMENT_DEFAULTS,
        "threshold": 0.25,
    }


def test_validate_search_config_rejects_invalid_refinement_defaults() -> None:
    with pytest.raises(ValueError, match="threshold"):
        validate_search_config({"semantic_refinement": {"threshold": 1.1}})

    with pytest.raises(ValueError, match="candidate_limit"):
        validate_search_config({"semantic_refinement": {"candidate_limit": 0}})

    with pytest.raises(ValueError, match="enabled"):
        validate_search_config({"semantic_refinement": {"enabled": "not-bool"}})


def test_normalize_search_config_adds_validated_generated_section() -> None:
    normalized = normalize_search_config({"server": {"port": 8000}})

    assert normalized["server"] == {"port": 8000}
    assert normalized["search"] == default_search_config()
