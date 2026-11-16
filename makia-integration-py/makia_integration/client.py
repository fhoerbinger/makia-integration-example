"""A thin client for the parts of MAKIA's stable API an integration uses.

Every route the integration calls is listed in ROUTES_USED; a test checks each one
against MAKIA's published API description (openapi.json).
"""

from __future__ import annotations

import mimetypes
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from makia_integration.model import ConfigError

ROUTES_USED: tuple[tuple[str, str], ...] = (
    ("POST", "/auth/token"),
    ("GET", "/me"),
    ("PUT", "/sync/projects/{externalId}"),
    ("GET", "/sync/projects"),
    ("POST", "/inbound/projects/{externalId}/workflows/{slug}/runs"),
    ("GET", "/changes/head"),
    ("GET", "/changes"),
    ("GET", "/runs/{id}/results/{view}"),
    ("GET", "/runs/{id}/progress"),
    ("GET", "/runs/{id}/views"),
    ("POST", "/webhooks"),
    ("GET", "/webhooks"),
    ("PATCH", "/webhooks/{id}"),
    ("DELETE", "/webhooks/{id}"),
    ("POST", "/webhooks/{id}/rotate-secret"),
    ("GET", "/webhooks/{id}/deliveries"),
)

__version__ = "0.1.0"
_USER_AGENT = f"makia-integration/{__version__}"

# Swap a little before the 15-minute access token runs out.
_REFRESH_MARGIN_S = 60


class TransportFailed(Exception):
    """A request could not reach MAKIA, even after the retries."""


@dataclass
class ApiError(Exception):
    status: int
    code: str
    body: Any = None

    def __str__(self) -> str:
        return f"{self.status} {self.code}"


def _error(resp: httpx.Response) -> ApiError:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    code = body.get("error", "") if isinstance(body, dict) else ""
    return ApiError(resp.status_code, code or "unknown", body)


def _content_type(file: Path) -> str:
    return mimetypes.guess_type(file.name)[0] or "application/octet-stream"


def tls_context(ca_bundle: str | None) -> ssl.SSLContext:
    """The trust store every call uses: `ca_bundle` if given, else the system store
    (on Windows, its certificate store), so a company CA there is trusted.

    Not strict (Python 3.13 turns VERIFY_X509_STRICT on, which refuses many company
    CAs). With a bundle, any certificate in it is a trust anchor (PARTIAL_CHAIN), so
    the chain `doctor` saves (server certificate and intermediates, often no root)
    works as it is."""
    try:
        ctx = ssl.create_default_context(cafile=ca_bundle)
    except (ssl.SSLError, OSError) as err:
        raise ConfigError(
            f"MAKIA_CA_BUNDLE {ca_bundle} is not a readable PEM certificate file ({err})"
        ) from err
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    if ca_bundle:
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


