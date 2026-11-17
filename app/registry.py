"""口径登记服务：指标 / 口径 / 报表 / 引擎的登记与生命周期管理。

核心职责：
1. 口径变化（维度过滤、时间归属、公式）时，自动登记受影响报表与产物（impacts）；
2. 触发计算器双算，保证同一指标旧口径与候选口径并存可对比；
3. 审批、撤回、延后升级、计算器退出均留审计痕迹；
4. 模拟接口不落库，只推演后果。
"""
from __future__ import annotations

import json
import sqlite3

from . import rules
from .calculator import compute_product
from .db import audit, json_dumps, utcnow
from .periods import SEED_PERIODS, Period

# ---------------------------------------------------------------- 常量

ENGINE_MAIN = "engine-main"
ENGINE_CANDIDATE = "engine-candidate"


# ---------------------------------------------------------------- 基础查询

def list_metrics(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM metrics ORDER BY metric_id")]


def list_calibers(conn: sqlite3.Connection, metric_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM calibers"
    args: tuple = ()
    if metric_id:
        sql += " WHERE metric_id=?"
        args = (metric_id,)
    sql += " ORDER BY metric_id, created_at, version"
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["filters"] = json.loads(d["filters_json"])
        out.append(d)
    return out


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY report_id")]


def list_engines(conn: sqlite3.Connection) -> list[dict]:
    engines = [dict(r) for r in conn.execute("SELECT * FROM engines ORDER BY engine_id")]
    for e in engines:
        e["calibers"] = [r["caliber_id"] for r in conn.execute(
            "SELECT caliber_id FROM engine_calibers WHERE engine_id=? ORDER BY caliber_id",
            (e["engine_id"],))]
    return engines


def latest_approved(conn: sqlite3.Connection, metric_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM calibers WHERE metric_id=? AND status='approved' "
        "ORDER BY created_at DESC LIMIT 1", (metric_id,)).fetchone()
    return dict(r) if r else None


def _spec_of(conn: sqlite3.Connection, caliber_id: str) -> rules.CaliberSpec:
    r = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if r is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    return rules.CaliberSpec.from_row(r)


# ---------------------------------------------------------------- 影响分析

def _analyze_change(conn: sqlite3.Connection, candidate_id: str) -> dict:
    """对比候选口径与当前已批准口径，逐报表登记影响。重复登记会刷新 pending 行。"""
    cand = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (candidate_id,)).fetchone()
    parent_id = cand["parent_caliber_id"]
    parent = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (parent_id,)).fetchone()
    spec_new = rules.CaliberSpec.from_row(cand)
    spec_old = rules.CaliberSpec.from_row(parent)
    rules.validate_spec(spec_new)
    change_types = rules.diff_rule_sets(spec_old, spec_new)

    # 输入是否已不同：时间归属变化 → 进入窗口的订单集合不同；过滤变化 → 行集合不同
    inputs_differ = any(c == "time_attr" or c.startswith("filter:") for c in change_types)
    # 能否直接比较总额：同时间窗口且同度量公式，差异才可逐行对账
    comparable = "time_attr" not in change_types and "formula" not in change_types

    # 清理旧的 pending 影响（修订后重新登记）
    conn.execute("DELETE FROM impacts WHERE caliber_id=? AND status='pending'", (candidate_id,))

    report_ids = [r["report_id"] for r in conn.execute(
        "SELECT report_id FROM reports WHERE metric_id=? ORDER BY report_id", (cand["metric_id"],))]
    periods = _relevant_periods(conn, cand["metric_id"])
    for rid in report_ids:
        for p in periods:
            prod = conn.execute(
                "SELECT product_id FROM products WHERE report_id=? AND caliber_id=? "
                "AND period_start=? AND period_end=?",
                (rid, candidate_id, p.start, p.end)).fetchone()
            note = _comparability_note(change_types, comparable, inputs_differ)
            conn.execute("""
                INSERT INTO impacts(caliber_id, report_id, period_start, period_end, product_id,
                                    change_types, comparable, inputs_differ, status, note, created_at)
                VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)
            """, (candidate_id, rid, p.start, p.end,
                  prod["product_id"] if prod else None,
                  ",".join(change_types), 1 if comparable else 0,
                  1 if inputs_differ else 0, note, utcnow()))
    conn.commit()
    return {"change_types": change_types, "comparable": comparable, "inputs_differ": inputs_differ,
            "reports": report_ids, "periods": [p.label for p in periods]}


