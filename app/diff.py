"""逐行贡献差异：把同一报表、同一期间下两个口径的逐订单贡献并排对账。"""
from __future__ import annotations

import sqlite3


def find_pair_products(conn: sqlite3.Connection, report_id: str, period_start: str,
                       period_end: str) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT p.product_id, p.caliber_id, c.version, c.status, p.total, p.calc_status,
               p.inputs_differ, p.row_count, p.computed_at
        FROM products p JOIN calibers c ON c.caliber_id = p.caliber_id
        WHERE p.report_id=? AND p.period_start=? AND p.period_end=?
        ORDER BY c.created_at
    """, (report_id, period_start, period_end))]


def _contrib_map(conn: sqlite3.Connection, product_id: int) -> dict[str, dict]:
    return {r["order_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM contributions WHERE product_id=?", (product_id,))}


def product_diff(conn: sqlite3.Connection, product_a: int, product_b: int) -> dict:
    pa = conn.execute("SELECT * FROM products WHERE product_id=?", (product_a,)).fetchone()
    pb = conn.execute("SELECT * FROM products WHERE product_id=?", (product_b,)).fetchone()
    if pa is None or pb is None:
        raise KeyError("产物不存在")
    ca = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (pa["caliber_id"],)).fetchone()
    cb = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (pb["caliber_id"],)).fetchone()
    ma, mb = _contrib_map(conn, product_a), _contrib_map(conn, product_b)
    orders = {r["order_id"]: dict(r) for r in conn.execute("SELECT * FROM orders")}

    rows = []
    sum_a = sum_b = 0.0
    same = added = removed = changed = excluded_diff = 0
    for oid in sorted(set(ma) | set(mb)):
        a, b = ma.get(oid), mb.get(oid)
        o = orders[oid]
        va = a["value"] if a and a["included"] else 0.0
        vb = b["value"] if b and b["included"] else 0.0
        sum_a += va
        sum_b += vb
        in_a = bool(a and a["included"])
        in_b = bool(b and b["included"])
        if in_a and in_b and abs(va - vb) < 0.005:
            kind = "same"
            same += 1
        elif in_a and not in_b:
            kind = "removed"      # 旧口径计入、候选口径移除
            removed += 1
        elif not in_a and in_b:
            kind = "added"        # 候选口径新增计入
            added += 1
        elif in_a and in_b:
            kind = "changed"      # 两口径都计入但贡献值不同（公式差异）
            changed += 1
        else:
            kind = "excluded_both"
            excluded_diff += 1
        rows.append({
            "order_id": oid,
            "kind": kind,
            "in_a": in_a, "in_b": in_b,
            "value_a": round(va, 2), "value_b": round(vb, 2),
            "delta": round(vb - va, 2),
            "reason_a": a["reason"] if a else "无记录",
            "reason_b": b["reason"] if b else "无记录",
            "order_time": o["order_time"], "pay_time": o["pay_time"],
            "status": o["status"], "channel": o["channel"], "region": o["region"],
            "category": o["category"], "is_gift": o["is_gift"],
            "pay_amount": o["pay_amount"], "refund_amount": o["refund_amount"],
        })

    bridge = [
        {"stage": f"旧口径 {ca['version']} 合计", "amount": round(sum_a, 2)},
        {"stage": "候选口径移除订单贡献", "amount": round(-sum(r["value_a"] for r in rows if r["kind"] == "removed"), 2)},
        {"stage": "候选口径新增订单贡献", "amount": round(sum(r["value_b"] for r in rows if r["kind"] == "added"), 2)},
        {"stage": "同单公式差异合计", "amount": round(sum(r["delta"] for r in rows if r["kind"] == "changed"), 2)},
        {"stage": f"候选口径 {cb['version']} 合计", "amount": round(sum_b, 2)},
    ]
    # 桥接校验：四段必须闭合
    closed = abs(bridge[0]["amount"] + bridge[1]["amount"]
                 + bridge[2]["amount"] + bridge[3]["amount"] - bridge[4]["amount"]) < 0.01

    return {
        "product_a": {"id": product_a, "caliber_id": pa["caliber_id"], "version": ca["version"],
                      "total_stored": pa["total"], "total_rebuilt": round(sum_a, 2)},
        "product_b": {"id": product_b, "caliber_id": pb["caliber_id"], "version": cb["version"],
                      "total_stored": pb["total"], "total_rebuilt": round(sum_b, 2)},
        "summary": {"same": same, "removed": removed, "added": added,
                    "changed": changed, "excluded_both": excluded_diff, "total_orders": len(rows)},
        "bridge": bridge, "bridge_closed": closed,
        "rows": rows,
    }


def query_orders(conn: sqlite3.Connection, filters: dict | None = None,
                 limit: int = 200) -> list[dict]:
    """原始订单查询：支持状态/渠道/地区/品类/赠品/期间（按下单或支付时间）过滤。"""
    filters = filters or {}
    where, args = [], []
    for col in ("status", "channel", "region", "category"):
        if filters.get(col):
            where.append(f"{col}=?")
            args.append(filters[col])
    if filters.get("is_gift") in (0, 1, "0", "1"):
        where.append("is_gift=?")
        args.append(int(filters["is_gift"]))
    if filters.get("order_id"):
        where.append("order_id LIKE ?")
        args.append(f"%{filters['order_id']}%")
    date_field = filters.get("date_field", "order_time")
    if date_field not in ("order_time", "pay_time"):
        date_field = "order_time"
    if filters.get("date_from"):
        where.append(f"COALESCE({date_field},'') >= ?")
        args.append(filters["date_from"])
    if filters.get("date_to"):
        where.append(f"COALESCE({date_field},'') <= ?")
        args.append(filters["date_to"] + " 23:59:59")
    sql = "SELECT * FROM orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY order_id LIMIT ?"
    args.append(min(int(limit or 200), 1000))
    return [dict(r) for r in conn.execute(sql, args)]
