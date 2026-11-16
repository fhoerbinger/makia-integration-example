from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from makia_integration import FINAL_REFUSALS, FIXABLE_REFUSALS
from makia_integration.cli import main
from makia_integration.scaffold import new_integration

PACKAGE = Path(__file__).resolve().parents[1]
DOCS = PACKAGE / "makia_integration" / "docs"
EXPECTED = {
    "integration.py",
    "test_integration.py",
    ".env.example",
    "makia-integration.service",
    "README.md",
}


def test_new_writes_template(tmp_path):
    target = tmp_path / "my-integration"
    written = new_integration(target)
    assert {p.name for p in written} == EXPECTED
    assert {p.name for p in target.iterdir()} == EXPECTED
    text = (target / "integration.py").read_text(encoding="utf-8")
    assert "class DbSource" in text and "class DbSink" in text and "# TODO" in text
    assert b"\r" not in (target / "integration.py").read_bytes()
    env = (target / ".env.example").read_text(encoding="utf-8")
    for name in (
        "MAKIA_API_URL",
        "MAKIA_SYNC_TOKEN",
        "MAKIA_RUN_TOKEN",
        "MAKIA_STORAGE_CONNECTION_ID",
        "MAKIA_FILES_DIR",
        "MAKIA_STATE_FILE",
        "MAKIA_CA_BUNDLE",
        "MAKIA_INTEGRATION",
        "MAKIA_WEBHOOK_SECRET",
    ):
        assert name in env
    unit = (target / "makia-integration.service").read_text(encoding="utf-8")
    (exec_start,) = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert exec_start.endswith("integration.log run")  # webhooks (--listen) are a later step


def test_new_into_an_existing_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    assert len(new_integration(tmp_path / "empty")) == len(EXPECTED)


def test_new_refuses_non_empty_dir(tmp_path):
    (tmp_path / "x.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        new_integration(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["x.txt"]


def test_cli_new(tmp_path, capsys):
    assert main(["new", str(tmp_path / "b")]) == 0
    assert (tmp_path / "b" / "integration.py").is_file()
    assert main(["new", str(tmp_path / "b")]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "not empty" in err


def test_generated_integration_test_passes(tmp_path):
    target = tmp_path / "generated"
    new_integration(target)
    before = set(sys.modules)
    try:
        code = pytest.main(
            [
                str(target / "test_integration.py"),
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(target),
                "-o",
                "addopts=",
                "-o",
                "asyncio_default_fixture_loop_scope=function",
            ]
        )
    finally:
        for name in set(sys.modules) - before:
            if name in ("integration", "test_integration"):
                del sys.modules[name]
    assert code == 0


def test_guide_covers_every_refusal_code():
    guide = (DOCS / "guide.md").read_text(encoding="utf-8")
    for code in FIXABLE_REFUSALS + FINAL_REFUSALS:
        assert code in guide, code


def test_guide_has_the_sections():
    guide = (DOCS / "guide.md").read_text(encoding="utf-8")
    headings = [line[3:].strip() for line in guide.splitlines() if line.startswith("## ")]
    assert headings == [
        "Install",
        "The five-minute path",
        "Source and Sink",
        "The state file",
        "Refusals",
        "TLS and proxy",
        "Running as a service",
        "Troubleshooting",
    ]


def test_docs_ship_in_wheel():
    # The wheel itself is built in the final task; here only the configuration is checked.
    config = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "makia_integration" in wheel["packages"]
    assert "makia_integration/**" in wheel.get("include", ["makia_integration/**"])
    assert config["tool"]["hatch"]["build"]["hooks"]["custom"]["path"] == "hatch_build.py"
    assert (PACKAGE / "hatch_build.py").is_file()
    assert "makia_integration/docs/openapi.json" in (PACKAGE / ".gitignore").read_text()
    template = DOCS / "template"
    for name in (
        "integration.py.tmpl",
        "test_integration.py.tmpl",
        "env.example.tmpl",
        "makia-integration.service.tmpl",
        "README.md.tmpl",
    ):
        assert (template / name).is_file(), name
