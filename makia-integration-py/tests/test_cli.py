from __future__ import annotations

import sys
import textwrap

import httpx
import pytest

from makia_integration import cli, config
from makia_integration.integration import ConfigError
from makia_integration.cli import main
from makia_integration.client import MakiaClient
from makia_integration.config import load_integration, load_config, read_env_file
from makia_integration.state import SqliteStateStore
from makia_integration.testing import RUN_SECRET, SYNC_SECRET

ENV_NAMES = (
    "MAKIA_API_URL",
    "MAKIA_SYNC_TOKEN",
    "MAKIA_RUN_TOKEN",
    "MAKIA_STORAGE_CONNECTION_ID",
    "MAKIA_FILES_DIR",
    "MAKIA_STATE_FILE",
    "MAKIA_CA_BUNDLE",
    "MAKIA_INTEGRATION",
    "MAKIA_WEBHOOK_SECRET",
)

INTEGRATION_SRC = """
    from integration_doubles import MemorySource, RecordingSink
    from makia_integration import Integration
    from makia_integration.model import Project

    def integration(config):
        sync_api, run_api = config.clients()
        return Integration(
            MemorySource(projects=[Project("p-1", "Alpha")]),
            RecordingSink(),
            sync_api=sync_api,
            run_api=run_api,
            state=config.open_state(),
            storage_connection_id=config.storage_connection_id,
        )
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKIA_API_URL", "https://makia.example")
    monkeypatch.setenv("MAKIA_SYNC_TOKEN", SYNC_SECRET)
    monkeypatch.setenv("MAKIA_RUN_TOKEN", RUN_SECRET)
    monkeypatch.setenv("MAKIA_STATE_FILE", str(tmp_path / "state.db"))
    monkeypatch.setenv("MAKIA_STORAGE_CONNECTION_ID", "conn-1")


@pytest.fixture
def wired(makia, monkeypatch):
    """Every MakiaClient the package builds talks to the fake."""
    real = MakiaClient

    def build(url, secret, **kw):
        return real(url, secret, transport=makia.transport(), sleep=lambda s: None, **kw)

    monkeypatch.setattr(config, "MakiaClient", build)
    return makia


@pytest.fixture
def integration_module(tmp_path, monkeypatch):
    (tmp_path / "myintegration.py").write_text(textwrap.dedent(INTEGRATION_SRC), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "myintegration", raising=False)
    yield "myintegration:integration"
    sys.modules.pop("myintegration", None)


# --- env file ---------------------------------------------------------------


def test_env_file_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(
        "﻿# a comment\n\nA=1\nexport B = two \nC='quoted value'\nD=\"x y\"\n"
        "E=https://h/?a=b=c\n  # indented comment\nF=\n".encode()
    )
    assert read_env_file(f) == {
        "A": "1",
        "B": "two",
        "C": "quoted value",
        "D": "x y",
        "E": "https://h/?a=b=c",
        "F": "",
    }


def test_env_file_malformed_line_names_the_line(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nnot a pair\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"line 2"):
        read_env_file(f)


def test_env_file_does_not_override_real_env(tmp_path, monkeypatch, capsys):
    f = tmp_path / ".env"
    f.write_text("MAKIA_WEBHOOK_SECRET=from-file\nMAKIA_API_URL=https://file.example\n")
    monkeypatch.setenv("MAKIA_API_URL", "https://real.example")
    seen = {}
    monkeypatch.setattr(cli, "_run_webhook", lambda cfg, args: seen.update(cfg=cfg) or 0)
    monkeypatch.setenv("MAKIA_RUN_TOKEN", "t")
    assert main(["--env-file", str(f), "webhook", "list"]) == 0
    assert seen["cfg"].api_url == "https://real.example"
    assert seen["cfg"].webhook_secret == "from-file"


def test_missing_env_file_exits_2(capsys):
    assert main(["--env-file", "nope.env", "version"]) == 2
    assert "nope.env" in capsys.readouterr().err


# --- config -----------------------------------------------------------------


def test_load_config_defaults_and_values(tmp_path):
    cfg = load_config(
        {
            "MAKIA_API_URL": "https://m.example",
            "MAKIA_SYNC_TOKEN": "s",
            "MAKIA_RUN_TOKEN": "r",
            "MAKIA_FILES_DIR": str(tmp_path),
        }
    )
    assert cfg.state_file.name == "makia-state.db"
    assert cfg.files_dir == tmp_path
    assert cfg.storage_connection_id == "" and cfg.integration_ref is None


def test_missing_variable_exits_2(monkeypatch, capsys, integration_module):
    monkeypatch.setenv("MAKIA_INTEGRATION", integration_module)
    assert main(["sync"]) == 2
    err = capsys.readouterr().err
    assert "MAKIA_API_URL" in err and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_integration_required_for_sync(env, capsys):
    assert main(["sync"]) == 2
    assert "MAKIA_INTEGRATION" in capsys.readouterr().err


def test_bad_integration_ref_exits_2(env, capsys):
    assert main(["--integration", "no_such_module_xyz:integration", "sync"]) == 2
    err = capsys.readouterr().err
    assert "no_such_module_xyz" in err and "Traceback" not in err
    assert main(["--integration", "nocolon", "sync"]) == 2


def test_load_integration_from_module(integration_module):
    factory = load_integration(integration_module)
    assert callable(factory)
    with pytest.raises(ConfigError, match="nothere"):
        load_integration("myintegration:nothere")


def test_load_integration_reads_current_directory(tmp_path, monkeypatch):
    (tmp_path / "cwdintegration.py").write_text("def integration(config):\n    return 1\n")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(tmp_path))])
    monkeypatch.delitem(sys.modules, "cwdintegration", raising=False)
    assert load_integration("cwdintegration:integration")(None) == 1  # type: ignore[arg-type]


# --- jobs -------------------------------------------------------------------


def test_sync_command_runs_integration(env, wired, integration_module, capsys):
    assert main(["--integration", integration_module, "sync"]) == 0
    assert "p-1" in wired.projects
    assert "sync" in capsys.readouterr().out
    # the state lock was released
    SqliteStateStore(config.load_config(dict(__import__("os").environ)).state_file).close()


def test_integration_from_env(env, wired, integration_module, monkeypatch):
    monkeypatch.setenv("MAKIA_INTEGRATION", integration_module)
    assert main(["sync"]) == 0


def test_state_locked_exits_2(env, wired, integration_module, capsys, tmp_path):
    held = SqliteStateStore(tmp_path / "state.db")
    try:
        assert main(["--integration", integration_module, "sync"]) == 2
    finally:
        held.close()
    err = capsys.readouterr().err
    assert "in use" in err and "Traceback" not in err


def test_unreachable_makia_exits_1_one_line(env, integration_module, monkeypatch, capsys):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    real = MakiaClient
    monkeypatch.setattr(
        config,
        "MakiaClient",
        lambda url, secret, **kw: real(
            url, secret, transport=httpx.MockTransport(refuse), sleep=lambda s: None, **kw
        ),
    )
    assert main(["--integration", integration_module, "sync"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    lines = err.strip().splitlines()
    assert len(lines) == 1 and "makia.example" in lines[0] and "MAKIA_CA_BUNDLE" in lines[0]


def test_verbose_adds_traceback(env, integration_module, monkeypatch, capsys):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    real = MakiaClient
    monkeypatch.setattr(
        config,
        "MakiaClient",
        lambda url, secret, **kw: real(
            url, secret, transport=httpx.MockTransport(refuse), sleep=lambda s: None, **kw
        ),
    )
    assert main(["-v", "--integration", integration_module, "sync"]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_job_runtime_error_exits_1(env, monkeypatch, tmp_path, capsys):
    (tmp_path / "badintegration.py").write_text(
        "def integration(config):\n    raise RuntimeError('source is down')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["--integration", "badintegration:integration", "poll"]) == 1
    assert "source is down" in capsys.readouterr().err


def test_log_file_is_written(env, wired, integration_module, tmp_path):
    log = tmp_path / "logs" / "m.log"
    log.parent.mkdir()
    assert main(["--log-file", str(log), "--integration", integration_module, "sync"]) == 0
    import logging

    for h in logging.getLogger().handlers:
        h.flush()
    assert log.exists()


# --- retry ------------------------------------------------------------------


def test_retry_clears_refusals(env, tmp_path, capsys, monkeypatch):
    SqliteStateStore(tmp_path / "state.db").close()
    calls = []
    real = SqliteStateStore.clear_refusals

    def spy(self, ext_id):
        calls.append(ext_id)
        return real(self, ext_id)

    monkeypatch.setattr(SqliteStateStore, "clear_refusals", spy)
    assert main(["retry", "rec-1"]) == 0
    assert main(["retry", "--all-refused"]) == 0
    assert calls == ["rec-1", None]
    assert "cleared 0" in capsys.readouterr().out


def test_retry_needs_target(env):
    with pytest.raises(SystemExit) as exc:
        main(["retry"])
    assert exc.value.code == 2


def test_retry_does_not_need_tokens_or_integration(monkeypatch, tmp_path):
    monkeypatch.setenv("MAKIA_STATE_FILE", str(tmp_path / "s.db"))
    SqliteStateStore(tmp_path / "s.db").close()
    assert main(["retry", "--all-refused"]) == 0


# --- run / listen -----------------------------------------------------------


class FakeRunner:
    instances: list[FakeRunner] = []
    error: BaseException | None = None
    on_join = None

    def __init__(self, factory, **kw):
        self.factory = factory
        self.kw = kw
        self.last_error = type(self).error
        self.stopped = 0
        type(self).instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped += 1

    def join(self, timeout=None):
        if type(self).on_join is not None:
            type(self).on_join()


@pytest.fixture
def fake_runner(monkeypatch):
    FakeRunner.instances = []
    FakeRunner.error = None
    FakeRunner.on_join = None
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    handlers = {}
    monkeypatch.setattr(cli.signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))
    monkeypatch.setattr(cli.signal, "getsignal", lambda sig: None)
    return handlers


def test_run_passes_options_and_installs_handlers(env, integration_module, fake_runner):
    assert (
        main(["--integration", integration_module, "run", "--interval", "5", "--sync-every", "7"])
        == 0
    )
    (r,) = FakeRunner.instances
    assert r.kw["interval"] == 5 and r.kw["sync_every"] == 7 and r.kw["listen_port"] is None
    import signal

    assert signal.SIGINT in fake_runner
    fake_runner[signal.SIGINT](signal.SIGINT, None)
    assert r.stopped == 1


def test_run_defaults(env, integration_module, fake_runner):
    main(["--integration", integration_module, "run"])
    (r,) = FakeRunner.instances
    assert r.kw["interval"] == 60 and r.kw["sync_every"] == 60


def test_run_exit_1_when_last_error(env, integration_module, fake_runner, capsys):
    FakeRunner.error = RuntimeError("sink down")
    assert main(["--integration", integration_module, "run"]) == 1
    assert "sink down" in capsys.readouterr().err


def test_run_listen_needs_secret(env, integration_module, fake_runner, capsys):
    assert main(["--integration", integration_module, "run", "--listen", "8080"]) == 2
    assert "MAKIA_WEBHOOK_SECRET" in capsys.readouterr().err


def test_listen_command(env, integration_module, fake_runner, monkeypatch):
    monkeypatch.setenv("MAKIA_WEBHOOK_SECRET", "s")
    assert main(["--integration", integration_module, "listen", "--port", "9000"]) == 0
    (r,) = FakeRunner.instances
    assert r.kw["listen_port"] == 9000 and r.kw["interval"] is None
    assert r.kw["webhook_secret"] == "s"


def test_run_state_locked_is_exit_2(env, wired, integration_module, tmp_path, capsys):
    held = SqliteStateStore(tmp_path / "state.db")
    try:
        assert main(["--integration", integration_module, "run", "--interval", "1"]) == 2
    finally:
        held.close()
    err = capsys.readouterr().err
    assert "in use" in err and "Traceback" not in err


# --- small commands ---------------------------------------------------------


def test_version_prints(capsys):
    from makia_integration import __version__

    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"makia-integration {__version__}"


def test_docs_prints_path(capsys):
    from pathlib import Path

    assert main(["docs"]) == 0
    out = Path(capsys.readouterr().out.strip())
    assert out.is_absolute() and out.name == "docs" and out.is_dir()


# --- webhook (moved from the example) ----------------------------------------


@pytest.fixture
def hook(makia, monkeypatch, capsys, env):
    monkeypatch.delenv("MAKIA_SYNC_TOKEN")  # webhook needs the run token only
    real = MakiaClient
    monkeypatch.setattr(
        config,
        "MakiaClient",
        lambda url, secret, **kw: real(
            url, secret, transport=makia.transport(), sleep=lambda s: None, **kw
        ),
    )

    def go(*argv: str) -> str:
        assert main(["webhook", *argv]) == 0
        return capsys.readouterr().out

    return go


CREATE = ["create", "--name", "integration", "--connection", "c-1", "--path", "/hook"]


def test_create_prints_secret_once_and_sends_filters(hook, makia):
    out = hook(
        *CREATE,
        "--project", "p1", "--project", "p2", "--workflow", "w1",
        "--blueprint", "b1", "--type", "run.views_built", "--type", "run.failed",
    )  # fmt: skip
    assert "wh-1" in out and "whsec_new_1" in out and "MAKIA_WEBHOOK_SECRET" in out
    assert makia.webhook_bodies == [
        (
            "POST",
            {
                "name": "integration",
                "connection_id": "c-1",
                "path": "/hook",
                "projects": ["p1", "p2"],
                "workflows": ["w1"],
                "blueprints": ["b1"],
                "types": ["run.views_built", "run.failed"],
            },
        )
    ]


def test_create_without_filters_sends_none(hook, makia):
    hook(*CREATE)
    assert makia.webhook_bodies[0][1] == {
        "name": "integration",
        "connection_id": "c-1",
        "path": "/hook",
    }


def test_list(hook):
    hook(*CREATE, "--project", "p1")
    out = hook("list")
    assert "wh-1" in out and "integration" in out and "enabled=true" in out
    assert "switched_off_reason=-" in out and '"projects": ["p1"]' in out and "last_seq=0" in out


def test_enable_disable_patch_bodies(hook, makia):
    hook(*CREATE)
    hook("disable", "wh-1")
    hook("enable", "wh-1")
    assert [b for m, b in makia.webhook_bodies if m == "PATCH"] == [
        {"enabled": False},
        {"enabled": True},
    ]
    assert makia.webhooks["wh-1"]["enabled"] is True


def test_rotate_and_delete(hook, makia):
    hook(*CREATE)
    assert "whsec_rotated" in hook("rotate-secret", "wh-1")
    hook("delete", "wh-1")
    assert makia.webhooks == {}


def test_deliveries_limit(hook, makia):
    hook(*CREATE)
    makia.deliveries["wh-1"] = [
        {
            "id": "d-1", "seq": 4, "event": "run.views_built", "run_id": None,
            "status": "failed", "attempts": 3, "last_status_code": 502,
            "last_error": "bad gateway", "next_attempt_at": None, "delivered_at": None,
            "created_at": "2026-11-12T10:00:00Z",
        }
    ]  # fmt: skip
    out = hook("deliveries", "wh-1", "--limit", "5")
    assert "d-1" in out and "failed" in out and "502" in out
    assert makia.delivery_limits == ["5"]


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["delete", "nope"], "webhook_not_found"),
        (
            ["create", "--name", "x", "--connection", "missing", "--path", "/h"],
            "connection_not_found",
        ),
    ],
)
def test_api_error_exits_1_with_code(hook, capsys, argv, code):
    assert main(["webhook", *argv]) == 1
    err = capsys.readouterr().err
    assert code in err and "Traceback" not in err


def test_sync_token_is_refused(hook, monkeypatch, capsys):
    monkeypatch.setenv("MAKIA_RUN_TOKEN", SYNC_SECRET)
    assert main(["webhook", "list"]) == 1
    assert "token_scope" in capsys.readouterr().err


# --- review fixes ------------------------------------------------------------


def test_config_repr_hides_secrets():
    cfg = load_config(
        {
            "MAKIA_API_URL": "https://m.example",
            "MAKIA_SYNC_TOKEN": "sync-secret-1",
            "MAKIA_RUN_TOKEN": "run-secret-2",
            "MAKIA_WEBHOOK_SECRET": "hook-secret-3",
        }
    )
    text = repr(cfg)
    assert not any(s in text for s in ("sync-secret-1", "run-secret-2", "hook-secret-3"))


def test_port_in_use_exits_2_one_line(env, wired, integration_module, monkeypatch, capsys):
    import socket

    monkeypatch.setenv("MAKIA_WEBHOOK_SECRET", "s")
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert main(["--integration", integration_module, "listen", "--port", str(port)]) == 2
    err = capsys.readouterr().err
    assert str(port) in err and "Traceback" not in err


def test_unexpected_job_error_exits_1_one_line(env, monkeypatch, tmp_path, capsys):
    (tmp_path / "valintegration.py").write_text(
        "def integration(config):\n    raise ValueError('bad row 7')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["--integration", "valintegration:integration", "poll"]) == 1
    err = capsys.readouterr().err
    assert "poll failed: ValueError: bad row 7" in err and "Traceback" not in err


def test_runner_last_error_unexpected_type_exits_1(env, integration_module, fake_runner, capsys):
    FakeRunner.error = ValueError("odd")
    assert main(["--integration", integration_module, "run"]) == 1
    assert "run failed: ValueError: odd" in capsys.readouterr().err


def test_integration_module_with_syntax_error_exits_2(env, monkeypatch, tmp_path, capsys):
    (tmp_path / "brokenintegration.py").write_text("def integration(:\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["--integration", "brokenintegration:integration", "sync"]) == 2
    err = capsys.readouterr().err
    assert "brokenintegration" in err and "Traceback" not in err


def test_fatal_factory_error_reaches_log_file(env, wired, integration_module, tmp_path):
    held = SqliteStateStore(tmp_path / "state.db")
    log = tmp_path / "m.log"
    try:
        assert main(["--log-file", str(log), "--integration", integration_module, "run"]) == 2
    finally:
        held.close()
    import logging

    for h in logging.getLogger().handlers:
        h.flush()
    text = log.read_text(encoding="utf-8")
    assert "in use" in text and "Traceback" not in text


def test_empty_real_env_var_does_not_override_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("MAKIA_API_URL=https://file.example\n")
    monkeypatch.setenv("MAKIA_API_URL", "")
    monkeypatch.setenv("MAKIA_RUN_TOKEN", "t")
    seen = {}
    monkeypatch.setattr(cli, "_run_webhook", lambda cfg, args: seen.update(cfg=cfg) or 0)
    assert main(["--env-file", str(f), "webhook", "list"]) == 0
    assert seen["cfg"].api_url == "https://file.example"


def test_retry_missing_state_file_exits_2(monkeypatch, tmp_path, capsys):
    target = tmp_path / "nope.db"
    monkeypatch.setenv("MAKIA_STATE_FILE", str(target))
    assert main(["retry", "--all-refused"]) == 2
    assert not target.exists()
    assert "nope.db" in capsys.readouterr().err


# --- final-review fixes -------------------------------------------------------


def test_signal_stop_after_a_failed_round_exits_0(env, integration_module, fake_runner, capsys):
    import signal

    FakeRunner.error = RuntimeError("sink down")
    FakeRunner.on_join = lambda: fake_runner[signal.SIGINT](signal.SIGINT, None)
    assert main(["--integration", integration_module, "run"]) == 0
    assert "sink down" not in capsys.readouterr().err


def test_run_passes_a_describe_that_explains(env, integration_module, fake_runner):
    from makia_integration.client import TransportFailed

    main(["--integration", integration_module, "run"])
    (r,) = FakeRunner.instances
    assert "cannot reach MAKIA at makia.example" in r.kw["describe"](TransportFailed("x"))
    assert r.kw["describe"](ValueError("odd")) == "ValueError: odd"


def test_sync_error_is_one_line_exit_1():
    from makia_integration.integration import SyncError, SyncReport

    err = SyncError(["a"], ["a (409 slug_taken)"], SyncReport(2, 0))
    assert cli._explain(err, {}, "sync") == (
        "sync failed: 1 of 2 projects refused: a (409 slug_taken)",
        1,
    )


@pytest.mark.parametrize("command", ["send", "run", "listen"])
def test_sending_commands_need_exactly_one_send_mode(
    env, integration_module, fake_runner, monkeypatch, capsys, command, tmp_path
):
    monkeypatch.setenv("MAKIA_WEBHOOK_SECRET", "s")
    monkeypatch.delenv("MAKIA_STORAGE_CONNECTION_ID")
    assert main(["--integration", integration_module, command]) == 2
    assert "set MAKIA_STORAGE_CONNECTION_ID or MAKIA_FILES_DIR" in capsys.readouterr().err
    monkeypatch.setenv("MAKIA_STORAGE_CONNECTION_ID", "conn-1")
    monkeypatch.setenv("MAKIA_FILES_DIR", str(tmp_path))
    assert main(["--integration", integration_module, command]) == 2
    assert "not both" in capsys.readouterr().err


def test_sync_needs_no_send_mode(env, wired, integration_module, monkeypatch):
    monkeypatch.delenv("MAKIA_STORAGE_CONNECTION_ID")
    assert main(["--integration", integration_module, "sync"]) == 0


def test_missing_ca_bundle_names_the_path_exit_2(
    env, integration_module, monkeypatch, capsys, tmp_path
):
    missing = tmp_path / "company-ca.pem"
    monkeypatch.setenv("MAKIA_CA_BUNDLE", str(missing))
    assert main(["--integration", integration_module, "sync"]) == 2
    err = capsys.readouterr().err
    assert str(missing) in err and "MAKIA_CA_BUNDLE" in err and "Errno" not in err


def test_ca_bundle_that_is_not_pem_exit_2(env, integration_module, monkeypatch, capsys, tmp_path):
    bad = tmp_path / "ca.pem"
    bad.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("MAKIA_CA_BUNDLE", str(bad))
    assert main(["--integration", integration_module, "sync"]) == 2
    assert str(bad) in capsys.readouterr().err


def test_one_shot_closes_the_integration_source_and_sink(env, wired, tmp_path, monkeypatch):
    (tmp_path / "closing.py").write_text(
        textwrap.dedent(
            """
            from integration_doubles import MemorySource, RecordingSink
            from makia_integration import Integration

            CLOSED = []

            class Sink(RecordingSink):
                def close(self):
                    CLOSED.append("sink")

            def integration(config):
                sync_api, run_api = config.clients()
                return Integration(MemorySource(), Sink(), sync_api=sync_api, run_api=run_api,
                              state=config.open_state(),
                              storage_connection_id=config.storage_connection_id)
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "closing", raising=False)
    assert main(["--integration", "closing:integration", "send"]) == 0
    assert sys.modules["closing"].CLOSED == ["sink"]
