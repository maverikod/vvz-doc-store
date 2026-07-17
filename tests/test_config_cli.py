from __future__ import annotations

import json

from doc_store_server.config_cli import (
    generate_main,
    validate_config_file,
    validate_main,
    validation_errors,
)
from doc_store_server.runtime.embedding_config import DEFAULT_EMBEDDING_PORT, runtime_embedding_config


def test_doc_store_config_generator_wraps_adapter_generator_and_validates(tmp_path) -> None:
    config_path = tmp_path / "config.json"

    status = generate_main(
        [
            "--out",
            str(config_path),
            "--protocol",
            "http",
            "--server-port",
            "18080",
            "--embedding-host",
            "embedding.local",
            "--semantic-refinement-threshold",
            "0.31",
            "--semantic-refinement-candidate-limit",
            "70",
            "--semantic-refinement-result-limit",
            "9",
        ]
    )

    assert status == 0
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["server"]["port"] == 18080
    assert config["embedding"]["host"] == "embedding.local"
    assert config["search"]["semantic_refinement"] == {
        "enabled": False,
        "threshold": 0.31,
        "candidate_limit": 70,
        "result_limit": 9,
        "diagnostics": False,
    }
    assert validation_errors(validate_config_file(config_path)) == []


def test_doc_store_config_generator_returns_failure_when_generated_config_is_invalid(tmp_path) -> None:
    config_path = tmp_path / "config.json"

    status = generate_main(
        [
            "--out",
            str(config_path),
            "--protocol",
            "http",
            "--semantic-refinement-result-limit",
            "0",
        ]
    )

    assert status == 1
    assert any(
        "result_limit" in issue.message
        for issue in validation_errors(validate_config_file(config_path))
    )


def test_doc_store_config_validator_requires_vectorizer_address(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server": {"host": "0.0.0.0", "port": 8000, "protocol": "http"},
                "queue_manager": {"enabled": True, "in_memory": True},
            }
        ),
        encoding="utf-8",
    )

    assert validate_main(["--file", str(config_path)]) == 1
    errors = validation_errors(validate_config_file(config_path))
    assert any("embedding config" in issue.message for issue in errors)


def test_doc_store_config_validator_defaults_vectorizer_port(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server": {"host": "0.0.0.0", "port": 8000, "protocol": "http"},
                "queue_manager": {"enabled": True, "in_memory": True},
                "embedding": {"host": "embedding.local"},
            }
        ),
        encoding="utf-8",
    )

    assert validation_errors(validate_config_file(config_path)) == []
    assert validate_main(["--file", str(config_path)]) == 0
    assert runtime_embedding_config(json.loads(config_path.read_text(encoding="utf-8"))).port == (
        DEFAULT_EMBEDDING_PORT
    )


def test_doc_store_config_validator_command_rejects_invalid_doc_store_sections(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "server": {"host": "0.0.0.0", "port": 8000, "protocol": "http"},
                "queue_manager": {"enabled": True, "in_memory": True},
                "embedding": {
                    "host": "",
                    "port": 8001,
                    "dimension": 384,
                    "batch_size": 16,
                    "wait_timeout": 300,
                    "timeout": 300,
                    "poll_interval": 1,
                    "model": "all-MiniLM-L6-v2",
                },
                "search": {"semantic_refinement": {"threshold": 2.0}},
            }
        ),
        encoding="utf-8",
    )

    assert validate_main(["--file", str(config_path)]) == 1
    errors = validation_errors(validate_config_file(config_path))
    assert any("embedding.host" in issue.message for issue in errors)
    assert any("threshold" in issue.message for issue in errors)
