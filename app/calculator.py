"""指标计算器：从 orders 原始表取数，按口径逐订单加工，产物与逐行贡献落库。

设计约束：
- 公式实现只存在于 rules.py，本模块只负责调度、分组与持久化；
- 每个订单对每个产物都留下一行 contributions（含被排除订单及原因），
  使网页可以展示逐行贡献差异；
- 重算采用“先删后建”，同一 (报表,口径,期间) 永远只有一份当前结果。
"""
from __future__ import annotations

import sqlite3

from . import rules
from .db import audit, utcnow
from .orders import fetch_orders
from .periods import Period


def _group_key(order: dict, breakdown_dim: str) -> str:
    if breakdown_dim == "none":
        return "全部"
    return str(order.get(breakdown_dim) or "未知")


def get_spec(conn: sqlite3.Connection, caliber_id: str) -> rules.CaliberSpec:
    row = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if row is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    spec = rules.CaliberSpec.from_row(row)
    rules.validate_spec(spec)
    return spec


def compute_product(conn: sqlite3.Connection, report_id: str, caliber_id: str,
                    period: Period, engine_id: str = "engine-main",
                    mark_impact: int | None = None) -> int:
    """执行一次完整计算，返回 product_id。重复调用即重算。"""
    report = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if report is None:
        raise KeyError(f"报表不存在: {report_id}")
    spec = get_spec(conn, caliber_id)
    if spec.metric_id != report["metric_id"]:
        raise ValueError(f"口径 {caliber_id} 的指标与报表 {report_id} 依赖的指标不一致，禁止跨指标计算")

    orders = fetch_orders(conn)
    # 先删旧产物（连带 contributions 级联删除）
    old = conn.execute(
        "SELECT product_id FROM products WHERE report_id=? AND caliber_id=? AND period_start=? AND period_end=?",
        (report_id, caliber_id, period.start, period.end),
    ).fetchone()
    if old:
        conn.execute("DELETE FROM products WHERE product_id=?", (old["product_id"],))

    cur = conn.execute("""
        INSERT INTO products(report_id, caliber_id, metric_id, period_start, period_end,
                             total, calc_status, engine_id, inputs_differ, row_count, computed_at)
        VALUES (?,?,?,?,?,?, 'computed', ?, 0, ?, ?)
    """, (report_id, caliber_id, spec.metric_id, period.start, period.end,
          0.0, engine_id, 0, utcnow()))
    product_id = cur.lastrowid

    total = 0.0
    included_rows = 0
    contrib_rows = []
    for order in orders:
        ev = rules.evaluate_order(order, spec, period.start, period.end)
        gk = _group_key(order, report["breakdown_dim"])
        contrib_rows.append((product_id, order["order_id"], gk,
                             1 if ev["included"] else 0, ev["value"], ev["reason"]))
        if ev["included"]:
            total += ev["value"]
            included_rows += 1

    conn.executemany(
        "INSERT INTO contributions(product_id, order_id, group_key, included, value, reason) "
        "VALUES (?,?,?,?,?,?)", contrib_rows)
    total = round(total, 2)
    conn.execute("UPDATE products SET total=?, row_count=? WHERE product_id=?",
                 (total, included_rows, product_id))

    if mark_impact is not None:
        conn.execute(
            "UPDATE impacts SET status='recomputed', product_id=?, resolved_at=? WHERE impact_id=?",
            (product_id, utcnow(), mark_impact))

    audit(conn, "product", str(product_id), "compute", {
        "report_id": report_id, "caliber_id": caliber_id,
        "period": period.label, "total": total, "rows_scanned": len(orders),
        "rows_included": included_rows, "engine_id": engine_id,
    })
    conn.commit()
    return product_id


def product_groups(conn: sqlite3.Connection, product_id: int) -> list[dict]:
    """产物按报表维度拆分后的分组汇总（网页报表视图用）。"""
    return [dict(r) for r in conn.execute("""
        SELECT group_key,
               SUM(included)              AS included_rows,
               ROUND(SUM(CASE WHEN included=1 THEN value ELSE 0 END), 2) AS subtotal,
               SUM(CASE WHEN included=0 THEN 1 ELSE 0 END) AS excluded_rows
        FROM contributions WHERE product_id=?
        GROUP BY group_key ORDER BY group_key
    """, (product_id,))]


def list_products(conn: sqlite3.Connection, report_id: str | None = None) -> list[dict]:
    sql = """
        SELECT p.*, r.name AS report_name, c.version AS caliber_version,
               c.status AS caliber_status, m.name AS metric_name, m.unit
        FROM products p
        JOIN reports r ON r.report_id = p.report_id
        JOIN calibers c ON c.caliber_id = p.caliber_id
        JOIN metrics m ON m.metric_id = p.metric_id
    """
    args: tuple = ()
    if report_id:
        sql += " WHERE p.report_id=?"
        args = (report_id,)
    sql += " ORDER BY p.report_id, p.period_start, c.version"
    return [dict(r) for r in conn.execute(sql, args)]
