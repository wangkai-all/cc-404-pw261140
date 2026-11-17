"""SQLite 连接与建表。所有模块共用同一 schema，禁止各自建表。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "METRICS_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "metrics.db")
)

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 指标：一个业务指标一行（GMV、订单数……）
CREATE TABLE IF NOT EXISTS metrics (
    metric_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    unit            TEXT NOT NULL,
    default_formula TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 口径：同一指标的不同计算口径版本（v1 旧口径 / v2 候选口径……）
CREATE TABLE IF NOT EXISTS calibers (
    caliber_id         TEXT PRIMARY KEY,
    metric_id          TEXT NOT NULL REFERENCES metrics(metric_id),
    version            TEXT NOT NULL,
    revision           INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL CHECK (status IN ('candidate','approved','superseded','withdrawn')),
    description        TEXT NOT NULL,
    time_attr          TEXT NOT NULL CHECK (time_attr IN ('order_time','pay_time')),
    filters_json       TEXT NOT NULL DEFAULT '[]',
    formula            TEXT NOT NULL,
    parent_caliber_id  TEXT REFERENCES calibers(caliber_id),
    change_note        TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    approved_at        TEXT,
    withdrawn_at       TEXT,
    UNIQUE(metric_id, version)
);

-- 计算引擎：计算器实例与可服务口径的映射，用于“计算器退出”演练
CREATE TABLE IF NOT EXISTS engines (
    engine_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active','retired')),
    retired_at    TEXT,
    retire_reason TEXT
);
CREATE TABLE IF NOT EXISTS engine_calibers (
    engine_id  TEXT NOT NULL REFERENCES engines(engine_id),
    caliber_id TEXT NOT NULL REFERENCES calibers(caliber_id),
    PRIMARY KEY (engine_id, caliber_id)
);

-- 报表（消费端）：声明依赖哪个指标、维度拆分方式、升级策略
CREATE TABLE IF NOT EXISTS reports (
    report_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT NOT NULL,
    metric_id        TEXT NOT NULL REFERENCES metrics(metric_id),
    period_grain     TEXT NOT NULL DEFAULT 'month',
    breakdown_dim    TEXT NOT NULL DEFAULT 'none',
    upgrade_policy   TEXT NOT NULL CHECK (upgrade_policy IN ('dual','whole','delayed')),
    delayed_until    TEXT,
    created_at       TEXT NOT NULL
);

-- 产物：报表 × 期间 × 口径 的一次计算结果
CREATE TABLE IF NOT EXISTS products (
    product_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     TEXT NOT NULL REFERENCES reports(report_id),
    caliber_id    TEXT NOT NULL REFERENCES calibers(caliber_id),
    metric_id     TEXT NOT NULL REFERENCES metrics(metric_id),
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    total         REAL NOT NULL DEFAULT 0,
    calc_status   TEXT NOT NULL CHECK (calc_status IN ('computed','stale','withdrawn','superseded')),
    engine_id     TEXT REFERENCES engines(engine_id),
    inputs_differ INTEGER NOT NULL DEFAULT 0,
    row_count     INTEGER NOT NULL DEFAULT 0,
    computed_at   TEXT NOT NULL,
    UNIQUE(report_id, caliber_id, period_start, period_end)
);

-- 逐订单贡献：全量订单逐行评估（included=0 记录被排除原因），支撑逐行差异
CREATE TABLE IF NOT EXISTS contributions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    order_id    TEXT NOT NULL,
    group_key   TEXT NOT NULL,
    included    INTEGER NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL,
    UNIQUE(product_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_contrib_product ON contributions(product_id);

-- 影响登记：口径变化后自动找出的需重算产物
CREATE TABLE IF NOT EXISTS impacts (
    impact_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    caliber_id     TEXT NOT NULL REFERENCES calibers(caliber_id),
    report_id      TEXT NOT NULL REFERENCES reports(report_id),
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    product_id     INTEGER REFERENCES products(product_id) ON DELETE SET NULL,
    change_types   TEXT NOT NULL,            -- 逗号分隔，如 time_attr,filter:status
    comparable     INTEGER NOT NULL,         -- 同窗口同归属，可并排比较
    inputs_differ  INTEGER NOT NULL,         -- 输入行集合已不同（如时间归属改变）
    status         TEXT NOT NULL CHECK (status IN ('pending','recomputed','withdrawn','ignored')),
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    resolved_at    TEXT
);

-- 审计日志：所有登记 / 审批 / 撤回 / 重算动作留痕
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    actor       TEXT NOT NULL DEFAULT '系统'
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def audit(conn: sqlite3.Connection, entity_type: str, entity_id: str, action: str,
          detail: dict | None = None, actor: str = "系统") -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, entity_type, entity_id, action, detail_json, actor) "
        "VALUES (?,?,?,?,?,?)",
        (utcnow(), entity_type, entity_id, action, json_dumps(detail or {}), actor),
    )


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
