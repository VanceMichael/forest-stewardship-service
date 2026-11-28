"""HTTP 路由与请求装配（标准库 ThreadingHTTPServer）。"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import handlers
from .database import connect, init_db
from .errors import AppError, unauthorized
from .fund import FundAdapter
from .seed import seed
from .timeutil import Clock
from .util import Context

# (method, path, 处理函数, 路径参数名)
ROUTES: list[tuple] = [
    ("GET", "/health", None, None),
    ("GET", "/parties", handlers.list_parties, None),
    ("GET", "/contracts", handlers.list_contracts, None),
    ("GET", "/units", handlers.list_units, None),
    ("GET", "/units/{unit_id}", handlers.get_unit_detail, ["unit_id"]),
    ("GET", "/units/{unit_id}/trace", handlers.unit_trace, ["unit_id"]),
    ("GET", "/contracts/{contract_id}/ledger", handlers.contract_ledger, ["contract_id"]),
    ("GET", "/events", handlers.list_events, None),
    ("GET", "/notifications", handlers.list_notifications, None),
    ("POST", "/notifications/ack", handlers.ack_notification, None),

    ("POST", "/declarations", handlers.create_declaration, None),
    ("POST", "/plantings", handlers.create_planting, None),
    ("GET", "/declarations/{declaration_id}", handlers.get_declaration_detail, ["declaration_id"]),
    ("POST", "/surveys", handlers.add_survey, None),
    ("POST", "/acceptances", handlers.create_acceptance, None),

    ("POST", "/patrol-batches", handlers.create_patrol_batch, None),
    ("GET", "/defects", handlers.list_defects, None),
    ("POST", "/defects/cause", handlers.set_defect_cause, None),
    ("POST", "/replants", handlers.order_replant, None),
    ("POST", "/replants/perform", handlers.perform_replant, None),
    ("POST", "/replants/verify", handlers.verify_replant, None),

    ("POST", "/takeovers", handlers.create_takeover, None),
    ("POST", "/takeovers/rehandover", handlers.rehandover, None),
    ("POST", "/facility-inspections", handlers.inspect_facility, None),

    ("POST", "/corrections/wrong-plot", handlers.wrong_plot_correction, None),
    ("POST", "/corrections/boundary", handlers.correct_unit_boundary, None),
    ("GET", "/holds", handlers.list_holds, None),
    ("POST", "/holds/resolve", handlers.resolve_hold, None),

    ("GET", "/payments/eligible", handlers.eligible_payments, None),
    ("POST", "/payments", handlers.create_payment, None),
    ("GET", "/payments", handlers.list_payments, None),
    ("POST", "/payments/dispatch", handlers.dispatch_payment, None),
    ("POST", "/payments/sync", handlers.sync_payment, None),
    ("POST", "/fund/trace", handlers.fund_trace, None),
    ("POST", "/recoveries", handlers.propose_recovery, None),
    ("POST", "/recoveries/confirm", handlers.confirm_recovery, None),

    ("POST", "/settlements/run", handlers.run_settlement, None),

    ("GET", "/admin/clock", handlers.get_clock, None),
    ("POST", "/admin/clock", handlers.set_clock, None),
    ("DELETE", "/admin/clock", handlers.clear_clock, None),
]

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def match_route(method: str, path: str):
    for m, pattern, fn, params in ROUTES:
        if m != method:
            continue
        if "{" not in pattern:
            if path == pattern:
                return fn, {}
            continue
        prefix, suffix = pattern.split("{")
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        param_name, tail = suffix.split("}", 1)
        if tail and not rest.endswith(tail):
            continue
        value = rest[:-len(tail)] if tail else rest
        if not value or "/" in value:
            continue
        return fn, {param_name: value}
    return None, None


def parse_query(raw: str) -> dict:
    out = {}
    if not raw:
        return out
    for part in raw.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ForestContract/1.0"

    def _send(self, status: int, body):
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        if self.server.access_log:
            super().log_message(*args)

    def _handle(self, method: str):
        path, _, query_raw = self.path.partition("?")
        query = parse_query(query_raw)
        if path == "/health" and method == "GET":
            return self._send(200, {"status": "ok", "service": "forest-contracts",
                                    "synthetic": True})
        fn, path_params = match_route(method, path)
        if fn is None:
            return self._send(404, {"error": {"code": "not_found", "message": f"接口不存在: {method} {path}"}})
        query.update(path_params)
        payload = {}
        if method in WRITE_METHODS:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                try:
                    payload = json.loads(self.rfile.read(length).decode())
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": {"code": "invalid_body", "message": "请求体不是合法 JSON"}})
            if not isinstance(payload, dict):
                return self._send(400, {"error": {"code": "invalid_body", "message": "请求体必须为 JSON 对象"}})
        db = connect(self.server.db_path)
        try:
            principal = self.headers.get("X-Principal")
            party = None
            if principal:
                row = db.execute(
                    "SELECT p.* FROM principals m JOIN parties p ON p.id=m.party_id "
                    "WHERE m.principal=?", (principal,)).fetchone()
                if not row:
                    raise AppError(401, "unknown_principal", f"未知演练身份: {principal}")
                party = row
            elif method in WRITE_METHODS:
                raise unauthorized()
            clock = Clock(db, self.headers.get("X-Now"))
            ctx = Context(db, clock, principal, party, self.server.fund)
            db.execute("BEGIN IMMEDIATE")
            try:
                result = fn(ctx, payload, query)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            self._send(200, result if result is not None else {"ok": True})
        except AppError as exc:
            self._send(exc.status, exc.to_body())
        except sqlite3.IntegrityError as exc:
            self._send(409, {"error": {"code": "integrity_conflict", "message": str(exc)}})
        except Exception as exc:  # 兜底：业务异常不应断连
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})
        finally:
            db.close()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def build_server(db_path: str, fund_base_url: str | None, host: str, port: int,
                 access_log: bool = False, do_seed: bool = True) -> ThreadingHTTPServer:
    db = connect(db_path)
    init_db(db)
    if do_seed:
        seed(db)
    db.close()
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path
    server.fund = FundAdapter(fund_base_url)
    server.access_log = access_log
    return server
