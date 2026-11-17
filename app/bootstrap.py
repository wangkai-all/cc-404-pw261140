"""系统初始化：虚构订单 + 指标/口径/报表/引擎种子数据 + 旧口径首批产物。

运行后系统处于“v1 已批准、全部报表已出数”的基线状态；
v2 候选口径通过网页或 API 登记，以演示完整的影响分析与双算流程。
"""
from __future__ import annotations

import sqlite3

from . import registry
from .calculator import compute_product
from .db import audit, get_conn, init_schema, utcnow
from .orders import load_orders
from .periods import SEED_PERIODS


def _seed_metrics(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM metrics").fetchone():
        return
    conn.executemany("""
        INSERT INTO metrics(metric_id, name, description, unit, default_formula, created_at)
        VALUES (?,?,?,?,?,?)
    """, [
        ("gmv", "成交总额 GMV", "已支付订单的实付净额（支付-退款），按下单时间归属", "元",
         "net_paid", utcnow()),
        ("order_count", "有效订单数", "满足有效条件的订单行数，每单计 1", "单",
         "row_count", utcnow()),
    ])


def _seed_calibers(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM calibers").fetchone():
        return
    # ---- 旧口径 v1（基线，直接批准） ----
    conn.execute("""
        INSERT INTO calibers(caliber_id, metric_id, version, revision, status, description,
                             time_attr, filters_json, formula, parent_caliber_id, change_note,
                             created_at, approved_at)
        VALUES ('gmv-v1','gmv','v1',1,'approved',
                'GMV 旧口径：已支付且非取消/待付，按下单时间归属，实付净额',
                'order_time',
                '[{"field":"status","op":"in","values":["paid","refunded","partial_refund"]}]',
                'net_paid', NULL, '基线口径', ?, ?)
    """, (utcnow(), utcnow()))
    conn.execute("""
        INSERT INTO calibers(caliber_id, metric_id, version, revision, status, description,
                             time_attr, filters_json, formula, parent_caliber_id, change_note,
                             created_at, approved_at)
        VALUES ('order_count-v1','order_count','v1',1,'approved',
                '订单数旧口径：有效订单（已支付三态），按下单时间，每单计 1',
                'order_time',
                '[{"field":"status","op":"in","values":["paid","refunded","partial_refund"]}]',
                'row_count', NULL, '基线口径', ?, ?)
    """, (utcnow(), utcnow()))


def _seed_engines(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM engines").fetchone():
        return
    conn.executemany(
        "INSERT INTO engines(engine_id, name, status) VALUES (?,?, 'active')",
        [(registry.ENGINE_MAIN, "主计算器（服务已批准口径）"),
         (registry.ENGINE_CANDIDATE, "候选计算器（服务候选口径双算）")])
    for cid in ("gmv-v1", "order_count-v1"):
        conn.execute("INSERT INTO engine_calibers(engine_id, caliber_id) VALUES (?,?)",
                     (registry.ENGINE_MAIN, cid))


def _seed_reports(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM reports").fetchone():
        return
    conn.executemany("""
        INSERT INTO reports(report_id, name, description, metric_id, period_grain,
                            breakdown_dim, upgrade_policy, delayed_until, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, [
        ("rpt-gmv-overview", "GMV 总览月报", "全渠道月度 GMV，双算过渡，保留新旧两版可对比",
         "gmv", "month", "none", "dual", None, utcnow()),
        ("rpt-gmv-channel", "渠道 GMV 月报", "按渠道拆分的 GMV，整版替换，升级后只展示新口径",
         "gmv", "month", "channel", "whole", None, utcnow()),
        ("rpt-gmv-region", "大区 GMV 月报", "按大区分拆的 GMV，业务侧申请延后升级",
         "gmv", "month", "region", "delayed", "2026-12-31", utcnow()),
        ("rpt-oc-channel", "渠道有效订单数月报", "按渠道拆分的有效订单数，双算过渡",
         "order_count", "month", "channel", "dual", None, utcnow()),
    ])


def bootstrap(db_path: str | None = None) -> dict:
    conn = get_conn(db_path)
    init_schema(conn)
    n_orders = load_orders(conn)
    _seed_metrics(conn)
    _seed_calibers(conn)
    _seed_engines(conn)
    _seed_reports(conn)
    conn.commit()

    # 基线产物：每个报表 × 每个种子期间 × v1 口径，由主计算器加工
    product_ids = []
    reports = registry.list_reports(conn)
    for r in reports:
        approved = registry.latest_approved(conn, r["metric_id"])
        for p in SEED_PERIODS:
            product_ids.append(compute_product(
                conn, r["report_id"], approved["caliber_id"], p,
                engine_id=registry.ENGINE_MAIN))
    audit(conn, "system", "bootstrap", "bootstrap",
          {"orders": n_orders, "baseline_products": len(product_ids)})
    conn.commit()
    conn.close()
    return {"orders": n_orders, "baseline_products": len(product_ids)}


if __name__ == "__main__":
    import json
    print(json.dumps(bootstrap(), ensure_ascii=False, indent=2))
