"""Public API for the transport-neutral doc-store client package."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path as _Path
import tomllib as _tomllib

from .client import DocStoreClient, DocStoreClientError
from .models import (
    ChapterGetRequest,
    ChapterGetResult,
    DocumentCreateRequest,
    DocumentCreateResult,
    DocumentDeleteRequest,
    DocumentDeleteResult,
    DocumentGetRequest,
    DocumentGetResult,
    DocumentUpdateRequest,
    DocumentUpdateResult,
    DocumentWriteRequest,
    DocumentWriteResult,
    OperationState,
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
    "DocStoreClient",
    "DocStoreClientError",
    "DocumentCreateRequest",
    "DocumentCreateResult",
    "DocumentDeleteRequest",
    "DocumentDeleteResult",
    "DocumentGetRequest",
    "DocumentGetResult",
    "DocumentUpdateRequest",
    "DocumentUpdateResult",
    "DocumentWriteRequest",
    "DocumentWriteResult",
    "OperationState",
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
