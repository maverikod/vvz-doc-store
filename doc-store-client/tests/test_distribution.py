"""Distribution-level checks for the independently installable client."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile


CLIENT_ROOT = Path(__file__).parents[1]
SERVER_MODULE = "doc_store_server"
EXPECTED_VERSION = tomllib.loads((CLIENT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _build(output_dir: Path) -> None:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = "946684800"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(output_dir)],
        cwd=CLIENT_ROOT,
        check=True,
        text=True,
        capture_output=True,
        env=environment,
    )


def _digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def _archive_content_digests(archive_path: Path) -> dict[str, str]:
    if archive_path.suffix == ".whl":
        with zipfile.ZipFile(archive_path) as archive:
            return {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in sorted(archive.namelist())
            }
    with tarfile.open(archive_path, "r:gz") as archive:
        result: dict[str, str] = {}
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                continue
            file_obj = archive.extractfile(member)
            assert file_obj is not None
            result[member.name] = hashlib.sha256(file_obj.read()).hexdigest()
        return result


def _wheel_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def _sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        return archive.getnames()


def _wheel_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        )
        return archive.read(metadata).decode("utf-8")


def _venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def test_client_distribution_isolated_and_server_free(tmp_path: Path) -> None:
    """Build, inspect, install, and exercise only the published client surface."""
    first = tmp_path / "build-one"
    second = tmp_path / "build-two"
    first.mkdir()
    second.mkdir()
    _build(first)
    _build(second)

    wheels = sorted(first.glob("*.whl"))
    sdists = sorted(first.glob("*.tar.gz"))
    second_wheels = sorted(second.glob("*.whl"))
    second_sdists = sorted(second.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    assert len(second_wheels) == 1
    assert len(second_sdists) == 1
    wheel = wheels[0]
    sdist = sdists[0]
    assert _digests(first)[wheel.name] == _digests(second)[second_wheels[0].name]
    assert _archive_content_digests(sdist) == _archive_content_digests(second_sdists[0])

    wheel_members = _wheel_members(wheel)
    sdist_members = _sdist_members(sdist)
    assert "doc_store_client/py.typed" in wheel_members
    assert any(name.endswith("/src/doc_store_client/py.typed") for name in sdist_members)
    assert not any(name.startswith(f"doc_store_client/{SERVER_MODULE}") for name in wheel_members)
    assert not any(f"/{SERVER_MODULE}/" in name for name in sdist_members)
    assert not any("server" in name.lower() for name in wheel_members if "doc_store_client" in name)

    metadata = _wheel_metadata(wheel)
    requirement_names = {
        match.group(1).lower().replace("_", "-")
        for match in re.finditer(r"^Requires-Dist: ([A-Za-z0-9_.-]+)(.*)$", metadata, re.MULTILINE)
        if "extra ==" not in match.group(2)
    }
    assert requirement_names == {"chunk-metadata-adapter", "mcp-proxy-adapter"}
    assert SERVER_MODULE not in metadata
    assert "doc-store-server" not in metadata.lower()

    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = _venv_python(environment)
    _run([str(python), "-m", "pip", "install", str(wheel)], cwd=tmp_path)

    smoke = f"""
import importlib
import importlib.metadata
import importlib.util
import json
import os
import sys

assert not any(os.path.abspath(path) == os.getcwd() for path in sys.path if path)
package = importlib.import_module("doc_store_client")
assert package.__version__ == importlib.metadata.version("doc-store-client")
assert package.__version__ == "{EXPECTED_VERSION}"
public = getattr(package, "__all__", ())
assert public
assert "DocStoreClient" in public
for name in public:
    assert getattr(package, name)
    assert os.path.exists(os.path.join(os.path.dirname(package.__file__), "py.typed"))
assert importlib.util.find_spec("doc_store_server") is None
assert not any(name.startswith("doc_store_server") for name in sys.modules)

class FakeAdapter:
    pass

client_type = package.DocStoreClient
client = client_type(FakeAdapter())
assert isinstance(client, client_type)
print(json.dumps({{"version": package.__version__, "exports": sorted(public)}}))
"""
    result = _run(
        [str(python), "-I", "-c", smoke],
        cwd=tmp_path,
    )
    report = json.loads(result.stdout)
    assert report["version"] == EXPECTED_VERSION
    assert "DocStoreClient" in report["exports"]
