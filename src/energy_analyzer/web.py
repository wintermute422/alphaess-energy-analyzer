"""Dashboard web server: static UI + JSON API over the SQLite database (stdlib only).

See docs/web-api.md for the API. One Storage is opened per request (SQLite in WAL mode lets
the refresh timer write meanwhile). Security: HTTP Basic auth when a password is configured
(mandatory on non-loopback addresses), same-origin + JSON checks for mutations, a strict CSP,
and a fixed whitelist of static files.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import statistics
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from energy_analyzer import __version__
from energy_analyzer.analyze import analyze
from energy_analyzer.config import ConfigError, Settings
from energy_analyzer.events import EFFECTS, EVENT_KINDS, EventError, validate
from energy_analyzer.fetch import AlphaESSClient, ApiError, Transport, mask_sn, urllib_transport
from energy_analyzer.models import AnalysisResult, Event
from energy_analyzer.report import render_report
from energy_analyzer.storage import Storage

log = logging.getLogger(__name__)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# The single-file report uses an inline <style>; it gets its own, still script-free, policy.
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
LIVE_TTL_S = 60.0
MAX_BODY = 64 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _day(text: str | None, name: str) -> str | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise HttpError(400, f"{name} must be YYYY-MM-DD") from exc


def _ratio(num: float, den: float) -> float | None:
    return None if den <= 0 else max(0.0, min(1.0, num / den))


def event_json(e: Event) -> dict[str, Any]:
    return asdict(e)


class App:
    """Everything a request needs besides the request itself."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Transport | None = None,
        demo: bool = False,
        auth: bool = True,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.demo = demo
        self.auth = auth and bool(settings.web_password)
        self._live_lock = threading.Lock()
        self._live: tuple[float, dict[str, Any]] | None = None

    # --- helpers ----------------------------------------------------------------------------

    def db(self) -> Storage:
        return Storage(self.settings.db_path)

    def sys_sn(self, db: Storage) -> str | None:
        if self.settings.sys_sn:
            return self.settings.sys_sn
        systems = db.systems()
        return systems[0] if len(systems) == 1 else None

    def check_auth(self, header: str | None) -> bool:
        if not self.auth:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), self.settings.web_user.encode())
        ok_pw = hmac.compare_digest(password.encode(), (self.settings.web_password or "").encode())
        return ok_user and ok_pw

    def _default_range(self, db: Storage, sn: str, days: int) -> tuple[str | None, str | None]:
        _, last = db.day_bounds(sn)
        if last is None:
            return None, None
        first = (date.fromisoformat(last) - timedelta(days=days - 1)).isoformat()
        return first, last

    def _analysis(self, db: Storage, sn: str, lo: str | None, hi: str | None) -> AnalysisResult:
        return analyze(db, sn, self.settings, lo, hi)

    # --- endpoints ---------------------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        with self.db() as db:
            sn = self.sys_sn(db)
            first, last = db.day_bounds(sn) if sn else (None, None)
            last_fetch = db.last_fetch()
        return {
            "version": __version__,
            "system": mask_sn(sn) if sn else None,
            "first_day": first,
            "last_day": last,
            "last_fetch_at": last_fetch,
            "live_available": bool(self.settings.app_id and self.settings.app_secret),
            "demo": self.demo,
            "settings": {
                "night_window": f"{self.settings.night_start}-{self.settings.night_end}",
                "night_start": self.settings.night_start,
                "night_end": self.settings.night_end,
                "rolling_days": str(self.settings.rolling_days),
                "z_threshold": str(self.settings.z_threshold),
                "min_rel_deviation": str(self.settings.min_rel_deviation),
                "spike_factor": str(self.settings.spike_factor),
                "spike_min_w": str(self.settings.spike_min_w),
                "spike_routine_share": str(self.settings.spike_routine_share),
                "min_history_days": str(self.settings.min_history_days),
            },
            "event_kinds": [asdict(k) for k in EVENT_KINDS],
            "effects": list(EFFECTS),
        }

    def summary(self, q: dict[str, str]) -> dict[str, Any]:
        with self.db() as db:
            sn = self.sys_sn(db)
            if sn is None:
                return {"from": None, "to": None, "kpis": None, "days": [], "anomalies": [],
                        "months": [], "events": [event_json(e) for e in db.events()]}  # fmt: skip
            lo, hi = _day(q.get("from"), "from"), _day(q.get("to"), "to")
            if lo is None and hi is None:
                lo, hi = self._default_range(db, sn, 30)
            if lo and hi and hi < lo:
                raise HttpError(400, "to is before from")
            r = self._analysis(db, sn, lo, hi)
        pv = sum(d.pv_kwh or 0.0 for d in r.days)
        load = sum(d.load_kwh or 0.0 for d in r.days)
        imp = sum(d.grid_import_kwh or 0.0 for d in r.days)
        exp = sum(d.grid_export_kwh or 0.0 for d in r.days)
        recent = [d.baseline_w for d in r.days if d.baseline_w is not None][-7:]
        return {
            "from": lo,
            "to": hi,
            "kpis": {
                "days": len(r.days),
                "pv_kwh": round(pv, 3),
                "load_kwh": round(load, 3),
                "grid_import_kwh": round(imp, 3),
                "grid_export_kwh": round(exp, 3),
                "self_consumption": _ratio(pv - exp, pv),
                "self_sufficiency": None if load <= 0 else max(0.0, min(1.0, 1 - imp / load)),
                "baseline_w": float(statistics.median(recent)) if recent else None,
                "baseline_trend_w_per_30d": r.baseline_trend_w_per_30d,
                "anomalies": len(r.anomalies),
                "anomalies_unexpected": sum(a.expected_by is None for a in r.anomalies),
            },
            "days": [asdict(d) for d in r.days],
            "anomalies": [asdict(a) for a in r.anomalies],
            "months": [asdict(m) for m in r.months],
            "events": [event_json(e) for e in r.events],
        }

    def day(self, q: dict[str, str]) -> dict[str, Any]:
        with self.db() as db:
            sn = self.sys_sn(db)
            first, last = db.day_bounds(sn) if sn else (None, None)
            the_day = _day(q.get("date"), "date") or last
            if sn is None or the_day is None:
                raise HttpError(404, "no data yet")
            samples = db.power_range(sn, the_day, the_day)
            daily = db.daily_range(sn, the_day, the_day)
            r = self._analysis(db, sn, the_day, the_day)
            events = [e for e in db.events() if e.covers(the_day)]
        d = date.fromisoformat(the_day)
        prev = (d - timedelta(days=1)).isoformat()
        nxt = (d + timedelta(days=1)).isoformat()
        totals = None
        if daily:
            t = daily[0]
            totals = {
                "pv_kwh": t.pv_kwh,
                "load_kwh": t.load_kwh,
                "grid_import_kwh": t.grid_import_kwh,
                "grid_export_kwh": t.grid_export_kwh,
                "battery_charge_kwh": t.battery_charge_kwh,
                "battery_discharge_kwh": t.battery_discharge_kwh,
                "self_consumption": r.days[0].self_consumption if r.days else None,
                "self_sufficiency": r.days[0].self_sufficiency if r.days else None,
            }
        return {
            "date": the_day,
            "prev": prev if first and prev >= first else None,
            "next": nxt if last and nxt <= last else None,
            "night_window": {"start": self.settings.night_start, "end": self.settings.night_end},
            "baseline_w": r.days[0].baseline_w if r.days else None,
            "samples": [
                {
                    "t": s.ts[11:16],
                    "pv": s.pv_w,
                    "load": s.load_w,
                    "grid_import": s.grid_import_w,
                    "grid_export": s.grid_export_w,
                    "battery_charge": s.battery_charge_w,
                    "battery_discharge": s.battery_discharge_w,
                    "soc": s.soc,
                }
                for s in samples
            ],
            "totals": totals,
            "anomalies": [asdict(a) for a in r.anomalies],
            "events": [event_json(e) for e in events],
        }

    def heatmap(self, q: dict[str, str]) -> dict[str, Any]:
        with self.db() as db:
            sn = self.sys_sn(db)
            if sn is None:
                return {"from": None, "to": None, "days": [], "hours": list(range(24)),
                        "values": [], "max": None}  # fmt: skip
            lo, hi = _day(q.get("from"), "from"), _day(q.get("to"), "to")
            if lo is None and hi is None:
                lo, hi = self._default_range(db, sn, 60)
            samples = db.power_range(sn, lo, hi)
        sums: dict[str, list[list[float]]] = {}
        for s in samples:
            if s.load_w is None:
                continue
            hours = sums.setdefault(s.day, [[] for _ in range(24)])
            hours[int(s.ts[11:13])].append(s.load_w)
        days = sorted(sums)
        values = [[round(statistics.fmean(h)) if h else None for h in sums[d]] for d in days]
        flat = [v for row in values for v in row if v is not None]
        return {
            "from": lo,
            "to": hi,
            "days": days,
            "hours": list(range(24)),
            "values": values,
            "max": max(flat) if flat else None,
        }

    def live(self) -> dict[str, Any]:
        if not (self.settings.app_id and self.settings.app_secret):
            return {"available": False, "error": "no AlphaESS credentials configured"}
        with self._live_lock:
            now = time.monotonic()
            if self._live and now - self._live[0] < LIVE_TTL_S:
                return self._live[1]
            with self.db() as db:
                sn = self.sys_sn(db)
            if sn is None:
                return {"available": False, "error": "no system known yet: run backfill/daily"}
            client = AlphaESSClient(
                self.settings.app_id,
                self.settings.app_secret,
                base_url=self.settings.base_url,
                transport=self.transport or urllib_transport,
                timeout_s=min(self.settings.timeout_s, 15.0),
                max_retries=0,
            )
            try:
                data = client.get_last_power(sn) or {}
            except ApiError as exc:
                result: dict[str, Any] = {"available": False, "error": str(exc)}
            else:
                result = {
                    "available": True,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "pv_w": data.get("ppv"),
                    "load_w": data.get("pload"),
                    "grid_w": data.get("pgrid"),
                    "battery_w": data.get("pbat"),
                    "soc": data.get("soc"),
                }
            self._live = (now, result)
            return result

    def events(self) -> dict[str, Any]:
        with self.db() as db:
            return {"events": [event_json(e) for e in db.events()]}

    def save_event(self, body: dict[str, Any], event_id: int | None) -> dict[str, Any]:
        try:
            event = validate(
                str(body.get("kind", "")),
                str(body.get("title") or ""),
                str(body.get("start_day", "")),
                str(body["end_day"]) if body.get("end_day") else None,
                str(body["effect"]) if body.get("effect") else None,
                str(body.get("note") or ""),
                event_id,
            )
        except EventError as exc:
            raise HttpError(400, str(exc)) from exc
        with self.db() as db:
            try:
                return {"event": event_json(db.save_event(event))}
            except KeyError as exc:
                raise HttpError(404, "event not found") from exc

    def delete_event(self, event_id: int) -> None:
        with self.db() as db:
            if not db.delete_event(event_id):
                raise HttpError(404, "event not found")

    def report(self, q: dict[str, str]) -> str:
        with self.db() as db:
            sn = self.sys_sn(db)
            if sn is None:
                raise HttpError(404, "no data yet")
            r = self._analysis(db, sn, _day(q.get("from"), "from"), _day(q.get("to"), "to"))
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        return render_report(r, title=self.settings.report_title, generated_at=stamp)


