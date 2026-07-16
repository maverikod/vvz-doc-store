"""Runtime embedding-service configuration shared by ingestion and search."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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


def runtime_embedding_config(config: Mapping[str, Any] | None = None) -> RuntimeEmbeddingConfig:
    """Resolve embedding-service settings from config and environment."""

    section = config.get("embedding") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = config.get("embedding_client") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}
    ssl = section.get("ssl") if isinstance(section.get("ssl"), Mapping) else {}
    return RuntimeEmbeddingConfig(
        protocol=os.getenv("DOC_STORE_EMBEDDING_PROTOCOL", str(section.get("protocol", "https"))),
        host=os.getenv("DOC_STORE_EMBEDDING_HOST", str(section.get("host", "192.168.254.26"))),
        port=int(os.getenv("DOC_STORE_EMBEDDING_PORT", str(section.get("port", 8001)))),
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
        timeout=float(os.getenv("DOC_STORE_EMBEDDING_TIMEOUT", str(section.get("timeout", 300.0)))),
        wait_timeout=int(
            os.getenv("DOC_STORE_EMBEDDING_WAIT_TIMEOUT", str(section.get("wait_timeout", 300)))
        ),
        poll_interval=float(
            os.getenv("DOC_STORE_EMBEDDING_POLL_INTERVAL", str(section.get("poll_interval", 1.0)))
        ),
        provider=os.getenv(
            "DOC_STORE_EMBEDDING_PROVIDER",
            str(section.get("provider", "embedding-service-vvz")),
        ),
        model=os.getenv(
            "DOC_STORE_EMBEDDING_MODEL",
            str(section.get("model", section.get("model_name", "all-MiniLM-L6-v2"))),
        ),
        model_version=os.getenv(
            "DOC_STORE_EMBEDDING_MODEL_VERSION",
            str(section.get("model_version", "4.0.2")),
        ),
        dimension=int(os.getenv("DOC_STORE_EMBEDDING_DIMENSION", str(section.get("dimension", 384)))),
        device=_optional(os.getenv("DOC_STORE_EMBEDDING_DEVICE", str(section.get("device", "")))),
        batch_size=int(
            os.getenv("DOC_STORE_EMBEDDING_BATCH_SIZE", str(section.get("batch_size", 16)))
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


__all__ = ["RuntimeEmbeddingConfig", "runtime_embedding_config"]
