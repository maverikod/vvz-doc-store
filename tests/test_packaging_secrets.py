"""The package must never ship a live secret, nor a generated file as config.

Two defects shared one root cause: ``install-package.sh`` shipped
``/var/doc-store/secrets/.env`` and ``/etc/doc-store/.env``, both of which are
generated per machine. Because the secrets file therefore always existed,
postinst's "generate on first install" branch never ran and the shipped
placeholder password stayed live at mode 0644 on the production host. Because
``/etc/doc-store/.env`` lived under ``/etc`` it was auto-marked a conffile while
postinst rewrote it, so every upgrade raised a prompt a non-interactive install
cannot answer -- after the stack had already been stopped.

These tests read the real packaging scripts. ``ensure_secrets`` is exercised by
sourcing it into a throwaway root, because its correctness is entirely about
which branch runs against which pre-existing state.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
POSTINST = REPO / "debian" / "postinst"
INSTALL_SCRIPT = REPO / "debian" / "install-package.sh"
TEMPLATE = REPO / "packaging" / "secrets.env.template"

PLACEHOLDER = "CHANGE_ME_APP"


def _function_source(script: Path, name: str) -> str:
    text = script.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"{script.name} defines no function {name}()")
    return match.group(0)


def _harness(root: Path) -> Path:
    """Write a sourceable file with the real functions bound to a fake root."""

    template_dir = root / "usr/share/doc/doc-store-server"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "secrets.env.template").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    body = "\n".join(
        [
            'DOC_STORE_USER="$(id -un)"',
            'DOC_STORE_GROUP="$(id -gn)"',
            f'DATA_DIR="{root}/var/doc-store"',
            f'SECRETS="{root}/var/doc-store/secrets"',
            _function_source(POSTINST, "cluster_is_initialised"),
            _function_source(POSTINST, "ensure_secrets"),
        ]
    )
    body = body.replace(
        "/usr/share/doc/doc-store-server/secrets.env.template",
        str(template_dir / "secrets.env.template"),
    )
    harness = root / "harness.sh"
    harness.write_text(body, encoding="utf-8")
    return harness


def _run_ensure_secrets(root: Path) -> subprocess.CompletedProcess[str]:
    harness = _harness(root)
    return subprocess.run(
        ["bash", "-c", f". {harness}; ensure_secrets"],
        capture_output=True,
        text=True,
        check=False,
    )


def _prepare(root: Path, *, cluster: bool, secrets: str | None, mode: int = 0o644) -> Path:
    secrets_dir = root / "var/doc-store/secrets"
    data_dir = root / "var/doc-store/postgres/data"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    if cluster:
        (data_dir / "PG_VERSION").write_text("16\n", encoding="utf-8")
    path = secrets_dir / ".env"
    if secrets is not None:
        path.write_text(secrets, encoding="utf-8")
        path.chmod(mode)
    return path


def test_the_package_ships_neither_generated_env_file() -> None:
    """The root cause: a generated file must not be packaged."""

    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    installs = [
        line
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
        and ("var/doc-store/secrets/.env" in line or "etc/doc-store/.env" in line)
    ]
    assert installs == [], installs
    assert not (REPO / "debian" / "conffiles").exists(), (
        "both hand-listed conffiles were generated files; /etc configuration "
        "keeps its conffile status from dh_installdeb"
    )
    # The template itself is still shipped, because postinst reads it from there.
    assert "usr/share/doc/doc-store-server" in script


def test_a_fresh_install_generates_a_real_password_at_0640(tmp_path: Path) -> None:
    path = _prepare(tmp_path, cluster=False, secrets=None)

    result = _run_ensure_secrets(tmp_path)

    assert result.returncode == 0, result.stderr
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert PLACEHOLDER not in content
    assert "POSTGRES_PASSWORD=" in content
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_an_upgrade_tightens_permissions_even_though_the_file_exists(
    tmp_path: Path,
) -> None:
    """The branch that never ran: the file exists, so nothing was enforced."""

    path = _prepare(
        tmp_path,
        cluster=True,
        secrets=TEMPLATE.read_text(encoding="utf-8"),
        mode=0o644,
    )

    result = _run_ensure_secrets(tmp_path)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_an_initialised_cluster_keeps_its_password_and_the_operator_is_told(
    tmp_path: Path,
) -> None:
    """Rewriting the file alone would lock the application out of its own database.

    PostgreSQL takes POSTGRES_PASSWORD on first initialisation and never again,
    so silently substituting here would leave the role on the old value.
    """

    path = _prepare(tmp_path, cluster=True, secrets=TEMPLATE.read_text(encoding="utf-8"))

    result = _run_ensure_secrets(tmp_path)

    assert result.returncode == 0
    assert PLACEHOLDER in path.read_text(encoding="utf-8")
    assert "WARNING" in result.stderr
    assert "ALTER ROLE" in result.stderr


def test_an_existing_real_password_is_never_touched(tmp_path: Path) -> None:
    """The inverse check: a healthy install must not be disturbed."""

    secrets = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, "a-real-password")
    path = _prepare(tmp_path, cluster=True, secrets=secrets, mode=0o640)

    result = _run_ensure_secrets(tmp_path)

    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == secrets
    assert "WARNING" not in result.stderr


@pytest.mark.parametrize("script", [POSTINST, INSTALL_SCRIPT])
def test_the_packaging_scripts_parse(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
