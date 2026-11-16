from __future__ import annotations

import logging

import pytest

from makia_integration.integration import Integration, ConfigError, SyncError
from makia_integration.client import ApiError
from makia_integration.model import Member, Project
from makia_integration.testing import RUN_ACCOUNT_ID

RUN = {"service_account": RUN_ACCOUNT_ID, "permissions": ["cases", "run", "review"]}


def fill(source):
    source._projects = [
        Project("2026-114", "Engagement 2026-114"),
        Project("2025-087", "Old", True),
    ]
    source._members = [Member("2026-114", "uid=a.huber,ou=people", ("review", "cases"))]


def test_sync_puts_every_project_with_members_and_run_account(integration, source, makia):
    fill(source)
    report = integration.sync()
    p = makia.projects["2026-114"]
    assert p["name"] == "Engagement 2026-114" and p["archived"] is False
    assert RUN in p["members"]
    assert {"subject": "uid=a.huber,ou=people", "permissions": ["review", "cases"]} in p["members"]
    assert makia.projects["2025-087"]["archived"] is True
    assert (report.projects, report.conflicts, report.not_archived_absent) == (2, 0, [])


def test_sync_twice_changes_nothing(integration, source, makia):
    fill(source)
    integration.sync()
    before = dict(makia.projects)
    integration.sync()
    assert makia.projects == before


def test_run_account_permissions_configurable(source, sink, api, state, makia):
    fill(source)
    sync_api, run_api = api
    Integration(
        source,
        sink,
        sync_api=sync_api,
        run_api=run_api,
        state=state,
        run_account_permissions=("run",),
    ).sync()
    assert {"service_account": RUN_ACCOUNT_ID, "permissions": ["run"]} in makia.projects[
        "2026-114"
    ]["members"]


def test_member_naming_service_account_is_refused(integration, source, makia):
    fill(source)
    source._members.append(Member("2026-114", RUN_ACCOUNT_ID, ("run",)))
    with pytest.raises(ConfigError):
        integration.sync()
    assert makia.projects == {}


def test_absent_but_not_archived_is_reported(integration, source, makia, caplog):
    fill(source)
    integration.sync()
    source._projects = [p for p in source._projects if p.external_id != "2026-114"]
    with caplog.at_level(logging.WARNING, logger="makia_integration"):
        report = integration.sync()
    assert report.not_archived_absent == ["2026-114"]
    assert makia.projects["2026-114"]["archived"] is False
    assert "no longer in the source" in caplog.text


def test_conflicts_counted(integration, source, api):
    fill(source)
    sync_api, _ = api
    real = sync_api.put_project

    def put(*a, **k):
        answer = real(*a, **k)
        answer["members"]["conflicts"] = [{"subject": "x"}]
        return answer

    sync_api.put_project = put
    assert integration.sync().conflicts == 2


def _refuse(sync_api, external_id, status, code):
    real = sync_api.put_project

    def put(ext, **k):
        if ext == external_id:
            raise ApiError(status, code)
        return real(ext, **k)

    sync_api.put_project = put


def test_one_refused_project_does_not_stop_the_others(integration, source, makia, api, caplog):
    fill(source)
    _refuse(api[0], "2025-087", 409, "slug_taken")
    with caplog.at_level(logging.WARNING, logger="makia_integration"):
        with pytest.raises(SyncError) as err:
            integration.sync()
    assert "2026-114" in makia.projects  # sorted after 2025-087, still sent
    assert err.value.failed == ["2025-087"]
    assert "2025-087" in str(err.value) and "slug_taken" in str(err.value)
    assert "sync 2025-087: refused (409 slug_taken)" in caplog.text


def test_foreign_service_account_names_project_and_members(integration, source, makia, api, caplog):
    fill(source)
    _refuse(api[0], "2026-114", 400, "foreign_service_account")
    with caplog.at_level(logging.WARNING, logger="makia_integration"):
        with pytest.raises(SyncError) as err:
            integration.sync()
    assert err.value.failed == ["2026-114"]
    assert "2025-087" in makia.projects
    line = next(
        r.getMessage() for r in caplog.records if "foreign_service_account" in r.getMessage()
    )
    assert "2026-114" in line and "uid=a.huber,ou=people" in line and "service account" in line


@pytest.mark.parametrize("status", [401, 403, 500])
def test_auth_or_server_error_still_ends_sync(integration, source, api, status):
    fill(source)
    _refuse(api[0], "2025-087", status, "boom")
    with pytest.raises(ApiError):
        integration.sync()
