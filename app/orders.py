"""虚构订单数据：平台唯一的原始数据源，计算器只从这里取数。

使用固定随机种子保证可复现；另手工注入若干边界订单（跨月支付、月末下单、
全额退款、赠品等），使两种口径的差异可被逐行解释，而不是偶然抖动。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

from .db import audit

CHANNELS = ("自营商城", "天猫旗舰店", "抖音直播", "线下门店")
REGIONS = ("华东", "华南", "华北", "西南")
CATEGORIES = ("数码", "家电", "服饰", "食品")
STATUSES_PAID = ("paid", "refunded", "partial_refund")
ALL_STATUSES = ("pending", "cancelled", "paid", "refunded", "partial_refund")

# 确定性种子：任何人重建数据库得到同一批订单
SEED = 20261117


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def generate_orders() -> list[dict]:
    rnd = random.Random(SEED)
    orders: list[dict] = []
    start = date(2026, 9, 1)
    # 9/1 ~ 11/15 每天 8~14 单
    day = start
    seq = 1
    while day <= date(2026, 11, 15):
        for _ in range(rnd.randint(8, 14)):
            order_dt = datetime(day.year, day.month, day.day,
                                rnd.randint(0, 23), rnd.randint(0, 59), rnd.randint(0, 59))
            status = rnd.choices(
                ALL_STATUSES, weights=[8, 7, 55, 12, 18], k=1
            )[0]
            amount = round(rnd.uniform(29, 4999), 2)
            pay_time = None
            pay_amount = 0.0
            refund_amount = 0.0
            if status in STATUSES_PAID:
                # 92% 当日支付，8% 次日支付（产生跨日；月末批次里再手工构造跨月）
                pay_day = day if rnd.random() < 0.92 else day + timedelta(days=1)
                pay_dt = datetime(pay_day.year, pay_day.month, pay_day.day,
                                  rnd.randint(0, 23), rnd.randint(0, 59), rnd.randint(0, 59))
                pay_time = _iso(pay_dt)
                pay_amount = amount
                if status == "refunded":
                    refund_amount = amount
                elif status == "partial_refund":
                    refund_amount = round(amount * rnd.uniform(0.1, 0.6), 2)
            is_gift = 1 if rnd.random() < 0.06 else 0
            orders.append({
                "order_id": f"SO{seq:05d}",
                "order_time": _iso(order_dt),
                "pay_time": pay_time,
                "status": status,
                "channel": rnd.choice(CHANNELS),
                "region": rnd.choice(REGIONS),
                "category": rnd.choice(CATEGORIES),
                "amount": amount,
                "pay_amount": pay_amount,
                "refund_amount": refund_amount,
                "is_gift": is_gift,
                "customer_id": f"C{rnd.randint(1, 220):04d}",
            })
            seq += 1
        day += timedelta(days=1)

    # ---- 手工边界订单：保证口径差异有可讲解的确定性案例 ----
    boundary = [
        # 9/30 深夜下单，10/1 凌晨支付：order_time 归属 9 月，pay_time 归属 10 月
        dict(order_time="2026-09-30 23:18:00", pay_time="2026-10-01 00:42:00",
             status="paid", channel="自营商城", region="华东", category="数码",
             amount=3299.00, pay_amount=3299.00, refund_amount=0.0, is_gift=0),
        # 10/31 深夜下单，11/1 支付：10 月与 11 月的归属分歧
        dict(order_time="2026-10-31 22:55:00", pay_time="2026-11-01 09:05:00",
             status="paid", channel="抖音直播", region="华南", category="家电",
             amount=1599.00, pay_amount=1599.00, refund_amount=0.0, is_gift=0),
        # 已下单未支付：按支付时间归属时不属于任何期间
        dict(order_time="2026-10-15 14:00:00", pay_time=None,
             status="pending", channel="线下门店", region="华北", category="服饰",
             amount=399.00, pay_amount=0.0, refund_amount=0.0, is_gift=0),
        # 取消单
        dict(order_time="2026-10-20 10:30:00", pay_time=None,
             status="cancelled", channel="天猫旗舰店", region="西南", category="食品",
             amount=89.90, pay_amount=0.0, refund_amount=0.0, is_gift=0),
        # 全额退款：net_paid 贡献为 0，但 gross_paid / row_count 仍计入
        dict(order_time="2026-10-08 16:12:00", pay_time="2026-10-08 17:01:00",
             status="refunded", channel="天猫旗舰店", region="华东", category="家电",
             amount=2299.00, pay_amount=2299.00, refund_amount=2299.00, is_gift=0),
        # 赠品单：v2 口径过滤 is_gift 时会被排除
        dict(order_time="2026-10-12 11:20:00", pay_time="2026-10-12 12:00:00",
             status="paid", channel="自营商城", region="华南", category="食品",
             amount=1.00, pay_amount=1.00, refund_amount=0.0, is_gift=1),
        # 线下门店单：v2 订单数排除线下渠道时被排除
        dict(order_time="2026-09-18 19:40:00", pay_time="2026-09-18 20:05:00",
             status="paid", channel="线下门店", region="华北", category="服饰",
             amount=699.00, pay_amount=699.00, refund_amount=0.0, is_gift=0),
    ]
    for b in boundary:
        b["order_id"] = f"SO{seq:05d}"
        b["customer_id"] = f"C{seq % 220:04d}"
        orders.append(b)
        seq += 1
    return orders


def load_orders(conn: sqlite3.Connection) -> int:
    """建表并写入虚构订单（幂等：先清空再写入）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id      TEXT PRIMARY KEY,
            order_time    TEXT NOT NULL,
            pay_time      TEXT,
            status        TEXT NOT NULL,
            channel       TEXT NOT NULL,
            region        TEXT NOT NULL,
            category      TEXT NOT NULL,
            amount        REAL NOT NULL,
            pay_amount    REAL NOT NULL,
            refund_amount REAL NOT NULL,
            is_gift       INTEGER NOT NULL,
            customer_id   TEXT NOT NULL
        )
    """)
    conn.execute("DELETE FROM orders")
    rows = generate_orders()
    conn.executemany("""
        INSERT INTO orders(order_id, order_time, pay_time, status, channel, region,
                           category, amount, pay_amount, refund_amount, is_gift, customer_id)
        VALUES (:order_id,:order_time,:pay_time,:status,:channel,:region,
                :category,:amount,:pay_amount,:refund_amount,:is_gift,:customer_id)
    """, rows)
    conn.commit()
    audit(conn, "orders", "orders", "load", {"count": len(rows), "seed": SEED}, actor="初始化脚本")
    return len(rows)


def fetch_orders(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY order_id")]
