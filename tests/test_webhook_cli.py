from __future__ import annotations

import pytest
from fake_makia import RUN_SECRET, SYNC_SECRET

from makia_integration import cli
from makia_integration.api import MakiaClient


@pytest.fixture
def run(makia, monkeypatch, capsys):
    monkeypatch.setenv("MAKIA_API_URL", "https://makia.example")
    monkeypatch.setenv("MAKIA_RUN_TOKEN", RUN_SECRET)
    transport = makia.transport()
    real = MakiaClient
    monkeypatch.setattr(
        cli, "MakiaClient", lambda url, secret: real(url, secret, transport=transport)
    )

    def go(*argv: str) -> str:
        cli.main(["webhook", *argv])
        return capsys.readouterr().out

    return go


CREATE = ["create", "--name", "integration", "--connection", "c-1", "--path", "/hook"]


def test_create_prints_secret_once_and_sends_filters(run, makia):
    out = run(
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


def test_create_without_filters_sends_none(run, makia):
    run(*CREATE)
    assert makia.webhook_bodies[0][1] == {
        "name": "integration",
        "connection_id": "c-1",
        "path": "/hook",
    }


def test_list(run):
    run(*CREATE, "--project", "p1")
    out = run("list")
    assert "wh-1" in out and "integration" in out and "enabled=true" in out
    assert "switched_off_reason=-" in out and '"projects": ["p1"]' in out and "last_seq=0" in out


def test_enable_disable_patch_bodies(run, makia):
    run(*CREATE)
    run("disable", "wh-1")
    run("enable", "wh-1")
    assert [b for m, b in makia.webhook_bodies if m == "PATCH"] == [
        {"enabled": False},
        {"enabled": True},
    ]
    assert makia.webhooks["wh-1"]["enabled"] is True


def test_rotate_and_delete(run, makia):
    run(*CREATE)
    assert "whsec_rotated" in run("rotate-secret", "wh-1")
    run("delete", "wh-1")
    assert makia.webhooks == {}


def test_deliveries_limit(run, makia):
    run(*CREATE)
    makia.deliveries["wh-1"] = [
        {
            "id": "d-1", "seq": 4, "event": "run.views_built", "run_id": None,
            "status": "failed", "attempts": 3, "last_status_code": 502,
            "last_error": "bad gateway", "next_attempt_at": None, "delivered_at": None,
            "created_at": "2026-11-12T10:00:00Z",
        }
    ]  # fmt: skip
    out = run("deliveries", "wh-1", "--limit", "5")
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
def test_api_error_exits_non_zero_with_code(run, argv, code):
    with pytest.raises(SystemExit) as exc:
        run(*argv)
    assert code in str(exc.value.code)


def test_sync_token_is_refused(run, monkeypatch):
    monkeypatch.setenv("MAKIA_RUN_TOKEN", SYNC_SECRET)
    with pytest.raises(SystemExit) as exc:
        run("list")
    assert "token_scope" in str(exc.value.code)