class MakiaClient:
    """One service account's view of MAKIA.

    `secret` is the account's API token as minted in Studio. It is swapped at
    POST /auth/token for an access token, which every call sends; a 401 swaps
    again once. The inbound routes are the exception: they take the secret.
    """

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        verify: str | bool | ssl.SSLContext | None = None,
        retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._secret = secret
        self._retries = retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            verify=tls_context(None) if verify is None else verify,
            trust_env=True,
            headers={"User-Agent": _USER_AGENT},
        )
        self._access: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        self._http.close()

    def _send(
        self, method: str, path: str, *, retries: int | None = None, **kwargs: Any
    ) -> httpx.Response:
        """One request, retried on transport errors, 429 and 5xx with a doubling
        delay (capped at 30 s); a Retry-After in seconds wins."""
        retries = self._retries if retries is None else retries
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise TransportFailed(f"{method} {path}: {exc}") from exc
                self._sleep(min(0.5 * 2**attempt, 30))
                continue
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            if last:
                return resp
            delay = min(0.5 * 2**attempt, 30)
            try:
                after = float(resp.headers["Retry-After"])
            except (KeyError, ValueError):
                after = -1.0
            if 0 <= after:
                delay = min(after, 30.0)
            self._sleep(delay)
        raise AssertionError("unreachable")

    # --- auth -------------------------------------------------------------

    def _swap(self) -> str:
        resp = self._send("POST", "/auth/token", json={"token": self._secret})
        if resp.status_code != 200:
            raise _error(resp)
        body = resp.json()
        self._access = body["access_token"]
        self._expires_at = time.monotonic() + body["expires_in"] - _REFRESH_MARGIN_S
        return self._access

    def _token(self) -> str:
        if self._access is None or time.monotonic() >= self._expires_at:
            return self._swap()
        return self._access

    def _call(
        self, method: str, path: str, *, retries: int | None = None, **kwargs: Any
    ) -> httpx.Response:
        token = self._token()
        resp = self._send(
            method, path, retries=retries, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            token = self._swap()
            resp = self._send(
                method,
                path,
                retries=retries,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        return resp

    def _json(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        resp = self._call(method, path, **kwargs)
        if resp.status_code not in ok:
            raise _error(resp)
        return resp.json()

    # --- routes -----------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._json("GET", "/me")

    def put_project(
        self, external_id: str, *, name: str, archived: bool, members: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = {"name": name, "archived": archived, "members": members}
        return self._json("PUT", f"/sync/projects/{quote(external_id, safe='')}", json=body)

    def synced_projects(self) -> list[dict[str, Any]]:
        return self._json("GET", "/sync/projects")["projects"]

    def send_record(
        self,
        project_external_id: str,
        workflow_slug: str,
        *,
        external_id: str,
        sources: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        """Start a run over a record's files. Answers (status, body): 202 for a
        new case, 200 when MAKIA already knows `external_id` (nothing written)."""
        path = (
            f"/inbound/projects/{quote(project_external_id, safe='')}"
            f"/workflows/{quote(workflow_slug, safe='')}/runs"
        )
        body = {"external_id": external_id, "sources": sources}
        # Inbound takes the secret itself, not an access token.
        resp = self._send(
            "POST", path, json=body, headers={"Authorization": f"Bearer {self._secret}"}
        )
        if resp.status_code not in (200, 202):
            raise _error(resp)
        return resp.status_code, resp.json()

    def send_record_files(
        self,
        project_external_id: str,
        workflow_slug: str,
        *,
        external_id: str,
        files: list[tuple[str, Path]],
    ) -> tuple[int, dict[str, Any]]:
        """Start a run over files uploaded as multipart: `external_id` first, then
        one part per file named by its input id. Answers as `send_record` does."""
        path = (
            f"/inbound/projects/{quote(project_external_id, safe='')}"
            f"/workflows/{quote(workflow_slug, safe='')}/runs"
        )
        # Bytes, not open handles: a retry must be able to send the body again.
        parts = [
            (input_id, (file.name, file.read_bytes(), _content_type(file)))
            for input_id, file in files
        ]
        resp = self._send(
            "POST",
            path,
            data={"external_id": external_id},
            files=parts,
            headers={"Authorization": f"Bearer {self._secret}"},
        )
        if resp.status_code not in (200, 202):
            raise _error(resp)
        return resp.status_code, resp.json()

    def changes_head(self) -> int:
        return self._json("GET", "/changes/head")["head"]

    def changes(self, after: int, *, limit: int = 100) -> dict[str, Any]:
        return self._json("GET", "/changes", params={"after": after, "limit": limit})

    def run_progress(self, run_id: str) -> dict[str, Any]:
        """The live snapshot of a run: items done per running node. No total."""
        return self._json("GET", f"/runs/{run_id}/progress")

    def run_views(self, run_id: str) -> list[dict[str, Any]]:
        """The views of a run; `built` is false for one not produced (yet)."""
        return self._json("GET", f"/runs/{run_id}/views")["views"]

    def results_page(
        self, run_id: str, view: str, *, after: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        return self._json("GET", f"/runs/{run_id}/results/{quote(view, safe='')}", params=params)

    # --- webhooks (run account only) --------------------------------------

    def create_webhook(
        self,
        *,
        name: str,
        connection_id: str,
        path: str,
        projects: list[str] | None = None,
        workflows: list[str] | None = None,
        blueprints: list[str] | None = None,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Answers {webhook, secret}; the secret is shown once. Never retried: a retry
        after a lost answer would create a second webhook."""
        body: dict[str, Any] = {"name": name, "connection_id": connection_id, "path": path}
        for key, value in (
            ("projects", projects),
            ("workflows", workflows),
            ("blueprints", blueprints),
            ("types", types),
        ):
            if value:
                body[key] = value
        return self._json("POST", "/webhooks", ok=(201,), retries=0, json=body)

    def webhooks(self) -> list[dict[str, Any]]:
        return self._json("GET", "/webhooks")["webhooks"]

    def update_webhook(self, webhook_id: str, **changes: Any) -> dict[str, Any]:
        """PATCH with only the given fields (name, path, enabled, filters)."""
        return self._json("PATCH", f"/webhooks/{quote(webhook_id, safe='')}", json=changes)[
            "webhook"
        ]

    def delete_webhook(self, webhook_id: str) -> None:
        resp = self._call("DELETE", f"/webhooks/{quote(webhook_id, safe='')}")
        if resp.status_code != 204:
            raise _error(resp)

    def rotate_webhook_secret(self, webhook_id: str) -> str:
        path = f"/webhooks/{quote(webhook_id, safe='')}/rotate-secret"
        return self._json("POST", path)["secret"]

    def webhook_deliveries(
        self, webhook_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        params = {"limit": limit} if limit is not None else None
        path = f"/webhooks/{quote(webhook_id, safe='')}/deliveries"
        return self._json("GET", path, params=params)["deliveries"]
