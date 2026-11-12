"""`bridge init-db | sync | send | poll | listen | run | webhook`. Configuration comes from the
environment (see README.md)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
import time

from .api import ApiError, MakiaClient
from .db import create_schema
from .db import connect as db_connect
from .jobs import Bridge


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"bridge: set {name} (see README.md)")
    return value


def _bridge() -> Bridge:
    conn, _ = db_connect(os.environ.get("BRIDGE_DB", "bridge.db"))
    url = _env("MAKIA_API_URL")
    return Bridge(
        conn=conn,
        sync_api=MakiaClient(url, _env("MAKIA_SYNC_TOKEN")),
        run_api=MakiaClient(url, _env("MAKIA_RUN_TOKEN")),
        storage_connection_id=os.environ.get("MAKIA_STORAGE_CONNECTION_ID", ""),
        files_dir=Path(folder)
        if (folder := os.environ.get("MAKIA_FILES_DIR", "").strip())
        else None,
    )


def _add_webhook_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    hook = sub.add_parser("webhook", help="manage this account's MAKIA webhooks (run token)")
    cmds = hook.add_subparsers(dest="webhook_command", required=True)
    create = cmds.add_parser("create", help="create a webhook; prints its secret once")
    create.add_argument("--name", required=True)
    create.add_argument("--connection", required=True, help="organization connection id (uuid)")
    create.add_argument("--path", required=True, help="appended to the connection's base URL")
    for flag, what in (
        ("project", "project"),
        ("workflow", "workflow"),
        ("blueprint", "blueprint"),
        ("type", "change type"),
    ):
        create.add_argument(f"--{flag}", action="append", default=[], help=f"only this {what}")
    cmds.add_parser("list", help="list webhooks")
    for name in ("enable", "disable", "rotate-secret", "delete", "deliveries"):
        one = cmds.add_parser(name, help=f"{name.replace('-', ' ')} a webhook")
        one.add_argument("id")
        if name == "deliveries":
            one.add_argument("--limit", type=int, help="newest N deliveries (default 50)")


def _webhook(args: argparse.Namespace) -> None:
    api = MakiaClient(_env("MAKIA_API_URL"), _env("MAKIA_RUN_TOKEN"))
    cmd = args.webhook_command
    try:
        if cmd == "create":
            made = api.create_webhook(
                name=args.name,
                connection_id=args.connection,
                path=args.path,
                projects=args.project,
                workflows=args.workflow,
                blueprints=args.blueprint,
                types=args.type,
            )
            print(f"webhook {made['webhook']['id']}")
            print(f"secret  {made['secret']}")
            print("This secret is shown once. Set it: export MAKIA_WEBHOOK_SECRET=<secret>")
        elif cmd == "list":
            for h in api.webhooks():
                filters = {
                    k: h[k] for k in ("projects", "workflows", "blueprints", "types") if h.get(k)
                }
                print(
                    f"{h['id']}  {h['name']}  enabled={str(h['enabled']).lower()}  "
                    f"switched_off_reason={h.get('switched_off_reason') or '-'}  "
                    f"filters={json.dumps(filters, sort_keys=True)}  last_seq={h['last_seq']}"
                )
        elif cmd in ("enable", "disable"):
            h = api.update_webhook(args.id, enabled=cmd == "enable")
            print(f"{h['id']} enabled={str(h['enabled']).lower()}")
        elif cmd == "rotate-secret":
            print(f"secret  {api.rotate_webhook_secret(args.id)}")
            print(
                "This secret is shown once. The old one stopped signing. Update MAKIA_WEBHOOK_SECRET."
            )
        elif cmd == "delete":
            api.delete_webhook(args.id)
            print(f"deleted {args.id}")
        else:
            for d in api.webhook_deliveries(args.id, limit=args.limit):
                print(
                    f"{d['created_at']}  {d['id']}  {d['event']}  seq={d['seq']}  {d['status']}  "
                    f"attempts={d['attempts']}  code={d['last_status_code']}  "
                    f"error={d['last_error'] or '-'}"
                )
    except ApiError as e:
        sys.exit(f"bridge: {e.code} (HTTP {e.status})")
    finally:
        api.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the source and bridge tables")
    sub.add_parser("sync", help="send projects and members")
    sub.add_parser("send", help="start runs for new records")
    sub.add_parser("poll", help="read the change feed and copy results")
    listen = sub.add_parser("listen", help="receive webhooks and poll on each (no loop needed)")
    listen.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    loop = sub.add_parser("run", help="sync, send and poll in a loop")
    loop.add_argument(
        "--interval", type=int, default=60, help="seconds between rounds (default 60)"
    )
    loop.add_argument("--sync-every", type=int, default=60, help="sync every N rounds (default 60)")
    _add_webhook_parser(sub)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "init-db":
        conn, dialect = db_connect(os.environ.get("BRIDGE_DB", "bridge.db"))
        create_schema(conn, dialect)
        return

    if args.command == "webhook":
        _webhook(args)
        return

    bridge = _bridge()
    if args.command == "sync":
        bridge.sync()
    elif args.command == "send":
        bridge.send()
    elif args.command == "poll":
        bridge.poll()
    elif args.command == "listen":
        from .receiver import serve

        serve(bridge, _env("MAKIA_WEBHOOK_SECRET"), args.port)
    else:
        round_no = 0
        while True:
            if round_no % args.sync_every == 0:
                bridge.sync()
            bridge.send()
            bridge.poll()
            round_no += 1
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
