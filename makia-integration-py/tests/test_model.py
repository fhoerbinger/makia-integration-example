from __future__ import annotations

import dataclasses

import pytest
from integration_doubles import MemorySource, RecordingSink

from makia_integration import testing
from makia_integration.model import Project, ResultRow, Sink, Source


def test_result_row_from_api():
    r = ResultRow.from_api(testing.row("a", state="decided", option="ok"))
    assert r.unit_key == "a"
    assert r.row_key == "a"
    assert r.findings_open == 1
    assert (r.review_state, r.review_option, r.review_key) == ("decided", "ok", "a")
    assert r.url == "https://studio.example/runs/x?link=a"
    assert r.data == {"konto": "4711"}


def test_types_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Project("p", "n").name = "x"  # type: ignore[misc]


def test_fake_and_doubles_satisfy_protocols():
    assert isinstance(MemorySource(), Source)
    assert isinstance(RecordingSink(), Sink)
