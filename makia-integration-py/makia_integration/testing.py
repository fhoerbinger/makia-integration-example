"""A tiny in-memory stand-in for MAKIA's API, enough to drive the integration in
tests. Its answers follow the shapes in MAKIA's openapi.json."""

from __future__ import annotations

import json
import re
from email.parser import BytesParser
from email.policy import HTTP
from dataclasses import dataclass, field
from typing import Any

import httpx

SYNC_SECRET = "makia_sync_secret"
RUN_SECRET = "makia_run_secret"
RUN_ACCOUNT_ID = "00000000-0000-4000-8000-0000000000aa"


@dataclass
class FakeMakia:
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)  # by external_id
    changes: list[dict[str, Any]] = field(default_factory=list)
    results: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )  # (run, view) -> {generation, rows}
    progress: dict[str, dict[str, Any]] = field(default_factory=dict)  # by run id
    webhooks: dict[str, dict[str, Any]] = field(default_factory=dict)
    webhook_bodies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (method, body)
    deliveries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    delivery_limits: list[str | None] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    uploads: list[list[tuple[str, str, bytes]]] = field(
        default_factory=list
    )  # multipart parts per request: (field, filename, bytes)
    inbound_status: int | None = None  # answer every inbound call with this error status
    inbound_code: str = ""
    swaps: int = 0
    expire_next: bool = False  # answer the next authenticated call 401 once
    rebuild_once: set[tuple[str, str]] = field(
        default_factory=set
    )  # 409 generation_changed on page 2 once
    deleted_runs: set[str] = field(default_factory=set)  # 404 run_not_found everywhere
    deleted_views: set[tuple[str, str]] = field(default_factory=set)  # 404 view_not_found
    _tokens: dict[str, str] = field(default_factory=dict)  # access token -> secret

    # --- helpers for tests --------------------------------------------------

    def emit(self, kind: str, **ref: Any) -> None:
        self.changes.append(
            {
                "seq": len(self.changes) + 1,
                "type": kind,
                "at": "2026-11-12T10:00:00Z",
                "project_id": None,
                "workflow_id": None,
                "case_id": ref.get("case_id"),
                "run_id": ref.get("run_id"),
                "ref": ref,
            }
        )

    def set_rows(
        self, run_id: str, view: str, rows: list[dict[str, Any]], generation: str = "g1"
    ) -> None:
        self.results[(run_id, view)] = {"generation": generation, "rows": rows}

    def delete_run(self, run_id: str) -> None:
        """The run is gone (deleted in Studio): its routes answer 404 run_not_found, and
        an inbound call for its record answers 200 with `run_id` null."""
        self.deleted_runs.add(run_id)
        self.progress.pop(run_id, None)
        for key in [k for k in self.results if k[0] == run_id]:
            del self.results[key]
        for answer in self.cases.values():
            if answer.get("run_id") == run_id:
                answer.update(run_id=None, status_url=None, review_url=None)

    def delete_view(self, run_id: str, view: str) -> None:
        """The view is no longer declared: reading it answers 404 view_not_found."""
        self.deleted_views.add((run_id, view))
        self.results.pop((run_id, view), None)

    def _run_known(self, run_id: str) -> bool:
        if run_id in self.deleted_runs:
            return False
        return run_id in self.progress or any(
            a.get("run_id") == run_id for a in self.cases.values()
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- the API ------------------------------------------------------------

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/auth/token":
            secret = json.loads(req.content)["token"]
            if secret not in (SYNC_SECRET, RUN_SECRET):
                return _err(401, "invalid_token")
            self.swaps += 1
            access = f"jwt-{self.swaps}"
            self._tokens[access] = secret
            return httpx.Response(
                200, json={"access_token": access, "token_type": "Bearer", "expires_in": 900}
            )

        bearer = req.headers.get("authorization", "").removeprefix("Bearer ")
        if path.startswith("/inbound/"):
            if bearer != RUN_SECRET:
                return _err(403, "token_scope" if bearer == SYNC_SECRET else "invalid_token")
            return self._inbound(req)

        if self.expire_next:
            self.expire_next = False
            return _err(401, "invalid_token")
        secret = self._tokens.get(bearer)
        if secret is None:
            return _err(401, "invalid_token")
        if path.startswith("/sync/") != (secret == SYNC_SECRET):
            return _err(403, "token_scope")

        if path.startswith("/webhooks"):
            if secret != RUN_SECRET:
                return _err(403, "token_scope")
            return self._webhooks(req)

        if path == "/me":
            return httpx.Response(
                200, json={"userId": RUN_ACCOUNT_ID, "actorKind": "system", "projects": []}
            )
        if m := re.fullmatch(r"/sync/projects/([^/]+)", path):
            return self._put_project(m.group(1), json.loads(req.content))
        if path == "/sync/projects":
            return httpx.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": f"p-{k}",
                            "external_id": k,
                            "slug": k,
                            "name": v["name"],
                            "archived": v["archived"],
                            "members": len(v["members"]),
                            "pending": 0,
                        }
                        for k, v in self.projects.items()
                    ]
                },
            )
        if path == "/changes/head":
            return httpx.Response(200, json={"head": len(self.changes)})
        if path == "/changes":
            after, limit = int(req.url.params["after"]), int(req.url.params["limit"])
            page = [c for c in self.changes if c["seq"] > after][:limit]
            nxt = page[-1]["seq"] if page else after
            return httpx.Response(
                200, json={"changes": page, "next": nxt, "more": nxt < len(self.changes)}
            )
        if m := re.fullmatch(r"/runs/([^/]+)/progress", path):
            if not self._run_known(m.group(1)):
                return _err(404, "run_not_found")
            # A run with no snapshot set is queued: nothing stored yet.
            return httpx.Response(200, json=self.progress.get(m.group(1)) or _queued(m.group(1)))
        if m := re.fullmatch(r"/runs/([^/]+)/results/([^/]+)", path):
            return self._results(m.group(1), m.group(2), req.url.params)
        if m := re.fullmatch(r"/runs/([^/]+)/views", path):
            if m.group(1) in self.deleted_runs:
                return _err(404, "run_not_found")
            built = sorted(v for r, v in self.results if r == m.group(1))
            views = [
                {"view": v, "title": None, "built": True, "backing": "rows", "builtAt": "t1"}
                for v in built
            ] + [
                {
                    "view": "declared-only",
                    "title": None,
                    "built": False,
                    "backing": None,
                    "builtAt": None,
                }
            ]
            return httpx.Response(200, json={"views": views})
        return _err(404, "not_found")

    def _webhooks(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if req.method in ("POST", "PATCH"):
            self.webhook_bodies.append((req.method, body))
        if path == "/webhooks" and req.method == "POST":
            if body["connection_id"] == "missing":
                return _err(400, "connection_not_found")
            n = len(self.webhooks) + 1
            hook = {
                "id": f"wh-{n}",
                "name": body["name"],
                "connection_id": body["connection_id"],
                "path": body["path"],
                "projects": body.get("projects", []),
                "workflows": body.get("workflows", []),
                "blueprints": body.get("blueprints", []),
                "types": body.get("types", []),
                "enabled": True,
                "switched_off_reason": None,
                "last_seq": 0,
                "created_at": "2026-11-12T10:00:00Z",
                "updated_at": "2026-11-12T10:00:00Z",
            }
            self.webhooks[hook["id"]] = hook
            return httpx.Response(201, json={"webhook": hook, "secret": f"whsec_new_{n}"})
        if path == "/webhooks":
            return httpx.Response(200, json={"webhooks": list(self.webhooks.values())})
        m = re.fullmatch(r"/webhooks/([^/]+)(/rotate-secret|/deliveries)?", path)
        if not m or m.group(1) not in self.webhooks:
            return _err(404, "webhook_not_found")
        hook = self.webhooks[m.group(1)]
        if m.group(2) == "/rotate-secret":
            return httpx.Response(200, json={"secret": "whsec_rotated"})
        if m.group(2) == "/deliveries":
            self.delivery_limits.append(req.url.params.get("limit"))
            return httpx.Response(200, json={"deliveries": self.deliveries.get(hook["id"], [])})
        if req.method == "DELETE":
            del self.webhooks[hook["id"]]
            return httpx.Response(204)
        hook.update(body)
        return httpx.Response(200, json={"webhook": hook})

    def _put_project(self, external_id: str, body: dict[str, Any]) -> httpx.Response:
        before = self.projects.get(external_id)
        self.projects[external_id] = body
        outcome = "created" if before is None else ("unchanged" if before == body else "updated")
        added = [] if before else [{"user_id": "u", **m} for m in body["members"]]
        return httpx.Response(
            200,
            json={
                "project_id": f"p-{external_id}",
                "slug": external_id,
                "outcome": outcome,
                "members": {
                    "added": added,
                    "changed": [],
                    "removed": [],
                    "pending": [],
                    "conflicts": [],
                },
            },
        )

    def _inbound(self, req: httpx.Request) -> httpx.Response:
        project = req.url.path.split("/")[3]
        if self.projects.get(project, {}).get("archived"):
            return _err(409, "project_archived")
        if self.inbound_status:
            return _err(self.inbound_status, self.inbound_code)
        ctype = req.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req.content
            )
            parts = [
                (
                    p.get_param("name", header="content-disposition"),
                    p.get_filename() or "",
                    p.get_payload(decode=True),
                )
                for p in msg.iter_parts()
            ]
            self.uploads.append(parts)
            body = {"external_id": parts[0][2].decode()}
        else:
            body = json.loads(req.content)
        known = self.cases.get(body["external_id"])
        if known:
            return httpx.Response(200, json=known)
        n = len(self.cases) + 1
        answer = {
            "case_id": f"case-{n}",
            "run_id": f"run-{n}",
            "status_url": f"/runs/run-{n}",
            "review_url": f"https://studio.example/runs/run-{n}",
            "import_id": f"imp-{n}",
        }
        self.cases[body["external_id"]] = answer
        return httpx.Response(202, json=answer)

    def _results(self, run_id: str, view: str, params: httpx.QueryParams) -> httpx.Response:
        if run_id in self.deleted_runs:
            return _err(404, "run_not_found")
        if (run_id, view) in self.deleted_views:
            return _err(404, "view_not_found")
        stored = self.results.get((run_id, view))
        if stored is None:
            return _err(409, "views_not_built")
        limit, after = int(params["limit"]), params.get("after")
        start = 0
        if after is not None:
            gen, _, pos = after.partition(":")
            if (run_id, view) in self.rebuild_once:
                self.rebuild_once.discard((run_id, view))
                stored["generation"] += "+"
            if gen != stored["generation"]:
                return _err(409, "generation_changed")
            start = int(pos)
        rows = stored["rows"][start : start + limit]
        end = start + len(rows)
        nxt = f"{stored['generation']}:{end}" if end < len(stored["rows"]) else None
        return httpx.Response(
            200,
            json={
                "run": {"id": run_id},
                "generation": stored["generation"],
                "columns": [],
                "rows": rows,
                "next": nxt,
            },
        )


