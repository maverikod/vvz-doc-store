from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]

LEGACY_QUERY_PATHS = (
    ROOT / "src/doc_store_server/query/__init__.py",
    ROOT / "src/doc_store_server/query/grammar.lark",
    ROOT / "src/doc_store_server/filters/__init__.py",
)

DOCUMENTATION_FILES = (
    ROOT / "README.md",
    ROOT / "docs/api/api_contract_draft.md",
    ROOT / "docs/architecture/overview.md",
    ROOT / "docs/architecture/product_context.md",
    ROOT / "docs/architecture/file_structure.md",
)

LEGACY_DOCUMENTATION_PATTERNS = (
    re.compile(r"\bquery[- ]language\b", re.IGNORECASE),
    re.compile(r"\bQuerySpec\b"),
    re.compile(r"\b(?:local )?query AST\b", re.IGNORECASE),
    re.compile(r"\bdocument AST\b", re.IGNORECASE),
    re.compile(r"POST\s+/api/v1/query", re.IGNORECASE),
    re.compile(r"(?:query|filter)(?:/|\\| package| module| directory)", re.IGNORECASE),
)


def test_legacy_query_package_paths_are_absent() -> None:
    assert all(not path.exists() for path in LEGACY_QUERY_PATHS)


def test_pyproject_has_no_lark_dependency_or_local_query_packaging_claim() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)

    dependencies = pyproject["project"].get("dependencies", [])
    optional_dependencies = pyproject["project"].get("optional-dependencies", {}).values()
    all_dependencies = [*dependencies, *(item for group in optional_dependencies for item in group)]

    assert all(not re.search(r"\blark\b", dependency, re.IGNORECASE) for dependency in all_dependencies)
    assert not re.search(r"\b(?:Lark|query[- ]language)\b", pyproject_text, re.IGNORECASE)


def test_bounded_documentation_has_no_legacy_query_surface() -> None:
    for path in DOCUMENTATION_FILES:
        text = path.read_text(encoding="utf-8")
        matches = [pattern.pattern for pattern in LEGACY_DOCUMENTATION_PATTERNS if pattern.search(text)]
        assert not matches, f"legacy query claims in {path.relative_to(ROOT)}: {matches}"
