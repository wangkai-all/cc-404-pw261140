"""规则引擎：口径公式 / 过滤 / 时间归属的唯一实现来源。

口径登记、计算器、报告消费端全部调用这里的函数，任何模块都不得自行复制公式。
同一订单在旧口径与候选口径下走同一份代码，只是传入的 CaliberSpec 不同。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 维度可选字段（维度过滤只允许针对这些字段）
DIMENSION_FIELDS = ("status", "channel", "region", "category", "is_gift")

# 时间归属可选字段
TIME_ATTR_FIELDS = ("order_time", "pay_time")

# 支持的公式（全部基于订单原始字段，避免各处手拼表达式）
FORMULAS = {
    "net_paid": "实付净额 = 支付金额 - 退款金额（已支付订单）",
    "gross_paid": "支付金额（含退款，不扣减）",
    "row_count": "行数：每单计 1",
}


@dataclass(frozen=True)
class CaliberSpec:
    """口径规格：一个口径版本在计算层面的全部输入。"""
    caliber_id: str
    metric_id: str
    version: str
    formula: str
    time_attr: str                 # order_time / pay_time
    filters: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_row(cls, row) -> "CaliberSpec":
        import json
        return cls(
            caliber_id=row["caliber_id"],
            metric_id=row["metric_id"],
            version=row["version"],
            formula=row["formula"],
            time_attr=row["time_attr"],
            filters=tuple(json.loads(row["filters_json"])),
        )

    def to_rule_dict(self) -> dict[str, Any]:
        return {
            "caliber_id": self.caliber_id,
            "formula": self.formula,
            "time_attr": self.time_attr,
            "filters": list(self.filters),
        }


def validate_filter(f: dict[str, Any]) -> None:
    """登记前校验过滤条件，非法口径不允许进入系统。"""
    if not isinstance(f, dict):
        raise ValueError(f"过滤条件必须是对象: {f!r}")
    field_ = f.get("field")
    op = f.get("op")
    if field_ not in DIMENSION_FIELDS:
        raise ValueError(f"不支持的维度字段 {field_!r}，可选 {DIMENSION_FIELDS}")
    if op not in ("in", "not_in", "eq", "ne"):
        raise ValueError(f"不支持的操作符 {op!r}，可选 in/not_in/eq/ne")
    if op in ("in", "not_in"):
        if not isinstance(f.get("values"), list) or not f["values"]:
            raise ValueError(f"{op} 需要非空 values 列表")
    else:
        if "value" not in f:
            raise ValueError(f"{op} 需要 value")


def validate_spec(spec: CaliberSpec) -> None:
    if spec.formula not in FORMULAS:
        raise ValueError(f"未知公式 {spec.formula!r}，已登记公式: {sorted(FORMULAS)}")
    if spec.time_attr not in TIME_ATTR_FIELDS:
        raise ValueError(f"未知时间归属 {spec.time_attr!r}，可选 {TIME_ATTR_FIELDS}")
    for f in spec.filters:
        validate_filter(f)


def _matches_filter(order: dict, f: dict) -> bool:
    actual = order.get(f["field"])
    op = f["op"]
    if op == "in":
        return actual in f["values"]
    if op == "not_in":
        return actual not in f["values"]
    if op == "eq":
        return actual == f["value"]
    if op == "ne":
        return actual != f["value"]
    raise ValueError(f"未知操作符 {op!r}")


def exclusion_reason(order: dict, filters: tuple[dict[str, Any], ...]) -> str | None:
    """返回第一条不满足的过滤条件的人类可读原因；全部满足返回 None。"""
    for f in filters:
        if not _matches_filter(order, f):
            if f["op"] in ("in", "eq"):
                want = f.get("values", f.get("value"))
                return f"{f['field']}={order.get(f['field'])} 不属于允许集合 {want}"
            else:
                want = f.get("values", f.get("value"))
                return f"{f['field']}={order.get(f['field'])} 命中排除集合 {want}"
    return None


def formula_value(order: dict, formula: str) -> float:
    """公式的唯一实现。所有口径计算都经过这里。"""
    if formula == "net_paid":
        # 未支付订单没有支付金额，净额为 0（配合 status 过滤使用）
        return round(float(order["pay_amount"] or 0) - float(order["refund_amount"] or 0), 2)
    if formula == "gross_paid":
        return round(float(order["pay_amount"] or 0), 2)
    if formula == "row_count":
        return 1.0
    raise ValueError(f"未知公式 {formula!r}")


def attribution_timestamp(order: dict, time_attr: str) -> str | None:
    """时间归属：返回该口径下订单归属期所依据的时间戳；为空说明尚未发生（如未支付）。"""
    if time_attr == "order_time":
        return order["order_time"]
    if time_attr == "pay_time":
        return order["pay_time"]
    raise ValueError(f"未知时间归属 {time_attr!r}")


def evaluate_order(order: dict, spec: CaliberSpec,
                   period_start: str, period_end: str) -> dict[str, Any]:
    """对单个订单执行一次完整口径评估。

    返回 {included, value, reason, attr_ts}。
    顺序固定：先看时间归属是否落入期间，再看维度过滤，最后取公式值。
    旧口径与候选口径的差异因此可以逐订单解释。
    """
    attr_ts = attribution_timestamp(order, spec.time_attr)
    if attr_ts is None:
        return {"included": False, "value": 0.0,
                "reason": f"时间归属字段 {spec.time_attr} 为空（事件尚未发生）", "attr_ts": None}
    if not (period_start <= attr_ts[:10] <= period_end):
        return {"included": False, "value": 0.0,
                "reason": f"归属时间 {attr_ts[:10]} 不在期间 [{period_start}, {period_end}] 内（按{_zh(spec.time_attr)}）",
                "attr_ts": attr_ts}
    reason = exclusion_reason(order, spec.filters)
    if reason is not None:
        return {"included": False, "value": 0.0, "reason": reason, "attr_ts": attr_ts}
    return {"included": True, "value": formula_value(order, spec.formula),
            "reason": "计入", "attr_ts": attr_ts}


def _zh(time_attr: str) -> str:
    return {"order_time": "下单时间", "pay_time": "支付时间"}[time_attr]


def diff_rule_sets(spec_a: CaliberSpec, spec_b: CaliberSpec) -> list[str]:
    """比较两个口径的规则差异类型，用于影响分析的 change_types。"""
    changes: list[str] = []
    if spec_a.time_attr != spec_b.time_attr:
        changes.append("time_attr")
    if spec_a.formula != spec_b.formula:
        changes.append("formula")
    fa = {(f["field"], f["op"], tuple(f.get("values", [])) or (f.get("value"),)) for f in spec_a.filters}
    fb = {(f["field"], f["op"], tuple(f.get("values", [])) or (f.get("value"),)) for f in spec_b.filters}
    for field_ in sorted({x[0] for x in fa} | {x[0] for x in fb}):
        a = {x for x in fa if x[0] == field_}
        b = {x for x in fb if x[0] == field_}
        if a != b:
            changes.append(f"filter:{field_}")
    return changes