def _err(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": code})


def _queued(run_id: str) -> dict[str, Any]:
    return {
        "run": {"id": run_id, "status": "queued", "started_at": None, "finished_at": None},
        "updated_at": None,
        "running": [],
        "nodes_finished": None,
        "findings": None,
        "reports": None,
        "stale": False,
    }


def row(
    unit_key: str, *, state: str = "none", option: str | None = None, konto: str = "4711"
) -> dict[str, Any]:
    return {
        "unit_key": unit_key,
        "row_key": unit_key,
        "data": {"konto": konto},
        "findings": {"count": 1, "by_severity": {"warning": 1}, "open": 1},
        "review": {"state": state, "key": unit_key, "option": option, "at": None, "by": None},
        "url": f"https://studio.example/runs/x?link={unit_key}",
    }


def progress(
    run_id: str, status: str = "running", *, done: int = 3, count: int = 10
) -> dict[str, Any]:
    """An answer of GET /runs/:id/progress."""
    return {
        "run": {"id": run_id, "status": status, "started_at": "t0", "finished_at": None},
        "updated_at": "2026-11-12T10:00:05Z",
        "running": [{"node_id": "extract", "items_done": done, "item_count": count}],
        "nodes_finished": 2,
        "findings": 0,
        "reports": 0,
        "stale": False,
    }