def _relevant_periods(conn: sqlite3.Connection, metric_id: str) -> list[Period]:
    rows = conn.execute("""
        SELECT DISTINCT period_start, period_end FROM products p
        JOIN reports r ON r.report_id = p.report_id
        WHERE r.metric_id=? ORDER BY period_start
    """, (metric_id,)).fetchall()
    if rows:
        return [Period(r["period_start"], r["period_end"], r["period_start"][:7]) for r in rows]
    return list(SEED_PERIODS)


def _comparability_note(change_types: list[str], comparable: bool, inputs_differ: bool) -> str:
    parts = []
    if "time_attr" in change_types:
        parts.append("时间归属改变：两口径的订单进入不同期间，输入行集合不同，总额不能直接比较")
    if any(c.startswith("filter:") for c in change_types):
        dims = [c.split(":", 1)[1] for c in change_types if c.startswith("filter:")]
        parts.append(f"维度过滤改变（{ '、'.join(dims) }）：同窗口下可逐行对账差异")
    if "formula" in change_types:
        parts.append("公式改变：度量含义变化，总额仅作参考不可直接比较")
    if not parts:
        parts.append("规则无实质变化")
    if comparable:
        parts.append("结论：同窗口同公式，可并排比较并逐行解释")
    return "；".join(parts)


def list_impacts(conn: sqlite3.Connection, caliber_id: str | None = None) -> list[dict]:
    sql = """
        SELECT i.*, r.name AS report_name, r.upgrade_policy, r.delayed_until,
               c.version AS caliber_version, m.name AS metric_name
        FROM impacts i
        JOIN reports r ON r.report_id = i.report_id
        JOIN calibers c ON c.caliber_id = i.caliber_id
        JOIN metrics m ON m.metric_id = c.metric_id
    """
    args: tuple = ()
    if caliber_id:
        sql += " WHERE i.caliber_id=?"
        args = (caliber_id,)
    sql += " ORDER BY i.created_at DESC, i.report_id, i.period_start"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- 候选口径注册 / 修订

def register_candidate(conn: sqlite3.Connection, metric_id: str, version: str,
                       description: str, time_attr: str, filters: list[dict],
                       formula: str, change_note: str,
                       engine_id: str = ENGINE_CANDIDATE) -> dict:
    parent = latest_approved(conn, metric_id)
    if parent is None:
        raise ValueError(f"指标 {metric_id} 尚无已批准口径，不能登记候选口径")
    spec = rules.CaliberSpec(
        caliber_id=f"{metric_id}-{version}", metric_id=metric_id, version=version,
        formula=formula, time_attr=time_attr, filters=tuple(filters))
    rules.validate_spec(spec)
    if conn.execute("SELECT 1 FROM calibers WHERE caliber_id=?", (spec.caliber_id,)).fetchone():
        raise ValueError(f"口径版本 {spec.caliber_id} 已存在")

    conn.execute("""
        INSERT INTO calibers(caliber_id, metric_id, version, revision, status, description,
                             time_attr, filters_json, formula, parent_caliber_id, change_note, created_at)
        VALUES (?,?,?,1,'candidate',?,?,?,?,?,?,?)
    """, (spec.caliber_id, metric_id, version, description, time_attr,
          json_dumps(filters), formula, parent["caliber_id"], change_note, utcnow()))
    conn.execute("INSERT OR IGNORE INTO engine_calibers(engine_id, caliber_id) VALUES (?,?)",
                 (engine_id, spec.caliber_id))
    audit(conn, "caliber", spec.caliber_id, "register_candidate", {
        "parent": parent["caliber_id"], "rule": spec.to_rule_dict(), "change_note": change_note})
    conn.commit()

    info = _analyze_change(conn, spec.caliber_id)
    _dual_run(conn, spec.caliber_id, engine_id)
    return {"caliber_id": spec.caliber_id, **info}


