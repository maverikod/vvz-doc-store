"""Runtime embedding-service configuration shared by ingestion and search."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_EMBEDDING_PROTOCOL = "https"
DEFAULT_EMBEDDING_PORT = 8001
DEFAULT_EMBEDDING_PROVIDER = "embedding-service-vvz"
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_MODEL_VERSION = "4.0.2"
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_EMBEDDING_TIMEOUT = 300.0
DEFAULT_EMBEDDING_WAIT_TIMEOUT = 300
DEFAULT_EMBEDDING_POLL_INTERVAL = 1.0
DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS = 0


@dataclass(frozen=True, slots=True)
class RuntimeEmbeddingConfig:
    """Effective embed-client connection and stored embedding metadata."""

    protocol: str
    host: str
    port: int
    cert: str | None
    key: str | None
    ca: str | None
    check_hostname: bool
    token: str | None
    token_header: str | None
    timeout: float
    wait_timeout: int
    poll_interval: float
    provider: str
    model: str
    model_version: str
    dimension: int
    device: str | None
    batch_size: int
    direct_text_max_chars: int


def runtime_embedding_config(config: Mapping[str, Any] | None = None) -> RuntimeEmbeddingConfig:
    """Resolve embedding-service settings from config and environment."""

    section = config.get("embedding") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = config.get("embedding_client") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}
    ssl = section.get("ssl") if isinstance(section.get("ssl"), Mapping) else {}
    return RuntimeEmbeddingConfig(
        protocol=os.getenv(
            "DOC_STORE_EMBEDDING_PROTOCOL",
            str(section.get("protocol", DEFAULT_EMBEDDING_PROTOCOL)),
        ),
        host=os.getenv("DOC_STORE_EMBEDDING_HOST", str(section.get("host", ""))),
        port=int(os.getenv("DOC_STORE_EMBEDDING_PORT", str(section.get("port", DEFAULT_EMBEDDING_PORT)))),
        cert=_optional(os.getenv("DOC_STORE_EMBEDDING_CERT", _config_text(ssl, section, "cert") or "")),
        key=_optional(os.getenv("DOC_STORE_EMBEDDING_KEY", _config_text(ssl, section, "key") or "")),
        ca=_optional(os.getenv("DOC_STORE_EMBEDDING_CA", _config_text(ssl, section, "ca") or "")),
        check_hostname=_bool(
            os.getenv("DOC_STORE_EMBEDDING_CHECK_HOSTNAME"),
            bool(ssl.get("check_hostname", section.get("check_hostname", False))),
        ),
        token=_optional(os.getenv("DOC_STORE_EMBEDDING_TOKEN", str(section.get("token", "")))),
        token_header=_optional(
            os.getenv("DOC_STORE_EMBEDDING_TOKEN_HEADER", str(section.get("token_header", "")))
        ),
        timeout=float(
            os.getenv(
                "DOC_STORE_EMBEDDING_TIMEOUT",
                str(section.get("timeout", DEFAULT_EMBEDDING_TIMEOUT)),
            )
        ),
        wait_timeout=int(
            os.getenv(
                "DOC_STORE_EMBEDDING_WAIT_TIMEOUT",
                str(section.get("wait_timeout", DEFAULT_EMBEDDING_WAIT_TIMEOUT)),
            )
        ),
        poll_interval=float(
            os.getenv(
                "DOC_STORE_EMBEDDING_POLL_INTERVAL",
                str(section.get("poll_interval", DEFAULT_EMBEDDING_POLL_INTERVAL)),
            )
        ),
        provider=os.getenv(
            "DOC_STORE_EMBEDDING_PROVIDER",
            str(section.get("provider", DEFAULT_EMBEDDING_PROVIDER)),
        ),
        model=os.getenv(
            "DOC_STORE_EMBEDDING_MODEL",
            str(section.get("model", section.get("model_name", DEFAULT_EMBEDDING_MODEL))),
        ),
        model_version=os.getenv(
            "DOC_STORE_EMBEDDING_MODEL_VERSION",
            str(section.get("model_version", DEFAULT_EMBEDDING_MODEL_VERSION)),
        ),
        dimension=int(
            os.getenv(
                "DOC_STORE_EMBEDDING_DIMENSION",
                str(section.get("dimension", DEFAULT_EMBEDDING_DIMENSION)),
            )
        ),
        device=_optional(os.getenv("DOC_STORE_EMBEDDING_DEVICE", str(section.get("device", "")))),
        batch_size=int(
            os.getenv(
                "DOC_STORE_EMBEDDING_BATCH_SIZE",
                str(section.get("batch_size", DEFAULT_EMBEDDING_BATCH_SIZE)),
            )
        ),
        direct_text_max_chars=int(
            os.getenv(
                "DOC_STORE_EMBEDDING_DIRECT_TEXT_MAX_CHARS",
                str(section.get("direct_text_max_chars", DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS)),
            )
        ),
    )


def _config_text(primary: Mapping[str, Any], fallback: Mapping[str, Any], key: str) -> str | None:
    value = primary.get(key)
    if value is None:
        value = fallback.get(key)
    return str(value) if value is not None else None


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "DEFAULT_EMBEDDING_BATCH_SIZE",
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_DIRECT_TEXT_MAX_CHARS",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_MODEL_VERSION",
    "DEFAULT_EMBEDDING_POLL_INTERVAL",
    "DEFAULT_EMBEDDING_PORT",
    "DEFAULT_EMBEDDING_PROTOCOL",
    "DEFAULT_EMBEDDING_PROVIDER",
    "DEFAULT_EMBEDDING_TIMEOUT",
    "DEFAULT_EMBEDDING_WAIT_TIMEOUT",
    "RuntimeEmbeddingConfig",
    "runtime_embedding_config",
]
