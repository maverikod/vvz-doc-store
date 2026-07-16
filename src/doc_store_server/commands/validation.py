"""Shared public command validation helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mcp_proxy_adapter.core.errors import ValidationError


def parse_uuid4(value: Any, field: str, command_name: str) -> UUID:
    """Return a typed UUID only when the public field contains a UUIDv4."""

    try:
        identifier = value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError(
            f"{command_name}: parameter {field!r} must be a UUID4 identifier",
            data={"field": field, "value": value},
        ) from exc
    if identifier.version != 4:
        raise ValidationError(
            f"{command_name}: parameter {field!r} must be a UUID4 identifier",
            data={"field": field, "value": value},
        )
    return identifier


def parse_optional_uuid4(value: Any, field: str, command_name: str) -> UUID | None:
    """Return None for omitted values, otherwise parse a UUIDv4."""

    if value is None:
        return None
    return parse_uuid4(value, field, command_name)


__all__ = ["parse_optional_uuid4", "parse_uuid4"]