def amend_candidate(conn: sqlite3.Connection, caliber_id: str,
                    time_attr: str | None = None, filters: list[dict] | None = None,
                    formula: str | None = None, change_note: str = "") -> dict:
    """候选口径可在批准前修订；修订即升 revision、重新登记影响并重算全部产物。"""
    row = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if row is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    if row["status"] != "candidate":
        raise ValueError(f"仅 candidate 状态可修订，当前为 {row['status']}")

    new_filters = filters if filters is not None else json.loads(row["filters_json"])
    spec = rules.CaliberSpec(
        caliber_id=caliber_id, metric_id=row["metric_id"], version=row["version"],
        formula=formula or row["formula"], time_attr=time_attr or row["time_attr"],
        filters=tuple(new_filters))
    rules.validate_spec(spec)

    old_rule = rules.CaliberSpec.from_row(row).to_rule_dict()
    conn.execute("""
        UPDATE calibers SET revision=revision+1, time_attr=?, filters_json=?, formula=?,
                            change_note=? WHERE caliber_id=?
    """, (spec.time_attr, json_dumps(new_filters), spec.formula, change_note, caliber_id))
    audit(conn, "caliber", caliber_id, "amend", {
        "revision_before": row["revision"], "old_rule": old_rule,
        "new_rule": spec.to_rule_dict(), "change_note": change_note})
    conn.commit()

    info = _analyze_change(conn, caliber_id)
    engine = conn.execute(
        "SELECT engine_id FROM engine_calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    _dual_run(conn, caliber_id, engine["engine_id"] if engine else ENGINE_CANDIDATE)
    return {"caliber_id": caliber_id, **info}


def _dual_run(conn: sqlite3.Connection, candidate_id: str, engine_id: str) -> None:
    """对所有依赖报表 × 相关期间执行候选口径计算（旧口径产物已存在，形成双算）。"""
    cand = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (candidate_id,)).fetchone()
    parent = conn.execute("SELECT * FROM calibers WHERE caliber_id=?",
                          (cand["parent_caliber_id"],)).fetchone()
    inputs_differ = 1 if cand["time_attr"] != parent["time_attr"] else 0
    impacts = conn.execute(
        "SELECT * FROM impacts WHERE caliber_id=? AND status='pending'", (candidate_id,)).fetchall()
    for imp in impacts:
        p = Period(imp["period_start"], imp["period_end"], imp["period_start"][:7])
        pid = compute_product(conn, imp["report_id"], candidate_id, p, engine_id=engine_id,
                              mark_impact=imp["impact_id"])
        conn.execute("UPDATE products SET inputs_differ=? WHERE product_id=?", (inputs_differ, pid))
    conn.commit()


# ---------------------------------------------------------------- 审批 / 撤回 / 延后

def approve(conn: sqlite3.Connection, caliber_id: str, actor: str = "口径管理员") -> dict:
    row = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if row is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    if row["status"] != "candidate":
        raise ValueError(f"仅 candidate 可批准，当前为 {row['status']}")

    parent_id = row["parent_caliber_id"]
    conn.execute("UPDATE calibers SET status='approved', approved_at=? WHERE caliber_id=?",
                 (utcnow(), caliber_id))
    if parent_id:
        conn.execute("UPDATE calibers SET status='superseded' WHERE caliber_id=?", (parent_id,))

    # 按报表升级策略处理旧产物
    delayed = []
    reports = conn.execute("SELECT * FROM reports WHERE metric_id=?", (row["metric_id"],)).fetchall()
    for r in reports:
        if r["upgrade_policy"] == "delayed":
            # 延后升级：旧口径产物继续作为该报表的有效数字，仅登记待办
            delayed.append(r["report_id"])
            continue
        if r["upgrade_policy"] == "whole":
            conn.execute(
                "UPDATE products SET calc_status='superseded' "
                "WHERE report_id=? AND caliber_id=?", (r["report_id"], parent_id))
        else:  # dual：双算过渡，旧产物标记 superseded 但保留可对比
            conn.execute(
                "UPDATE products SET calc_status='superseded' "
                "WHERE report_id=? AND caliber_id=?", (r["report_id"], parent_id))
    conn.execute("UPDATE impacts SET status='recomputed', resolved_at=? "
                 "WHERE caliber_id=? AND status IN ('pending','recomputed')",
                 (utcnow(), caliber_id))
    audit(conn, "caliber", caliber_id, "approve",
          {"parent": parent_id, "delayed_reports": delayed}, actor=actor)
    conn.commit()
    return {"caliber_id": caliber_id, "delayed_reports": delayed}


