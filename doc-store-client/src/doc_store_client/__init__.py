"""Public API for the transport-neutral doc-store client package."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path as _Path
import tomllib as _tomllib

from .client import DOC_STORE_COMMANDS, DocStoreClient, DocStoreClientError
from .models import (
    ChapterGetRequest,
    ChapterGetResult,
    DocumentChunkRequest,
    DocumentChunkResult,
    DocumentCreateRequest,
    DocumentCreateResult,
    DocumentDeleteRequest,
    DocumentDeleteResult,
    DocumentGetRequest,
    DocumentGetResult,
    DocumentRebindRequest,
    DocumentRebindResult,
    DocumentUpdateRequest,
    DocumentUpdateResult,
    DocumentWriteRequest,
    DocumentWriteResult,
    EntityGetRequest,
    EntityGetResult,
    EntityIdsRequest,
    EntityLifecycleResult,
    EntityListRequest,
    EntityListResult,
    EntityReferencesRequest,
    EntityReferencesResult,
    OperationState,
    ParagraphGetByNumberRequest,
    ParagraphGetByNumberResult,
    ParagraphGetRequest,
    ParagraphGetResult,
    ProcessingStatusRequest,
    ProcessingStatusResult,
    RankedSearchHit,
    RetrievalRequest,
    RetrievalResult,
    SearchResult,
    ServerError,
)

try:
    __version__ = _distribution_version("doc-store-client")
except _PackageNotFoundError:
    _pyproject = _Path(__file__).resolve().parents[2] / "pyproject.toml"
    __version__ = _tomllib.loads(_pyproject.read_text(encoding="utf-8"))["project"]["version"]

# The package includes ``py.typed`` in its distribution metadata.
__all__ = [
    "ChapterGetRequest",
    "ChapterGetResult",
    "DOC_STORE_COMMANDS",
    "DocStoreClient",
    "DocStoreClientError",
    "DocumentCreateRequest",
    "DocumentCreateResult",
    "DocumentChunkRequest",
    "DocumentChunkResult",
    "DocumentDeleteRequest",
    "DocumentDeleteResult",
    "DocumentGetRequest",
    "DocumentGetResult",
    "DocumentRebindRequest",
    "DocumentRebindResult",
    "DocumentUpdateRequest",
    "DocumentUpdateResult",
    "DocumentWriteRequest",
    "DocumentWriteResult",
    "EntityGetRequest",
    "EntityGetResult",
    "EntityIdsRequest",
    "EntityLifecycleResult",
    "EntityListRequest",
    "EntityListResult",
    "EntityReferencesRequest",
    "EntityReferencesResult",
    "OperationState",
    "ParagraphGetByNumberRequest",
    "ParagraphGetByNumberResult",
    "ParagraphGetRequest",
    "ParagraphGetResult",
    "ProcessingStatusRequest",
    "ProcessingStatusResult",
    "RankedSearchHit",
    "RetrievalRequest",
    "RetrievalResult",
    "SearchResult",
    "ServerError",
]
