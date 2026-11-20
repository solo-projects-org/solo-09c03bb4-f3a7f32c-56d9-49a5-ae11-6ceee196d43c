"""月度贡献明细页测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from test_app import Base, csv_bytes  # noqa: E402


class TestContributionHappyPath(Base):
    def test_contribution_math_and_render(self):
        self.upload(
            "2025-01",
            [("A", "土豆", 1.0), ("B", "黄瓜", 4.0), ("C", "白菜", 10.0)],
        )
        self.upload(
            "2025-02",
            [("A", "土豆", 3.0), ("B", "黄瓜", 4.4), ("C", "白菜", 5.0)],
        )
        resp = self.client.get("/contrib?month=2025-02")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("对环比系数的贡献", html)
        self.assertIn("可比报价对数", html)
        # 汇总对数与平均系数
        self.assertIn("<strong>3</strong>", html)

        # 直接核验服务端计算
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        self.assertTrue(detail["decomposable"])
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["pair_count"], 3)
        ratios = {"A|土豆": 3.0, "B|黄瓜": 1.1, "C|白菜": 0.5}
        contributions = {r["stall"] + "|" + r["item"]: r["contribution"]
                         for r in detail["rows"]}
        for key, ratio in ratios.items():
            self.assertAlmostEqual(contributions[key], ratio / 3, places=9)
        # 贡献之和 = 缓存环比系数
        self.assertAlmostEqual(
            sum(contributions.values()), detail["avg_factor"], places=9
        )
        self.assertAlmostEqual(detail["avg_factor"], (3.0 + 1.1 + 0.5) / 3, places=9)

    def test_rows_sorted_by_abs_contribution_then_stall_item(self):
        self.upload(
            "2025-01",
            [("A", "土豆", 1.0), ("B", "黄瓜", 4.0), ("C", "白菜", 10.0)],
        )
        self.upload(
            "2025-02",
            [("A", "土豆", 3.0), ("B", "黄瓜", 4.4), ("C", "白菜", 5.0)],
        )
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        keys = [(r["stall"], r["item"]) for r in detail["rows"]]
        # 贡献 1.0(A) > 0.3667(B) > 0.1667(C)
        self.assertEqual(keys, [("A", "土豆"), ("B", "黄瓜"), ("C", "白菜")])

    def test_tie_breaks_by_stall_and_item(self):
        self.upload(
            "2025-01", [("B", "土豆", 2.0), ("A", "黄瓜", 2.0), ("A", "土豆", 2.0)]
        )
        self.upload(
            "2025-02", [("B", "土豆", 4.0), ("A", "黄瓜", 4.0), ("A", "土豆", 4.0)]
        )
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        # 三个比值都是 2.0，贡献相同 → 摊位升序，同摊位按品种升序
        keys = [(r["stall"], r["item"]) for r in detail["rows"]]
        self.assertEqual(keys, [("A", "土豆"), ("A", "黄瓜"), ("B", "土豆")])

    def test_detail_uses_cached_upload_batch_ids(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
            cache = conn.execute(
                "SELECT source_upload_id FROM index_months WHERE month='2025-02'"
            ).fetchone()["source_upload_id"]
            prev_cache = conn.execute(
                "SELECT source_upload_id FROM index_months WHERE month='2025-01'"
            ).fetchone()["source_upload_id"]
        finally:
            conn.close()
        self.assertEqual(detail["current"]["source_upload_id"], cache)
        self.assertEqual(detail["previous"]["source_upload_id"], prev_cache)


class TestContributionNonDecomposable(Base):
    def test_base_month_not_decomposable(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        resp = self.client.get("/contrib?month=2025-01")
        html = resp.get_data(as_text=True)
        self.assertIn("环比不可分解", html)
        self.assertIn("基月", html)

    def test_unknown_month_message(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        resp = self.client.get("/contrib?month=2099-01")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("没有 2099-01 的指数缓存", resp.get_data(as_text=True))

    def test_missing_month_not_decomposable(self):
        self.client.post("/demo/load", follow_redirects=True)
        resp = self.client.get("/contrib?month=2025-04")
        html = resp.get_data(as_text=True)
        self.assertIn("环比不可分解", html)
        self.assertIn("缺月", html)

    def test_broken_chain_not_decomposable(self):
        self.client.post("/demo/load", follow_redirects=True)
        resp = self.client.get("/contrib?month=2025-05")
        html = resp.get_data(as_text=True)
        self.assertIn("环比不可分解", html)
        self.assertIn("断链", html)

    def test_no_pairs_not_decomposable(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("Q", "黄瓜", 9.0)])
        resp = self.client.get("/contrib?month=2025-02")
        html = resp.get_data(as_text=True)
        self.assertIn("环比不可分解", html)
        self.assertIn("可比报价对", html)

    def test_no_month_param_shows_only_selector(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        resp = self.client.get("/contrib")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("选择月份", resp.get_data(as_text=True))


class TestContributionStaleness(Base):
    def test_tampered_prices_mark_stale_and_cache_still_served(self):
        self.upload("2025-01", [("A", "土豆", 1.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 10.0)])
        before = self.table()
        conn = self.conn()
        try:
            conn.execute("UPDATE prices SET price = 99 WHERE month='2025-02'")
        finally:
            conn.close()

        resp = self.client.get("/contrib?month=2025-02")
        html = resp.get_data(as_text=True)
        self.assertIn("缓存已过期", html)
        self.assertIn("不展示混合结果", html)
        # 过期时不渲染明细表（表头列名只出现在可分解页面）
        self.assertNotIn("平均系数", html)
        self.assertNotIn("<tbody>", html.split("环比贡献明细", 1)[1])

        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        self.assertEqual(detail["status"], "stale")
        self.assertFalse(detail["decomposable"])
        self.assertEqual(detail["rows"], [])
        self.assertTrue(detail["stale_info"])

        # 指数总览仍读缓存，不受篡改影响
        self.assertEqual(self.table(), before)

    def test_batch_mismatch_marked_stale(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        conn = self.conn()
        try:
            cur = conn.execute(
                "INSERT INTO uploads(month, filename, row_count, uploaded_at, "
                "is_base_month) VALUES ('2025-02','fake.csv',1,"
                "'2026-01-01T00:00:00+00:00',0)"
            )
            fake_id = cur.lastrowid
            conn.execute(
                "UPDATE prices SET upload_id=? WHERE month='2025-02'", (fake_id,)
            )
        finally:
            conn.close()
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        self.assertEqual(detail["status"], "stale")
        self.assertTrue(any("批次" in s for s in detail["stale_info"]))

    def test_cached_factor_mismatch_marked_stale(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE index_months SET mom_factor = 1.5 WHERE month='2025-02'"
            )
        finally:
            conn.close()
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        self.assertEqual(detail["status"], "stale")
        self.assertTrue(any("重算" in s for s in detail["stale_info"]))

    def test_tampered_previous_month_marks_current_stale(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.5)]:
            self.upload(month, [("A", "土豆", price)])
        conn = self.conn()
        try:
            conn.execute("UPDATE prices SET price = 88 WHERE month='2025-02'")
        finally:
            conn.close()
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-03")
        finally:
            conn.close()
        self.assertEqual(detail["status"], "stale")
        self.assertTrue(any("2025-02" in s for s in detail["stale_info"]))

    def test_reupload_clears_stale(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        conn = self.conn()
        try:
            conn.execute("UPDATE prices SET price = 99 WHERE month='2025-02'")
        finally:
            conn.close()
        # 重新走正常上传路径，触发重算，缓存与报价重新一致
        self.upload("2025-02", [("A", "土豆", 3.0)])
        conn = self.conn()
        try:
            from app import contrib

            detail = contrib.contribution_detail(conn, "2025-02")
        finally:
            conn.close()
        self.assertEqual(detail["status"], "ok")
        self.assertTrue(detail["decomposable"])
        self.assertAlmostEqual(detail["avg_factor"], 3.0, places=6)


class TestStaleDemo(Base):
    def test_demo_stale_action_and_recovery(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        resp = self.client.post("/demo/stale", follow_redirects=True)
        self.assertIn("已直接篡改", resp.get_data(as_text=True))
        page = self.client.get("/contrib?month=2025-02").get_data(as_text=True)
        self.assertIn("缓存已过期", page)
        # 3 月依赖 2 月报价，同样应判过期
        page3 = self.client.get("/contrib?month=2025-03").get_data(as_text=True)
        self.assertIn("缓存已过期", page3)
        # 正常更正演示会重算，过期消除
        self.client.post("/demo/correct", follow_redirects=True)
        page = self.client.get("/contrib?month=2025-02").get_data(as_text=True)
        self.assertIn("对环比系数的贡献", page)
        self.assertNotIn("缓存已过期", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
