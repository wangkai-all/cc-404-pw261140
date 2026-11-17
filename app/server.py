"""HTTP 服务：JSON API + 前端静态资源。仅依赖标准库。"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import bootstrap as boot
from . import diff as diff_mod
from . import registry
from .calculator import compute_product, list_products, product_groups
from .db import DB_PATH, get_conn, init_schema
from .orders import fetch_orders
from .periods import parse_period
from .rules import DIMENSION_FIELDS, FORMULAS, TIME_ATTR_FIELDS

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
_LOCK = threading.Lock()


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"请求体不是合法 JSON: {e}")


class Handler(BaseHTTPRequestHandler):
    server_version = "CaliberPlatform/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------------------------------------------------------- 响应工具
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel: str, ctype: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send(404, {"error": "not found"})
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- 路由
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                return self._send_file("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._send_file("app.js", "application/javascript; charset=utf-8")
            if path == "/styles.css":
                return self._send_file("styles.css", "text/css; charset=utf-8")
            with _LOCK, get_conn() as conn:
                if path == "/api/overview":
                    return self._send(200, self._overview(conn))
                if path == "/api/metrics":
                    return self._send(200, registry.list_metrics(conn))
                if path == "/api/calibers":
                    return self._send(200, registry.list_calibers(conn, qs.get("metric", [None])[0]))
                if path == "/api/reports":
                    return self._send(200, registry.list_reports(conn))
                if path == "/api/engines":
                    return self._send(200, registry.list_engines(conn))
                if path == "/api/products":
                    return self._send(200, list_products(conn, qs.get("report", [None])[0]))
                if path == "/api/impacts":
                    return self._send(200, registry.list_impacts(conn, qs.get("caliber", [None])[0]))
                if path == "/api/orders":
                    return self._send(200, self._orders(conn, qs))
                if path == "/api/order-count":
                    return self._send(200, {"count": conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]})
                if path == "/api/audit":
                    return self._send(200, registry.audit_trail(conn, int(qs.get("limit", ["100"])[0])))
                if path == "/api/meta":
                    return self._send(200, {"dimension_fields": DIMENSION_FIELDS,
                                            "time_attr_fields": TIME_ATTR_FIELDS,
                                            "formulas": FORMULAS})
                if path == "/api/report-detail":
                    return self._send(200, self._report_detail(conn, qs))
                if path == "/api/diff":
                    return self._send(200, diff_mod.product_diff(
                        conn, int(qs["a"][0]), int(qs["b"][0])))
                if path == "/api/simulate/withdraw":
                    return self._send(200, registry.simulate_withdraw(conn, qs["caliber"][0]))
                if path == "/api/simulate/retire":
                    return self._send(200, registry.simulate_engine_retire(conn, qs["engine"][0]))
            self._send(404, {"error": f"未知路径 {path}"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = _json_body(self)
            with _LOCK, get_conn() as conn:
                if path == "/api/calibers":
                    res = registry.register_candidate(
                        conn, body["metric_id"], body["version"], body["description"],
                        body["time_attr"], body.get("filters", []), body["formula"],
                        body.get("change_note", ""))
                elif path == "/api/calibers/amend":
                    res = registry.amend_candidate(
                        conn, body["caliber_id"], body.get("time_attr"),
                        body.get("filters"), body.get("formula"), body.get("change_note", ""))
                elif path == "/api/calibers/approve":
                    res = registry.approve(conn, body["caliber_id"], body.get("actor", "口径管理员"))
                elif path == "/api/calibers/withdraw":
                    res = registry.withdraw(conn, body["caliber_id"], body.get("actor", "口径管理员"))
                elif path == "/api/reports/delay":
                    res = registry.delay_report(conn, body["report_id"], body["until"])
                elif path == "/api/reports/lift-delay":
                    res = registry.lift_delay(conn, body["report_id"])
                elif path == "/api/recompute":
                    res = {"product_ids": registry.recompute_stale(conn)}
                elif path == "/api/compute":
                    p = parse_period(body["period"])
                    pid = compute_product(conn, body["report_id"], body["caliber_id"], p)
                    res = {"product_id": pid}
                elif path == "/api/engines/retire":
                    res = registry.retire_engine(conn, body["engine_id"])
                elif path == "/api/engines/reactivate":
                    res = registry.reactivate_engine(conn, body["engine_id"])
                elif path == "/api/bootstrap":
                    conn.close()
                    res = boot.bootstrap()
                else:
                    return self._send(404, {"error": f"未知路径 {path}"})
                self._send(200, {"ok": True, "data": res})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"ok": False, "error": str(e)})

    # ---------------------------------------------------------- 聚合视图
    @staticmethod
    def _overview(conn) -> dict:
        return {
            "metrics": registry.list_metrics(conn),
            "calibers": registry.list_calibers(conn),
            "reports": registry.list_reports(conn),
            "products": list_products(conn),
            "impacts": registry.list_impacts(conn),
            "engines": registry.list_engines(conn),
            "order_count": conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"],
        }

    @staticmethod
    def _report_detail(conn, qs) -> dict:
        report_id = qs["report"][0]
        report = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if report is None:
            raise KeyError("报表不存在")
        out = {"report": dict(report), "periods": []}
        prods = conn.execute(
            "SELECT * FROM products WHERE report_id=? ORDER BY period_start, caliber_id",
            (report_id,)).fetchall()
        for p in prods:
            d = dict(p)
            d["groups"] = product_groups(conn, p["product_id"])
            d["caliber_version"] = conn.execute(
                "SELECT version FROM calibers WHERE caliber_id=?", (p["caliber_id"],)).fetchone()["version"]
            out["periods"].append(d)
        return out

    @staticmethod
    def _orders(conn, qs) -> list[dict]:
        f = {k: v[0] for k, v in qs.items() if k in
             ("status", "channel", "region", "category", "is_gift", "order_id",
              "date_field", "date_from", "date_to")}
        return diff_mod.query_orders(conn, f, qs.get("limit", ["200"])[0])


def ensure_bootstrapped() -> None:
    conn = get_conn()
    init_schema(conn)
    need = conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()["n"] == 0
    conn.close()
    if need:
        boot.bootstrap()


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    ensure_bootstrapped()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"口径双算平台已启动: http://{host}:{port}  (DB: {DB_PATH})")
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    serve(args.host, args.port)
