"""Command line interface: `energy-analyzer init|backfill|daily|report|live|selftest`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from energy_analyzer import __version__
from energy_analyzer.config import CONFIG_TEMPLATE, ConfigError, Settings, load_settings
from energy_analyzer.fetch import (
    AlphaESSClient,
    ApiError,
    AuthError,
    DayResult,
    Fetcher,
    SystemNotFoundError,
    TransientError,
    Transport,
    mask_sn,
    resolve_system,
    urllib_transport,
)
from energy_analyzer.storage import Storage

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_API = 0, 1, 2, 3

ENV_EXAMPLE = """\
# Copy to .env (keep it private: chmod 600 .env) and fill in. Never commit or share it.
# Developer ID / Developer Key from https://open.alphaess.com
ALPHA_APP_ID=
ALPHA_APP_SECRET=
# Only needed if your developer account has more than one system:
ALPHA_SYSTEM_SN=
"""


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="energy-analyzer",
        description="AlphaESS Open API -> SQLite -> analysis -> offline HTML report.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", type=Path, help="config.toml (default: ./config.toml if present)")
    p.add_argument("--env-file", type=Path, help=".env with credentials (default: next to config)")
    p.add_argument("--db", type=Path, help="SQLite file (default: energy.db)")
    p.add_argument("--sn", help="system serial, if the account has several systems")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create config.toml + .env.example and check credentials")
    s.add_argument("--dir", type=Path, help="where to create the files (default: DB folder)")
    s.add_argument("--no-check", action="store_true", help="do not call the API")

    s = sub.add_parser("backfill", help="fetch a date range (one day per request, rate limited)")
    s.add_argument("--from", dest="first", type=_date, required=True, metavar="YYYY-MM-DD")
    s.add_argument("--to", dest="last", type=_date, required=True, metavar="YYYY-MM-DD")

    s = sub.add_parser("daily", help="fetch yesterday and today (safe to repeat)")
    s.add_argument("--report", type=Path, help="also regenerate this HTML report")
    s.add_argument("--today", type=_date, help=argparse.SUPPRESS)

    s = sub.add_parser("report", help="write the HTML report from the database")
    s.add_argument("--out", type=Path, default=Path("report.html"))
    s.add_argument("--from", dest="first", type=_date, metavar="YYYY-MM-DD")
    s.add_argument("--to", dest="last", type=_date, metavar="YYYY-MM-DD")

    sub.add_parser("live", help="show the latest live power values")

    sub.add_parser("refresh", help="fetch today only (2 API calls), for frequent dashboard updates")

    s = sub.add_parser("serve", help="run the web dashboard")
    s.add_argument("--host", help="address to listen on (default from [web] host: 127.0.0.1)")
    s.add_argument("--port", type=int, help="port (default from [web] port: 8088)")
    s.add_argument(
        "--demo", action="store_true", help="serve 120 days of synthetic demo data (no account)"
    )

    s = sub.add_parser("selftest", help="run the full pipeline on bundled synthetic data")
    s.add_argument("--keep", type=Path, help="keep the selftest DB and report in this folder")
    return p


def _client(settings: Settings, transport: Transport | None) -> AlphaESSClient:
    app_id, secret = settings.require_credentials()
    return AlphaESSClient(
        app_id,
        secret,
        base_url=settings.base_url,
        transport=transport or urllib_transport,
        min_interval_s=settings.min_interval_s,
        timeout_s=settings.timeout_s,
        max_retries=settings.max_retries,
        backoff_s=settings.backoff_s,
    )


def _show(path: Path) -> str:
    """Path for messages: relative to the working directory when possible, so logs and
    journals do not carry home directories."""
    try:
        return Path(os.path.relpath(path)).as_posix()
    except ValueError:  # different drive on Windows
        return path.name


def _say(msg: str) -> None:
    print(msg, flush=True)


def _print_day(r: DayResult) -> None:
    energy = "daily totals" if r.has_energy else "no daily totals"
    _say(f"  {r.day}: {r.power_rows} power samples, {energy}")


def cmd_init(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    target = args.dir or settings.db_path.parent
    target.mkdir(parents=True, exist_ok=True)
    for name, content in (("config.toml", CONFIG_TEMPLATE), (".env.example", ENV_EXAMPLE)):
        path = target / name
        if path.exists():
            _say(f"exists, left unchanged: {_show(path)}")
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
            _say(f"created: {_show(path)}")
    with Storage(settings.db_path) as db:
        _say(f"database ready: {_show(settings.db_path)} (schema v{db.schema_version()})")
    if args.no_check:
        return EXIT_OK
    if not (settings.app_id and settings.app_secret):
        if settings.env_path is not None and settings.env_path.is_file():
            _say(
                f"next: put ALPHA_APP_ID and ALPHA_APP_SECRET into {_show(settings.env_path)} "
                "and run `energy-analyzer init` again to check them."
            )
        else:
            _say(
                f"next: copy {_show(target / '.env.example')} to {_show(target / '.env')}, "
                "fill in your credentials and run `energy-analyzer init` again to check them."
            )
        return EXIT_OK
    client = _client(settings, transport)
    systems = client.get_ess_list()
    _say(f"credentials OK: {len(systems)} system(s) in this developer account")
    for info in systems:
        sn = str(info.get("sysSn", ""))
        _say(
            f"  {mask_sn(sn)}  inverter {info.get('minv', '?')}, "
            f"PV {info.get('popv', '?')} kWp, battery {info.get('cobat', '?')} kWh"
        )
    if len(systems) > 1 and not settings.sys_sn:
        _say("several systems found: set ALPHA_SYSTEM_SN in .env (or [system] sn) to pick one")
    return EXIT_OK


def _fetcher(
    settings: Settings, db: Storage, transport: Transport | None
) -> tuple[Fetcher, AlphaESSClient]:
    client = _client(settings, transport)
    sn, info = resolve_system(client, settings.sys_sn)
    db.upsert_system(sn, info)
    _say(f"system {mask_sn(sn)}")
    return Fetcher(client, db, sn), client


def _known_system_fetcher(settings: Settings, db: Storage, transport: Transport | None) -> Fetcher:
    """Like _fetcher, but skips getEssList when the system is already known (saves a call)."""
    systems = db.systems()
    sn = settings.sys_sn or (systems[0] if len(systems) == 1 else None)
    if sn is None:
        return _fetcher(settings, db, transport)[0]
    return Fetcher(_client(settings, transport), db, sn)


def cmd_backfill(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    if args.last < args.first:
        raise ConfigError("--to must not be before --from")
    if args.first > date.today():
        raise ConfigError("--from is in the future")
    days = (args.last - args.first).days + 1
    eta = timedelta(seconds=int(days * 2 * settings.min_interval_s))
    _say(f"backfill {args.first} .. {args.last}: {days} day(s), about {eta} (API rate limit)")
    with Storage(settings.db_path) as db:
        fetcher, _ = _fetcher(settings, db, transport)
        results = fetcher.backfill(args.first, args.last, on_day=_print_day)
    _say(f"done: {sum(r.power_rows for r in results)} samples, {len(results)} day(s)")
    return EXIT_OK


def _pick_sn(settings: Settings, db: Storage) -> str:
    if settings.sys_sn:
        return settings.sys_sn
    systems = db.systems()
    if not systems:
        raise ConfigError("the database has no data yet: run `energy-analyzer backfill` first")
    if len(systems) > 1:
        raise ConfigError("the database holds several systems: choose one with --sn")
    return systems[0]


def _write_report(
    settings: Settings, db: Storage, out: Path, first: date | None, last: date | None
) -> Path:
    from energy_analyzer.analyze import analyze
    from energy_analyzer.report import write_report

    sn = _pick_sn(settings, db)
    result = analyze(
        db, sn, settings, first.isoformat() if first else None, last.isoformat() if last else None
    )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    path = write_report(result, out, title=settings.report_title, generated_at=stamp)
    _say(f"report: {_show(path)} ({len(result.days)} day(s), {len(result.anomalies)} anomalies)")
    return path


def cmd_daily(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    with Storage(settings.db_path) as db:
        fetcher, _ = _fetcher(settings, db, transport)
        for r in fetcher.daily(args.today):
            _print_day(r)
        if args.report:
            _write_report(settings, db, args.report, None, None)
    return EXIT_OK


def cmd_report(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    with Storage(settings.db_path) as db:
        _write_report(settings, db, args.out, args.first, args.last)
    return EXIT_OK


def cmd_live(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    client = _client(settings, transport)
    sn, _ = resolve_system(client, settings.sys_sn)
    data = client.get_last_power(sn) or {}
    _say(f"system {mask_sn(sn)}")
    for key, label in (("ppv", "PV"), ("pload", "load"), ("pgrid", "grid"), ("pbat", "battery")):
        _say(f"  {label:8} {data.get(key, '-')} W")
    _say(f"  {'SoC':8} {data.get('soc', '-')} %")
    return EXIT_OK


def cmd_refresh(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    with Storage(settings.db_path) as db:
        _print_day(_known_system_fetcher(settings, db, transport).fetch_day(date.today()))
    return EXIT_OK


def cmd_serve(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    import tempfile

    from energy_analyzer.web import App, make_server

    host = args.host or settings.web_host
    port = args.port or settings.web_port
    tmp = None
    if args.demo:
        from energy_analyzer.demo import DEMO_APP_ID, DEMO_APP_SECRET, build_demo

        tmp = tempfile.TemporaryDirectory(prefix="energy-demo-")
        db_path = Path(tmp.name) / "demo.db"
        _say("building demo data (120 synthetic days) ...")
        transport = build_demo(db_path)
        settings = replace(
            settings,
            db_path=db_path,
            sys_sn=None,
            app_id=DEMO_APP_ID,
            app_secret=DEMO_APP_SECRET,
            base_url="https://demo.invalid/api",
        )
    app = App(settings, transport=transport, demo=args.demo)
    server = make_server(app, host, port)
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    _say(f"dashboard: http://{shown}:{server.server_address[1]}/ (Ctrl+C to stop)")
    if app.auth:
        _say(f"login user: {settings.web_user} (password: WEB_PASSWORD in your .env)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say("stopped")
    finally:
        server.server_close()
        if tmp is not None:
            tmp.cleanup()
    return EXIT_OK


def cmd_selftest(args: argparse.Namespace, settings: Settings, transport: Transport | None) -> int:
    from energy_analyzer.selftest import run_selftest

    return EXIT_OK if run_selftest(keep_dir=args.keep, out=_say) else EXIT_ERROR


COMMANDS = {
    "init": cmd_init,
    "backfill": cmd_backfill,
    "daily": cmd_daily,
    "report": cmd_report,
    "live": cmd_live,
    "refresh": cmd_refresh,
    "serve": cmd_serve,
    "selftest": cmd_selftest,
}


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "selftest" or (args.command == "serve" and args.demo):
            settings = Settings()  # must not depend on local config, data or credentials
        else:
            settings = load_settings(
                args.config, args.env_file, {"db_path": args.db, "sys_sn": args.sn}
            )
        return COMMANDS[args.command](args, settings, transport)
    except (ConfigError, AuthError, SystemNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (TransientError, ApiError) as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return EXIT_API
    except KeyboardInterrupt:
        print("interrupted (data fetched so far is saved)", file=sys.stderr)
        return EXIT_ERROR
