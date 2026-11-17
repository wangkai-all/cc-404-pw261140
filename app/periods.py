"""期间工具：统一的期间解析，避免各模块自己算月份边界。"""
from __future__ import annotations

import calendar
from dataclasses import dataclass


@dataclass(frozen=True)
class Period:
    start: str   # YYYY-MM-DD，含
    end: str     # YYYY-MM-DD，含
    label: str

    def as_tuple(self) -> tuple[str, str]:
        return self.start, self.end


def month_period(year: int, month: int) -> Period:
    last = calendar.monthrange(year, month)[1]
    return Period(f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}",
                  f"{year:04d}-{month:02d}")


def parse_period(token: str) -> Period:
    """支持 '2026-10'（月）与 '2026-10-01:2026-10-31'（显式区间）。"""
    if ":" in token:
        s, e = token.split(":", 1)
        return Period(s, e, f"{s}~{e}")
    y, m = token.split("-")
    return month_period(int(y), int(m))


# 平台预置的演示期间（种子期）
SEED_PERIODS = (
    month_period(2026, 9),
    month_period(2026, 10),
)