def withdraw(conn: sqlite3.Connection, caliber_id: str, actor: str = "口径管理员") -> dict:
    row = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if row is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    if row["status"] != "candidate":
        raise ValueError(f"仅 candidate 可撤回，当前为 {row['status']}（已批准口径请走新版替换流程）")
    conn.execute("UPDATE calibers SET status='withdrawn', withdrawn_at=? WHERE caliber_id=?",
                 (utcnow(), caliber_id))
    conn.execute("UPDATE products SET calc_status='withdrawn' WHERE caliber_id=?", (caliber_id,))
    conn.execute("UPDATE impacts SET status='withdrawn', resolved_at=? WHERE caliber_id=?",
                 (utcnow(), caliber_id))
    conn.execute("DELETE FROM engine_calibers WHERE caliber_id=?", (caliber_id,))
    audit(conn, "caliber", caliber_id, "withdraw", {"rule": rules.CaliberSpec.from_row(row).to_rule_dict()},
          actor=actor)
    conn.commit()
    return {"caliber_id": caliber_id}


def delay_report(conn: sqlite3.Connection, report_id: str, until: str,
                 actor: str = "口径管理员") -> dict:
    r = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if r is None:
        raise KeyError(f"报表不存在: {report_id}")
    conn.execute("UPDATE reports SET upgrade_policy='delayed', delayed_until=? WHERE report_id=?",
                 (until, report_id))
    audit(conn, "report", report_id, "delay_upgrade", {"until": until}, actor=actor)
    conn.commit()
    return {"report_id": report_id, "delayed_until": until}


def lift_delay(conn: sqlite3.Connection, report_id: str, actor: str = "口径管理员") -> dict:
    """结束延后：报表切换到当前已批准口径，旧产物转为 superseded。"""
    r = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
    if r is None:
        raise KeyError(f"报表不存在: {report_id}")
    if r["upgrade_policy"] != "delayed":
        raise ValueError("该报表未处于延后升级状态")
    current = latest_approved(conn, r["metric_id"])
    if current is None:
        raise ValueError("该指标没有已批准口径，无法结束延后")
    conn.execute(
        "UPDATE products SET calc_status='superseded' WHERE report_id=? AND caliber_id IN "
        "(SELECT caliber_id FROM calibers WHERE metric_id=? AND status='superseded')",
        (report_id, r["metric_id"]))
    conn.execute(
        "UPDATE reports SET upgrade_policy='whole', delayed_until=NULL WHERE report_id=?",
        (report_id,))
    audit(conn, "report", report_id, "lift_delay",
          {"active_caliber": current["caliber_id"]}, actor=actor)
    conn.commit()
    return {"report_id": report_id, "active_caliber": current["caliber_id"]}


# ---------------------------------------------------------------- 模拟（不落库）

def simulate_withdraw(conn: sqlite3.Connection, caliber_id: str) -> dict:
    row = conn.execute("SELECT * FROM calibers WHERE caliber_id=?", (caliber_id,)).fetchone()
    if row is None:
        raise KeyError(f"口径不存在: {caliber_id}")
    products = conn.execute(
        "SELECT p.*, r.name AS report_name FROM products p JOIN reports r ON r.report_id=p.report_id "
        "WHERE p.caliber_id=?", (caliber_id,)).fetchall()
    parent = conn.execute("SELECT * FROM calibers WHERE caliber_id=?",
                          (row["parent_caliber_id"],)).fetchone()
    per_report = []
    safe = True
    for p in products:
        fallback = conn.execute(
            "SELECT product_id FROM products WHERE report_id=? AND caliber_id=? AND period_start=? "
            "AND period_end=? AND calc_status IN ('computed','stale')",
            (p["report_id"], parent["caliber_id"], p["period_start"], p["period_end"])).fetchone()
        if not fallback:
            safe = False
        per_report.append({
            "report_id": p["report_id"], "report_name": p["report_name"],
            "period": p["period_start"][:7], "fallback_product_id": fallback["product_id"] if fallback else None,
        })
    return {
        "action": "withdraw", "caliber_id": caliber_id,
        "would_withdraw_products": len(products),
        "parent_caliber": parent["caliber_id"],
        "parent_still_approved": parent["status"] == "approved",
        "safe": safe and parent["status"] == "approved",
        "report_coverage": per_report,
        "explanation": "撤回后候选产物全部作废、引擎解绑；每个报表期间需能回退到父口径产物，否则出现断档。",
    }