class Handler(BaseHTTPRequestHandler):
    server_version = "energy-analyzer"
    sys_version = ""
    app: App  # set on the subclass by make_server

    # --- plumbing ----------------------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def _headers(self, status: int, ctype: str, length: int, csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, csp if (k == "Content-Security-Policy" and csp) else v)
        self.end_headers()

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, csp: str | None = None) -> None:
        self._headers(200, ctype, len(body), csp)
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = b'{"error":"login required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="energy-analyzer", charset="UTF-8"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        host = self.headers.get("Host", "")
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source or not host:
            return False
        return urllib.parse.urlparse(source).netloc == host

    def _body(self) -> dict[str, Any]:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise HttpError(415, "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "request too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise HttpError(400, "invalid JSON") from exc
        if not isinstance(data, dict):
            raise HttpError(400, "expected a JSON object")
        return data

    def _event_id(self, path: str) -> int:
        tail = path.removeprefix("/api/events/")
        if not tail.isdigit():
            raise HttpError(404, "not found")
        return int(tail)

    def _dispatch(self, method: str) -> None:
        if not self.app.check_auth(self.headers.get("Authorization")):
            self._unauthorized()
            return
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if method != "GET" and not self._same_origin():
                raise HttpError(403, "cross-origin request refused")
            self._route(method, path, q)
        except HttpError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            log.exception("error handling %s %s", method, path)
            self._json(500, {"error": "internal error, see the server log"})

    def _route(self, method: str, path: str, q: dict[str, str]) -> None:
        app = self.app
        if method == "GET":
            if path in STATIC:
                name, ctype = STATIC[path]
                data = (
                    resources.files("energy_analyzer").joinpath("web").joinpath(name).read_bytes()
                )
                self._bytes(data, ctype)
            elif path == "/report.html":
                html = app.report(q).encode("utf-8")
                self._bytes(html, "text/html; charset=utf-8", REPORT_CSP)
            elif path == "/api/meta":
                self._json(200, app.meta())
            elif path == "/api/summary":
                self._json(200, app.summary(q))
            elif path == "/api/day":
                self._json(200, app.day(q))
            elif path == "/api/heatmap":
                self._json(200, app.heatmap(q))
            elif path == "/api/live":
                self._json(200, app.live())
            elif path == "/api/events":
                self._json(200, app.events())
            else:
                raise HttpError(404, "not found")
        elif method == "POST" and path == "/api/events":
            self._json(201, app.save_event(self._body(), None))
        elif method == "PUT" and path.startswith("/api/events/"):
            self._json(200, app.save_event(self._body(), self._event_id(path)))
        elif method == "DELETE" and path.startswith("/api/events/"):
            app.delete_event(self._event_id(path))
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            raise HttpError(405 if path.startswith("/api/") else 404, "not allowed")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    if not is_loopback(host) and not app.auth:
        raise ConfigError(
            f"refusing to serve on {host} without a password: set WEB_PASSWORD in .env "
            "(or listen on 127.0.0.1)"
        )
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
