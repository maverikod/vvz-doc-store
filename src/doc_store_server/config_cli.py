"""doc-store config generation and validation CLI wrappers."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from mcp_proxy_adapter.core.config.simple_config_generator import SimpleConfigGenerator
from mcp_proxy_adapter.core.validation.config_validator import ConfigValidator

from doc_store_server.runtime.embedding_config import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_POLL_INTERVAL,
    DEFAULT_EMBEDDING_PORT,
    DEFAULT_EMBEDDING_PROTOCOL,
    DEFAULT_EMBEDDING_TIMEOUT,
    DEFAULT_EMBEDDING_WAIT_TIMEOUT,
)
from doc_store_server.runtime.search_config import (
    normalize_search_config,
    validate_search_config,
)


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """One config validation issue from adapter or doc-store validation."""

    level: str
    message: str
    section: str | None = None
    key: str | None = None


def generate_config(args: argparse.Namespace) -> dict[str, Any]:
    """Generate doc-store config through the adapter generator and doc-store extensions."""

    out_path = Path(args.out)
    SimpleConfigGenerator().generate(
        protocol=args.protocol,
        with_proxy=True,
        out_path=str(out_path),
        server_host=args.server_host,
        server_port=args.server_port,
        server_cert_file=args.server_ssl_cert,
        server_key_file=args.server_ssl_key,
        server_ca_cert_file=args.server_ssl_ca,
        server_debug=args.server_debug,
        server_log_level=args.server_log_level,
        server_log_dir=args.server_log_dir,
        registration_host=args.registration_host,
        registration_port=args.registration_port,
        registration_protocol=args.registration_protocol,
        registration_ca_cert_file=args.registration_ssl_ca,
        registration_crl_file=args.registration_ssl_crl,
        registration_server_id=args.registration_server_id,
        registration_server_name=args.registration_server_name,
        instance_uuid=None if args.registration_instance_uuid == "REPLACE_ON_INSTALL" else args.registration_instance_uuid,
    )
    config = json.loads(out_path.read_text(encoding="utf-8"))
    config = _apply_doc_store_config_args(config, args)
    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def validate_config_data(
    config: dict[str, Any],
    *,
    config_path: str | None = None,
) -> list[ConfigIssue]:
    """Validate config through the adapter validator and doc-store validators."""

    issues: list[ConfigIssue] = []
    adapter_validator = ConfigValidator(config_path=config_path)
    adapter_validator.config_data = config
    for item in adapter_validator.validate_config():
        issues.append(
            ConfigIssue(
                level=str(item.level),
                message=str(item.message),
                section=item.section,
                key=item.key,
            )
        )
    try:
        validate_search_config(config.get("search") if isinstance(config, dict) else None)
    except ValueError as exc:
        issues.append(ConfigIssue(level="error", message=str(exc), section="search"))
    try:
        _validate_embedding_config(config)
    except ValueError as exc:
        issues.append(ConfigIssue(level="error", message=str(exc), section="embedding"))
    return issues


def validate_config_file(path: str | Path) -> list[ConfigIssue]:
    """Load and validate one config file."""

    config_path = Path(path)
    if not config_path.exists():
        return [ConfigIssue(level="error", message=f"configuration file not found: {config_path}")]
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [ConfigIssue(level="error", message=f"invalid JSON: {exc}")]
    if not isinstance(config, dict):
        return [ConfigIssue(level="error", message="configuration root must be an object")]
    return validate_config_data(config, config_path=str(config_path))


def validation_errors(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    """Return only fatal validation errors."""

    return [issue for issue in issues if issue.level == "error"]


def print_validation_report(issues: list[ConfigIssue]) -> None:
    """Print a compact validation report."""

    errors = validation_errors(issues)
    warnings = [issue for issue in issues if issue.level == "warning"]
    if errors:
        print("Validation failed:", file=sys.stderr)
        for issue in errors:
            print(f"  - {_format_issue(issue)}", file=sys.stderr)
    if warnings:
        print("Validation warnings:", file=sys.stderr)
        for issue in warnings:
            print(f"  - {_format_issue(issue)}", file=sys.stderr)
    if not errors:
        print("Validation OK")


def generate_main(argv: list[str] | None = None) -> int:
    """CLI entry point for config generation."""

    parser = _build_generate_parser()
    args = parser.parse_args(argv)
    generate_config(args)
    issues = validate_config_file(args.out)
    print_validation_report(issues)
    if validation_errors(issues):
        return 1
    print(f"Configuration generated: {args.out}")
    return 0


def validate_main(argv: list[str] | None = None) -> int:
    """CLI entry point for config validation."""

    parser = argparse.ArgumentParser(prog="doc-store-config-validate")
    parser.add_argument("--file", required=True, help="Config JSON file to validate.")
    args = parser.parse_args(argv)
    issues = validate_config_file(args.file)
    print_validation_report(issues)
    return 1 if validation_errors(issues) else 0


def _build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doc-store-config-generate")
    parser.add_argument("--out", default="config.json", help="Output config JSON path.")
    parser.add_argument("--version", default=_package_version(), help="doc-store build version.")
    parser.add_argument("--protocol", choices=["http", "https", "mtls"], default="http")

    parser.add_argument("--server-host", default="0.0.0.0")
    parser.add_argument("--server-port", type=int, default=8000)
    parser.add_argument("--server-servername", default="doc-store")
    parser.add_argument("--server-advertised-host", default="doc-store")
    _add_bool_arg(parser, "--server-debug", default=False)
    parser.add_argument("--server-log-level", default="INFO")
    parser.add_argument("--server-log-dir", default="/var/log/doc-store")
    parser.add_argument("--server-ssl-cert", default="/etc/doc-store/mtls/server.crt")
    parser.add_argument("--server-ssl-key", default="/etc/doc-store/mtls/server.key")
    parser.add_argument("--server-ssl-ca", default="/etc/doc-store/mtls/ca.crt")
    _add_bool_arg(parser, "--server-ssl-check-hostname", default=False)

    _add_bool_arg(parser, "--registration-enabled", default=True)
    parser.add_argument("--registration-protocol", choices=["http", "https", "mtls"], default="http")
    parser.add_argument("--registration-host", default="localhost")
    parser.add_argument("--registration-port", type=int, default=3005)
    parser.add_argument("--registration-register-url", default="http://localhost:3005/register")
    parser.add_argument("--registration-unregister-url", default="http://localhost:3005/unregister")
    parser.add_argument("--registration-heartbeat-url", default="http://localhost:3005/proxy/heartbeat")
    parser.add_argument("--registration-heartbeat-interval", type=int, default=30)
    parser.add_argument("--registration-ssl-ca", default=None)
    parser.add_argument("--registration-ssl-crl", default=None)
    _add_bool_arg(parser, "--registration-ssl-dnscheck", default=False)
    _add_bool_arg(parser, "--registration-ssl-check-hostname", default=False)
    parser.add_argument("--registration-server-id", default="doc-store-vvz")
    parser.add_argument("--registration-server-name", default="doc-store")
    parser.add_argument("--registration-description", default="doc-store MCP adapter server")
    parser.add_argument("--registration-instance-uuid", default="REPLACE_ON_INSTALL")
    _add_bool_arg(parser, "--registration-auto-on-startup", default=True)
    _add_bool_arg(parser, "--registration-auto-on-shutdown", default=True)

    _add_bool_arg(parser, "--queue-enabled", default=True)
    _add_bool_arg(parser, "--queue-in-memory", default=True)
    parser.add_argument("--queue-shutdown-timeout", type=float, default=30.0)
    parser.add_argument("--queue-max-concurrent-jobs", type=int, default=5)
    parser.add_argument("--queue-completed-job-retention-seconds", type=int, default=21600)

    parser.add_argument("--embedding-protocol", choices=["http", "https", "mtls"], default="https")
    parser.add_argument("--embedding-host", default="192.168.254.26")
    parser.add_argument("--embedding-port", type=int, default=8001)
    parser.add_argument("--embedding-provider", default="embedding-service-vvz")
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--embedding-model-version", default="4.0.2")
    parser.add_argument("--embedding-dimension", type=int, default=384)
    parser.add_argument("--embedding-batch-size", type=int, default=16)
    parser.add_argument("--embedding-timeout", type=float, default=300.0)
    parser.add_argument("--embedding-wait-timeout", type=int, default=300)
    parser.add_argument("--embedding-poll-interval", type=float, default=1.0)
    parser.add_argument("--embedding-direct-text-max-chars", type=int, default=0)
    _add_bool_arg(parser, "--embedding-check-hostname", default=False)

    _add_bool_arg(parser, "--semantic-refinement-enabled", default=False)
    parser.add_argument("--semantic-refinement-threshold", type=float, default=0.0)
    parser.add_argument("--semantic-refinement-candidate-limit", type=int, default=50)
    parser.add_argument("--semantic-refinement-result-limit", type=int, default=10)
    _add_bool_arg(parser, "--semantic-refinement-diagnostics", default=False)
    return parser


def _apply_doc_store_config_args(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = normalize_search_config(config)
    server = dict(config.get("server") or {})
    server.update(
        {
            "host": args.server_host,
            "port": args.server_port,
            "protocol": args.protocol,
            "servername": args.server_servername,
            "advertised_host": args.server_advertised_host,
            "debug": args.server_debug,
            "log_level": args.server_log_level,
            "log_dir": args.server_log_dir,
        }
    )
    if args.protocol in {"https", "mtls"}:
        server["ssl"] = {
            "cert": args.server_ssl_cert,
            "key": args.server_ssl_key,
            "ca": args.server_ssl_ca,
            "check_hostname": args.server_ssl_check_hostname,
        }
    else:
        server.pop("ssl", None)
    config["server"] = server

    registration = dict(config.get("registration") or {})
    registration.update(
        {
            "enabled": args.registration_enabled,
            "protocol": args.registration_protocol,
            "register_url": args.registration_register_url,
            "unregister_url": args.registration_unregister_url,
            "heartbeat_interval": args.registration_heartbeat_interval,
            "server_id": args.registration_server_id,
            "server_name": args.registration_server_name,
            "instance_uuid": args.registration_instance_uuid,
            "auto_on_startup": args.registration_auto_on_startup,
            "auto_on_shutdown": args.registration_auto_on_shutdown,
            "ssl": {
                "ca": args.registration_ssl_ca,
                "crl": args.registration_ssl_crl,
                "dnscheck": args.registration_ssl_dnscheck,
                "check_hostname": args.registration_ssl_check_hostname,
            },
            "metadata": {
                "server_id": args.registration_server_id,
                "server_name": args.registration_server_name,
                "description": args.registration_description,
                "version": args.version,
            },
            "heartbeat": {
                "url": args.registration_heartbeat_url,
                "interval": args.registration_heartbeat_interval,
            },
        }
    )
    config["registration"] = registration
    config["queue_manager"] = {
        "enabled": args.queue_enabled,
        "in_memory": args.queue_in_memory,
        "shutdown_timeout": args.queue_shutdown_timeout,
        "max_concurrent_jobs": args.queue_max_concurrent_jobs,
        "completed_job_retention_seconds": args.queue_completed_job_retention_seconds,
    }
    config["embedding"] = {
        "protocol": args.embedding_protocol,
        "host": args.embedding_host,
        "port": args.embedding_port,
        "provider": args.embedding_provider,
        "model": args.embedding_model,
        "model_version": args.embedding_model_version,
        "dimension": args.embedding_dimension,
        "batch_size": args.embedding_batch_size,
        "timeout": args.embedding_timeout,
        "wait_timeout": args.embedding_wait_timeout,
        "poll_interval": args.embedding_poll_interval,
        "direct_text_max_chars": args.embedding_direct_text_max_chars,
        "check_hostname": args.embedding_check_hostname,
    }
    config["search"] = {
        "semantic_refinement": {
            "enabled": args.semantic_refinement_enabled,
            "threshold": args.semantic_refinement_threshold,
            "candidate_limit": args.semantic_refinement_candidate_limit,
            "result_limit": args.semantic_refinement_result_limit,
            "diagnostics": args.semantic_refinement_diagnostics,
        }
    }
    return config


def _validate_embedding_config(config: dict[str, Any]) -> None:
    section = config.get("embedding")
    if section is None:
        section = config.get("embedding_client")
    if section is None:
        raise ValueError("embedding config must define vectorizer client settings")
    if not isinstance(section, dict):
        raise ValueError("embedding config must be an object")
    protocol = section.get("protocol", DEFAULT_EMBEDDING_PROTOCOL)
    if protocol not in {"http", "https", "mtls"}:
        raise ValueError("embedding.protocol must be one of http, https, mtls")
    integer_defaults = {
        "port": DEFAULT_EMBEDDING_PORT,
        "dimension": DEFAULT_EMBEDDING_DIMENSION,
        "batch_size": DEFAULT_EMBEDDING_BATCH_SIZE,
        "wait_timeout": DEFAULT_EMBEDDING_WAIT_TIMEOUT,
        "direct_text_max_chars": DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS,
    }
    for key, default in integer_defaults.items():
        value = int(section.get(key, default))
        if key == "direct_text_max_chars":
            if value < 0:
                raise ValueError("embedding.direct_text_max_chars must be >= 0")
        elif value < 1:
            raise ValueError(f"embedding.{key} must be >= 1")
    float_defaults = {
        "timeout": DEFAULT_EMBEDDING_TIMEOUT,
        "poll_interval": DEFAULT_EMBEDDING_POLL_INTERVAL,
    }
    for key, default in float_defaults.items():
        if float(section.get(key, default)) <= 0:
            raise ValueError(f"embedding.{key} must be > 0")
    if not str(section.get("host", "")).strip():
        raise ValueError("embedding.host must not be empty")
    if not str(section.get("model", DEFAULT_EMBEDDING_MODEL)).strip():
        raise ValueError("embedding.model must not be empty")


def _add_bool_arg(parser: argparse.ArgumentParser, name: str, *, default: bool) -> None:
    dest = name.removeprefix("--").replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(name, dest=dest, action="store_true")
    group.add_argument(f"--no-{name.removeprefix('--')}", dest=dest, action="store_false")
    parser.set_defaults(**{dest: default})


def _format_issue(issue: ConfigIssue) -> str:
    location = ""
    if issue.section:
        location = f" ({issue.section}{'.' + issue.key if issue.key else ''})"
    return f"{issue.message}{location}"


def _package_version() -> str:
    try:
        return version("doc-store")
    except PackageNotFoundError:
        return "0.0.0"


__all__ = [
    "ConfigIssue",
    "generate_config",
    "generate_main",
    "print_validation_report",
    "validate_config_data",
    "validate_config_file",
    "validate_main",
    "validation_errors",
]
