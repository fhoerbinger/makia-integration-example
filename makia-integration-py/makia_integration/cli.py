"""The `makia-integration` command.

Exit codes: 0 done, 1 a job failed, 2 configuration error. Expected failures print one
line to stderr; `-v` adds the traceback.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import traceback
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from makia_integration import __version__
from makia_integration.integration import Integration, ConfigError, SendError, SyncError
from makia_integration.client import ApiError, TransportFailed
from makia_integration.doctor import render, run_checks
from makia_integration.config import Config, load_integration, load_config, read_env_file
from makia_integration.runner import Runner
from makia_integration.scaffold import new_integration
from makia_integration.state import SqliteStateStore, StateLocked

PROG = "makia-integration"
JOBS = ("sync", "send", "poll")
SENDING = (
    "send",
    "run",
    "listen",
)  # need exactly one of MAKIA_STORAGE_CONNECTION_ID, MAKIA_FILES_DIR


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _common() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=Path, default=argparse.SUPPRESS, help="read KEY=VALUE")
    common.add_argument("--integration", default=argparse.SUPPRESS, help="package.module:function")
    common.add_argument("--log-file", type=Path, default=argparse.SUPPRESS, help="rotating log")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    return common


def _parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=None, help="read KEY=VALUE lines from it")
    parser.add_argument(
        "--integration", default=None, help="package.module:function (MAKIA_INTEGRATION)"
    )
    parser.add_argument("--log-file", type=Path, default=None, help="also log to this file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logs and tracebacks")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[common])

    add("sync", "send projects and members")
    add("send", "start runs for new records")
    add("poll", "read the change feed and copy results")
    run = add("run", "sync, send and poll in a loop")
    run.add_argument("--interval", type=_positive, default=60, help="seconds between rounds (60)")
    run.add_argument("--sync-every", type=_positive, default=60, help="sync every N rounds (60)")
    run.add_argument("--listen", type=int, metavar="PORT", help="also receive webhooks")
    listen = add("listen", "receive webhooks and run a round on each")
    listen.add_argument("--port", type=int, default=8080, help="port to listen on (8080)")
    listen.add_argument("--sync-every", type=_positive, default=60, help="sync every N rounds (60)")
    retry = add("retry", "let refused or gone records be sent again")
    which = retry.add_mutually_exclusive_group(required=True)
    which.add_argument("external_id", nargs="?", help="one record's external id (refused or gone)")
    which.add_argument("--all-refused", action="store_true", help="every record refused for good")
    which.add_argument(
        "--all-gone",
        action="store_true",
        help="every gone run; a case deleted on purpose gets a new case and run",
    )
    drop = add("drop-pending", "forget a sink call that cannot be delivered")
    drop.add_argument("external_id", help="the record's external id")
    doctor = add("doctor", "check the setup; run this first")
    doctor.add_argument("--listen", type=int, metavar="PORT", help="check the port can be bound")
    add("docs", "print the path of the bundled guides")
    new = add("new", "write a starter integration into an empty folder")
    new.add_argument("directory", type=Path, help="folder to create or fill")
    add("version", "print the version")
    _add_webhook_parser(sub, common)
    return parser


def _add_webhook_parser(sub: Any, common: argparse.ArgumentParser) -> None:
    hook = sub.add_parser(
        "webhook", help="manage this account's MAKIA webhooks (run token)", parents=[common]
    )
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


def _run_webhook(config: Config, args: argparse.Namespace) -> int:
    api = config.run_client()
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
                "This secret is shown once. The old one stopped signing. "
                "Update MAKIA_WEBHOOK_SECRET."
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
    finally:
        api.close()
    return 0


def _setup_logging(verbose: bool, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        try:
            handlers.append(
                RotatingFileHandler(
                    log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
            )
        except OSError as err:
            raise ConfigError(f"cannot write the log file {log_file} ({err})") from err
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _integration_factory(config: Config, ref: str | None) -> Any:
    if not ref:
        raise ConfigError("set MAKIA_INTEGRATION or pass --integration package.module:function")
    return load_integration(ref)


def _one_shot(config: Config, command: str, ref: str | None) -> int:
    integration: Integration = _integration_factory(config, ref)(config)
    try:
        report = getattr(integration, command)()
        print(f"{command}: {report}")
    finally:
        integration.close()
    return 0


def _run_loop(
    config: Config, args: argparse.Namespace, ref: str | None, env: Mapping[str, str]
) -> int:
    factory = _integration_factory(config, ref)
    listen_port = args.port if args.command == "listen" else args.listen
    if listen_port is not None and not config.webhook_secret:
        raise ConfigError("set MAKIA_WEBHOOK_SECRET to receive webhooks")
    fatal: list[BaseException] = []
    signalled: list[int] = []
    box: list[Runner] = []

    def build() -> Integration:
        try:
            return factory(config)  # type: ignore[no-any-return]
        except (StateLocked, ConfigError) as err:
            fatal.append(err)  # retrying cannot fix it: end the loop
            box[0].stop()
            raise

    runner = Runner(
        build,
        interval=None if args.command == "listen" else args.interval,
        listen_port=listen_port,
        webhook_secret=config.webhook_secret,
        sync_every=args.sync_every,
        describe=lambda err: _describe(err, env)[0],
    )
    box.append(runner)

    def stop(signum: int, frame: Any) -> None:
        signalled.append(signum)
        runner.stop()

    signals = [signal.SIGINT] + ([signal.SIGTERM] if hasattr(signal, "SIGTERM") else [])
    if threading.current_thread() is not threading.main_thread():
        signals = []  # signal.signal works on the main thread only
    previous = {sig: signal.getsignal(sig) for sig in signals}
    for sig in signals:
        signal.signal(sig, stop)
    try:
        try:
            runner.start()
        except OSError as err:
            runner.stop()
            runner.join(timeout=5)
            raise ConfigError(
                f"cannot listen on port {listen_port} ({err}; is another program using it?)"
            ) from err
        runner.join()
    finally:
        for sig, handler in previous.items():
            if handler is not None:
                signal.signal(sig, handler)
    if fatal:
        raise fatal[0]
    if signalled:
        return 0  # asked to stop: every failed step was logged when it happened
    if runner.last_error is not None:
        raise runner.last_error
    return 0


def _doctor(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = load_config(env, require=())
    ref = args.integration or config.integration_ref
    factory = None
    if ref:
        try:
            factory = load_integration(ref)
        except ConfigError as err:
            print(f"{PROG}: {err}", file=sys.stderr)
            return 2
    checks = run_checks(config, factory=factory, listen_port=args.listen)
    print(render(checks))
    return 0 if all(c.ok for c in checks) else 1


def _open_existing_state(env: Mapping[str, str]) -> SqliteStateStore:
    config = load_config(env, require=())
    if not config.state_file.exists():
        raise ConfigError(
            f"{config.state_file} does not exist (set MAKIA_STATE_FILE to the integration's state file)"
        )
    return SqliteStateStore(config.state_file)


def _retry(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = _open_existing_state(env)
    try:
        if args.all_gone:
            cleared, what = store.clear_gone(), "gone"
        else:
            ext_id = None if args.all_refused else args.external_id
            cleared = store.clear_refusals(ext_id)
            what = "refused" if args.all_refused else "refused or gone"
    finally:
        store.close()
    if cleared:
        print(
            f"cleared {len(cleared)} {what} record(s): {', '.join(cleared)}; "
            "the next send tries them again"
        )
    elif args.external_id:
        print(f"cleared 0: {args.external_id} is not refused or gone")
    else:
        print(f"cleared 0: no record is {what}")
    return 0


def _drop_pending(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = _open_existing_state(env)
    try:
        dropped = store.drop_pending(args.external_id)
    finally:
        store.close()
    if not dropped:
        print(f"{PROG}: {args.external_id} has no pending sink call", file=sys.stderr)
        return 1
    print(
        f"dropped {args.external_id}: its pending sink call will not be made; "
        "if records() returns it again, the next send sends it again"
    )
    return 0


def _new(directory: Path) -> int:
    try:
        written = new_integration(directory)
    except FileExistsError as err:
        raise ConfigError(f"{err}; pick an empty or new folder") from err
    except (NotADirectoryError, OSError) as err:
        raise ConfigError(f"cannot write the starter integration ({err})") from err
    for path in written:
        print(path)
    print(f"next: read {directory / 'README.md'}")
    return 0


def _dispatch(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    cmd = args.command
    if cmd == "version":
        print(f"{PROG} {__version__}")
        return 0
    if cmd == "docs":
        print((Path(__file__).parent / "docs").resolve())
        return 0
    if cmd == "new":
        return _new(args.directory)
    if cmd == "doctor":
        return _doctor(args, env)
    if cmd == "retry":
        return _retry(args, env)
    if cmd == "drop-pending":
        return _drop_pending(args, env)
    if cmd == "webhook":
        return _run_webhook(load_config(env, require=("MAKIA_API_URL", "MAKIA_RUN_TOKEN")), args)
    config = load_config(env, sending=cmd in SENDING)
    ref = args.integration or config.integration_ref
    if cmd in JOBS:
        return _one_shot(config, cmd, ref)
    return _run_loop(config, args, ref, env)


def _describe(err: BaseException, env: Mapping[str, str]) -> tuple[str, int]:
    """One line for an error, and its exit code."""
    if isinstance(err, (ConfigError, StateLocked)):
        return str(err), 2
    if isinstance(err, TransportFailed):
        host = urlparse(env.get("MAKIA_API_URL", "")).hostname or env.get("MAKIA_API_URL", "")
        return (
            f"cannot reach MAKIA at {host} "
            "(check the network, the proxy, and MAKIA_CA_BUNDLE for a private CA)",
            1,
        )
    if isinstance(err, ApiError):
        return f"MAKIA answered {err} (check the token, its permissions and the ids)", 1
    if isinstance(err, (SyncError, SendError)):
        return str(err), 1
    return f"{type(err).__name__}: {err}", 1


def _explain(err: BaseException, env: Mapping[str, str], job: str) -> tuple[str, int]:
    message, code = _describe(err, env)
    if isinstance(err, (ConfigError, StateLocked, TransportFailed, ApiError)):
        return message, code
    return f"{job} failed: {message}", code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    verbose = bool(getattr(args, "verbose", False))
    env: Mapping[str, str] = os.environ
    try:
        if args.env_file is not None:
            env = {
                **read_env_file(args.env_file),
                **{k: v for k, v in os.environ.items() if v.strip()},
            }
        _setup_logging(verbose, args.log_file)
        return _dispatch(args, env)
    except Exception as err:
        message, code = _explain(err, env, args.command)
        print(f"{PROG}: {message}", file=sys.stderr)
        if verbose:
            traceback.print_exception(err, file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