def simulate_engine_retire(conn: sqlite3.Connection, engine_id: str) -> dict:
    eng = conn.execute("SELECT * FROM engines WHERE engine_id=?", (engine_id,)).fetchone()
    if eng is None:
        raise KeyError(f"引擎不存在: {engine_id}")
    served = [r["caliber_id"] for r in conn.execute(
        "SELECT caliber_id FROM engine_calibers WHERE engine_id=?", (engine_id,))]
    exclusive, covered_elsewhere = [], []
    blockers = []
    for cid in served:
        others = conn.execute(
            "SELECT engine_id FROM engine_calibers WHERE caliber_id=? AND engine_id<>?",
            (cid, engine_id)).fetchall()
        (covered_elsewhere if others else exclusive).append(cid)
        if not others:
            prods = conn.execute(
                "SELECT p.product_id, r.name AS report_name, p.period_start FROM products p "
                "JOIN reports r ON r.report_id=p.report_id "
                "WHERE p.caliber_id=? AND p.calc_status IN ('computed','stale')", (cid,)).fetchall()
            for p in prods:
                blockers.append({"product_id": p["product_id"], "report_name": p["report_name"],
                                 "period": p["period_start"][:7], "caliber_id": cid})
    active_products = conn.execute(
        "SELECT COUNT(*) AS n FROM products WHERE engine_id=? AND calc_status IN ('computed','stale')",
        (engine_id,)).fetchone()["n"]
    return {
        "action": "retire_engine", "engine_id": engine_id,
        "served_calibers": served,
        "exclusive_calibers": exclusive,
        "calibers_covered_by_other_engines": covered_elsewhere,
        "active_products_on_engine": active_products,
        "blocking_products": blockers,
        "safe": not blockers and eng["status"] == "active",
        "explanation": "退出条件：引擎独占的每个口径都已由其他在役引擎承接，且没有仍以该引擎为计算方的有效产物。",
    }


def retire_engine(conn: sqlite3.Connection, engine_id: str, actor: str = "运维") -> dict:
    sim = simulate_engine_retire(conn, engine_id)
    if not sim["safe"]:
        raise ValueError("引擎退出条件不满足：" + json.dumps(sim["blocking_products"], ensure_ascii=False))
    conn.execute("UPDATE engines SET status='retired', retired_at=?, retire_reason='演练退出' WHERE engine_id=?",
                 (utcnow(), engine_id))
    conn.execute("DELETE FROM engine_calibers WHERE engine_id=?", (engine_id,))
    audit(conn, "engine", engine_id, "retire", {"simulation": sim}, actor=actor)
    conn.commit()
    return {"engine_id": engine_id}


def reactivate_engine(conn: sqlite3.Connection, engine_id: str) -> dict:
    conn.execute("UPDATE engines SET status='active', retired_at=NULL, retire_reason=NULL WHERE engine_id=?",
                 (engine_id,))
    audit(conn, "engine", engine_id, "reactivate", {})
    conn.commit()
    return {"engine_id": engine_id}


def audit_trail(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]


def recompute_stale(conn: sqlite3.Connection) -> list[int]:
    """手动触发：把所有 stale 产物按原口径重算。"""
    rows = conn.execute("SELECT DISTINCT report_id, caliber_id, period_start, period_end FROM products "
                        "WHERE calc_status='stale'").fetchall()
    from .periods import parse_period
    ids = []
    for r in rows:
        p = parse_period(f"{r['period_start']}:{r['period_end']}")
        ids.append(compute_product(conn, r["report_id"], r["caliber_id"], p))
    return ids
