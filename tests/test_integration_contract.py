"""Every route the integration calls exists in MAKIA's API description.

In the MAKIA repository it reads apps/audit-api/openapi.json; the published
example carries a copy as openapi.json at its root. MAKIA_OPENAPI overrides both
(point it at your instance's GET /openapi.json)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from makia_integration.api import ROUTES_USED

_HERE = Path(__file__).resolve()
IN_MONOREPO = _HERE.parents[3] / "apps" / "audit-api" / "openapi.json"
PUBLISHED_COPY = _HERE.parents[1] / "openapi.json"


def _spec() -> dict:
    if "MAKIA_OPENAPI" in os.environ:
        path = Path(os.environ["MAKIA_OPENAPI"])
    else:
        path = IN_MONOREPO if IN_MONOREPO.is_file() else PUBLISHED_COPY
    if not path.is_file():
        pytest.skip("no openapi.json: set MAKIA_OPENAPI")
    return json.loads(path.read_text())


@pytest.mark.parametrize(("method", "path"), ROUTES_USED)
def test_route_exists(method, path):
    paths = _spec()["paths"]
    assert path in paths, f"{path} is not in MAKIA's API"
    assert method.lower() in paths[path], f"{method} {path} is not in MAKIA's API"


def test_every_call_is_a_listed_route(integration, makia):
    """Drive every job once; each request the integration sent must match a route
    in ROUTES_USED, so the openapi check above covers all of them."""
    from fake_makia import row

    integration.sync()
    integration.send()
    integration.poll()
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    patterns = [
        (m, re.compile("^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(p)) + "$"))
        for m, p in ROUTES_USED
    ]
    stray = {
        (m, p) for m, p in makia.calls if not any(m == pm and rx.match(p) for pm, rx in patterns)
    }
    assert not stray, f"called but not in ROUTES_USED: {stray}"
    assert {m for m, _ in makia.calls} >= {"GET", "PUT", "POST"}
