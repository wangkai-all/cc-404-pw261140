"""端到端测试：在临时 SQLite 库上跑完整业务流，不依赖运行中的服务。

运行：python -m unittest tests.test_e2e -v
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app import registry
from app.bootstrap import bootstrap
from app.calculator import list_products
from app.db import get_conn
from app.diff import product_diff, query_orders
from app.periods import month_period


class PlatformE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        info = bootstrap(self.db)
        self.assertGreater(info["orders"], 500)
        self.conn = get_conn(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # 1. 基线：8 份 v1 产物，且产物合计 == 逐行贡献重建合计
    def test_01_baseline_products_tie_to_contributions(self):
        products = list_products(self.conn)
        self.assertEqual(len(products), 8)  # 4 报表 × 2 期间
        for p in products:
            rebuilt = self.conn.execute(
                "SELECT ROUND(SUM(value),2) s FROM contributions WHERE product_id=? AND included=1",
                (p["product_id"],)).fetchone()["s"]
            self.assertAlmostEqual(rebuilt or 0.0, p["total"], places=2)

    # 2. GMV v2：时间归属改支付时间 → 影响登记标记不可直接比较、输入已不同；双算产物生成
    def test_02_gmv_candidate_time_attr_dual_run(self):
        info = registry.register_candidate(
            self.conn, "gmv", "v2", "GMV 候选口径：按支付时间归属",
            "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "财务要求按支付时间确认收入")
        self.assertIn("time_attr", info["change_types"])
        self.assertFalse(info["comparable"])
        self.assertTrue(info["inputs_differ"])

        impacts = registry.list_impacts(self.conn, "gmv-v2")
        self.assertEqual(len(impacts), 6)  # 3 份 GMV 报表 × 2 期间
        self.assertTrue(all(i["inputs_differ"] == 1 for i in impacts))
        self.assertTrue(all(i["comparable"] == 0 for i in impacts))
        self.assertTrue(all(i["status"] == "recomputed" for i in impacts))

        # 双算：每个 GMV 报表期间都有 v1 与 v2 两份产物
        prods = list_products(self.conn, "rpt-gmv-overview")
        self.assertEqual(len(prods), 4)
        versions = {p["caliber_version"] for p in prods}
        self.assertEqual(versions, {"v1", "v2"})

    # 3. 跨月边界订单：9/30 下单 10/1 支付 → v1 计 9 月，v2 计 10 月，逐行可解释
    def test_03_cross_month_order_moves_periods(self):
        registry.register_candidate(
            self.conn, "gmv", "v2", "按支付时间", "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "时间归属调整")
        sep = self._pair("rpt-gmv-overview", month_period(2026, 9))
        d_sep = product_diff(self.conn, sep[0], sep[1])
        row = next(r for r in d_sep["rows"] if r["order_id"] == "SO" + f"{self._boundary_seq():05d}")
        # 该订单在 9 月：v1 计入、v2 排除（支付时间在 10 月）
        self.assertTrue(row["in_a"])
        self.assertFalse(row["in_b"])
        self.assertIn("不在期间", row["reason_b"])

        oct_ = self._pair("rpt-gmv-overview", month_period(2026, 10))
        d_oct = product_diff(self.conn, oct_[0], oct_[1])
        row2 = next(r for r in d_oct["rows"] if r["order_id"] == row["order_id"])
        self.assertFalse(row2["in_a"])
        self.assertTrue(row2["in_b"])

    # 4. 桥接闭合：旧合计 - 移除 + 新增 + 公式差 = 新合计
    def test_04_bridge_closes(self):
        registry.register_candidate(
            self.conn, "gmv", "v2", "按支付时间", "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "时间归属调整")
        oct_ = self._pair("rpt-gmv-overview", month_period(2026, 10))
        d = product_diff(self.conn, oct_[0], oct_[1])
        self.assertTrue(d["bridge_closed"])
        self.assertAlmostEqual(d["product_a"]["total_stored"], d["product_a"]["total_rebuilt"], places=2)
        self.assertAlmostEqual(d["product_b"]["total_stored"], d["product_b"]["total_rebuilt"], places=2)

    # 5. 订单数 v2：只改维度过滤（时间归属不变）→ 可比较；赠品/线下门店被移除
    def test_05_order_count_filter_change_is_comparable(self):
        info = registry.register_candidate(
            self.conn, "order_count", "v2", "有效订单候选：排除赠品与线下门店",
            "order_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]},
             {"field": "is_gift", "op": "eq", "value": 0},
             {"field": "channel", "op": "not_in", "values": ["线下门店"]}],
            "row_count", "口径治理：赠品与线下渠道不计有效订单")
        self.assertTrue(info["comparable"])
        self.assertTrue(info["inputs_differ"])  # 过滤改变 → 行集合不同，但同窗口可对账
        impacts = registry.list_impacts(self.conn, "order_count-v2")
        self.assertTrue(all(i["comparable"] == 1 for i in impacts))

        oct_ = self._pair("rpt-oc-channel", month_period(2026, 10))
        d = product_diff(self.conn, oct_[0], oct_[1])
        self.assertTrue(d["bridge_closed"])
        # 已支付赠品：v1 计入，v2 被 is_gift 过滤移除
        paid_gifts = [r for r in d["rows"] if r["is_gift"] == 1 and r["in_a"]]
        self.assertTrue(paid_gifts)
        self.assertTrue(all(r["kind"] == "removed" and "is_gift" in r["reason_b"] for r in paid_gifts))
        # 线下门店订单同样被移除（赠品若同时是线下单，先命中 is_gift 条件）
        offline = [r for r in d["rows"] if r["channel"] == "线下门店" and r["in_a"]]
        self.assertTrue(offline)
        self.assertTrue(all(r["kind"] == "removed" for r in offline))
        self.assertTrue(any("channel" in r["reason_b"] for r in offline))

    # 6. 审批：dual/whole 报表旧产物 superseded；delayed 报表保持旧口径
    def test_06_approve_respects_upgrade_policies(self):
        registry.register_candidate(
            self.conn, "gmv", "v2", "按支付时间", "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "时间归属调整")
        res = registry.approve(self.conn, "gmv-v2")
        self.assertEqual(res["delayed_reports"], ["rpt-gmv-region"])

        c2 = self.conn.execute("SELECT status FROM calibers WHERE caliber_id='gmv-v2'").fetchone()["status"]
        c1 = self.conn.execute("SELECT status FROM calibers WHERE caliber_id='gmv-v1'").fetchone()["status"]
        self.assertEqual(c2, "approved")
        self.assertEqual(c1, "superseded")

        # 延后报表的 v1 产物仍是 computed
        delayed = [p for p in list_products(self.conn, "rpt-gmv-region") if p["caliber_version"] == "v1"]
        self.assertTrue(all(p["calc_status"] == "computed" for p in delayed))
        # 整版替换报表的 v1 产物已 superseded
        whole = [p for p in list_products(self.conn, "rpt-gmv-channel") if p["caliber_version"] == "v1"]
        self.assertTrue(all(p["calc_status"] == "superseded" for p in whole))

        # 结束延后 → 切换到批准口径
        lift = registry.lift_delay(self.conn, "rpt-gmv-region")
        self.assertEqual(lift["active_caliber"], "gmv-v2")
        r = self.conn.execute("SELECT upgrade_policy, delayed_until FROM reports WHERE report_id='rpt-gmv-region'").fetchone()
        self.assertEqual(r["upgrade_policy"], "whole")
        self.assertIsNone(r["delayed_until"])

    # 7. 撤回模拟：父口径在役且产物齐全 → 安全；真实撤回后产物作废、引擎解绑
    def test_07_withdraw_simulation_and_real(self):
        registry.register_candidate(
            self.conn, "gmv", "v2", "按支付时间", "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "时间归属调整")
        sim = registry.simulate_withdraw(self.conn, "gmv-v2")
        self.assertTrue(sim["safe"])
        self.assertTrue(all(x["fallback_product_id"] for x in sim["report_coverage"]))

        registry.withdraw(self.conn, "gmv-v2")
        self.assertEqual(
            self.conn.execute("SELECT status FROM calibers WHERE caliber_id='gmv-v2'").fetchone()["status"],
            "withdrawn")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM products WHERE caliber_id='gmv-v2' AND calc_status<>'withdrawn'").fetchone()["n"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM engine_calibers WHERE caliber_id='gmv-v2'").fetchone()["n"], 0)
        # 已批准口径不能撤回
        with self.assertRaises(ValueError):
            registry.withdraw(self.conn, "gmv-v1")

    # 8. 引擎退出模拟：候选引擎独占 v2 → 阻塞；v2 撤回后可安全退出
    def test_08_engine_retire_blocked_then_safe(self):
        registry.register_candidate(
            self.conn, "gmv", "v2", "按支付时间", "pay_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]}],
            "net_paid", "时间归属调整")
        sim = registry.simulate_engine_retire(self.conn, "engine-candidate")
        self.assertFalse(sim["safe"])
        self.assertIn("gmv-v2", sim["exclusive_calibers"])
        self.assertTrue(sim["blocking_products"])
        with self.assertRaises(ValueError):
            registry.retire_engine(self.conn, "engine-candidate")

        registry.withdraw(self.conn, "gmv-v2")
        sim2 = registry.simulate_engine_retire(self.conn, "engine-candidate")
        self.assertTrue(sim2["safe"])
        registry.retire_engine(self.conn, "engine-candidate")
        self.assertEqual(
            self.conn.execute("SELECT status FROM engines WHERE engine_id='engine-candidate'").fetchone()["status"],
            "retired")

        # 主引擎同时也可演练：它独占 v1 口径 → 阻塞
        sim_main = registry.simulate_engine_retire(self.conn, "engine-main")
        self.assertFalse(sim_main["safe"])

    # 9. 修订：改候选口径 → revision 自增、影响重登记、产物重算且桥接仍闭合
    def test_09_amend_recomputes_and_reopens_impacts(self):
        reg = registry.register_candidate(
            self.conn, "order_count", "v2", "排除赠品", "order_time",
            [{"field": "status", "op": "in",
              "values": ["paid", "refunded", "partial_refund"]},
             {"field": "is_gift", "op": "eq", "value": 0}],
            "row_count", "v2 初版")
        before = self.conn.execute(
            "SELECT total FROM products p JOIN calibers c ON c.caliber_id=p.caliber_id "
            "WHERE c.caliber_id='order_count-v2' AND p.report_id='rpt-oc-channel' "
            "AND p.period_start='2026-10-01'").fetchone()["total"]
        registry.amend_candidate(
            self.conn, "order_count-v2",
            filters=[{"field": "status", "op": "in",
                      "values": ["paid", "refunded", "partial_refund"]}],
            change_note="修订：取消赠品过滤")
        rev = self.conn.execute("SELECT revision FROM calibers WHERE caliber_id='order_count-v2'").fetchone()["revision"]
        self.assertEqual(rev, 2)
        after = self.conn.execute(
            "SELECT total FROM products p JOIN calibers c ON c.caliber_id=p.caliber_id "
            "WHERE c.caliber_id='order_count-v2' AND p.report_id='rpt-oc-channel' "
            "AND p.period_start='2026-10-01'").fetchone()["total"]
        self.assertNotEqual(before, after)
        # 取消赠品过滤后，v2 与 v1 在该报表上的 10 月结果应一致
        pair = self._pair("rpt-oc-channel", month_period(2026, 10))
        d = product_diff(self.conn, pair[0], pair[1])
        self.assertTrue(d["bridge_closed"])
        self.assertEqual(d["summary"]["removed"], 0)
        self.assertEqual(d["summary"]["added"], 0)

    # 10. 非法口径拒绝登记（未知公式 / 非法维度 / 跨指标计算）
    def test_10_validation_rejects_bad_calibers(self):
        with self.assertRaises(ValueError):
            registry.register_candidate(
                self.conn, "gmv", "v9", "坏口径", "order_time", [], "unknown_formula", "")
        with self.assertRaises(ValueError):
            registry.register_candidate(
                self.conn, "gmv", "v9", "坏口径", "order_time",
                [{"field": "customer_id", "op": "in", "values": ["C1"]}], "net_paid", "")
        with self.assertRaises(ValueError):
            registry.register_candidate(
                self.conn, "gmv", "v9", "坏口径", "ship_time", [], "net_paid", "")

    # 11. 原始订单查询与赠品/退款边界数据存在
    def test_11_order_query_and_boundary_data(self):
        gifts = query_orders(self.conn, {"is_gift": 1})
        self.assertTrue(gifts)
        refunded = query_orders(self.conn, {"status": "refunded"})
        self.assertTrue(any(o["refund_amount"] == o["pay_amount"] for o in refunded))
        pending = query_orders(self.conn, {"status": "pending"})
        self.assertTrue(all(o["pay_time"] is None for o in pending))

    # ---------------------------------------------------------- 辅助
    def _pair(self, report_id, period):
        rows = self.conn.execute(
            "SELECT product_id, caliber_id FROM products WHERE report_id=? AND period_start=? AND period_end=? ",
            (report_id, period.start, period.end)).fetchall()
        self.assertGreaterEqual(len(rows), 2)
        old = next(r for r in rows if r["caliber_id"].endswith("-v1"))
        new = next(r for r in rows if not r["caliber_id"].endswith("-v1"))
        return old["product_id"], new["product_id"]

    def _boundary_seq(self):
        # 边界订单在随机订单之后，从 orders 表直接查跨月订单号序号
        row = self.conn.execute(
            "SELECT order_id FROM orders WHERE order_time LIKE '2026-09-30%' AND pay_time LIKE '2026-10-01%'").fetchone()
        return int(row["order_id"].replace("SO", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
