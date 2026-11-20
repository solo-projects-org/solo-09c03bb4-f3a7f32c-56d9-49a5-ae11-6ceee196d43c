"""内置演示数据：一键复现「缺月 → 断链 → 补报接续 / 更正恢复」。

演示数据直接以 CSV 文本生成，再走与网页上传完全相同的业务路径，
因此演示结果与真实上传一致。
"""

from __future__ import annotations

import csv
import io

from . import db, services

MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]

STALLS = ["A-01", "A-02", "B-01"]
ITEMS = ["大白菜", "土豆", "西红柿"]

# 每月相对基月的价格倍率
SCALE = {
    "2025-01": 1.00,
    "2025-02": 1.05,
    "2025-03": 1.12,
    "2025-04": 1.09,
    "2025-05": 1.18,
}

BASE_PRICES = {
    ("A-01", "大白菜"): 4.00,
    ("A-01", "土豆"): 3.00,
    ("A-02", "大白菜"): 5.00,
    ("A-02", "西红柿"): 7.00,
    ("B-01", "土豆"): 3.00,
    ("B-01", "西红柿"): 6.00,
}

BREAK_STALLS = ["X-01", "Y-01"]
BREAK_PRICES = {
    ("X-01", "大白菜"): 5.10,
    ("X-01", "土豆"): 4.05,
    ("Y-01", "西红柿"): 7.25,
}



class DemoError(Exception):
    pass


def _price(stall: str, item: str, month: str) -> float | None:
    base = BASE_PRICES.get((stall, item))
    if base is None:
        return None
    return round(base * SCALE[month], 2)


def build_csv(month: str, layout: str = "normal") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月份", "摊位", "品种", "每公斤报价"])
    if layout == "normal":
        source = {
            (stall, item): _price(stall, item, month)
            for stall in STALLS
            for item in ITEMS
        }
    else:
        source = dict(BREAK_PRICES)
    for (stall, item), price in source.items():
        if price is None:
            continue
        writer.writerow([month, stall, item, f"{price:.2f}"])
    return buf.getvalue()


def _upload(conn, month: str, layout: str = "normal") -> None:
    raw = build_csv(month, layout).encode("utf-8")
    services.process_upload(conn, raw, f"demo-{month}-{layout}.csv")


def reset(conn) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM prices")
        conn.execute("DELETE FROM index_months")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('prices','uploads')")


def _require_demo(conn) -> None:
    base = services.base_month(conn)
    if base is None:
        raise DemoError("请先点「载入示例数据」")
    if base != MONTHS[0]:
        raise DemoError("当前数据不是示例数据，请先点「载入示例数据」")
    have = {
        r["month"]
        for r in conn.execute("SELECT DISTINCT month FROM uploads").fetchall()
    }
    if "2025-02" not in have or "2025-03" not in have:
        raise DemoError("示例数据不完整，请重新载入")


def run(action: str, conn) -> str:
    if action == "reset":
        reset(conn)
        return "演示数据已清空，可重新载入示例。"

    if action == "load":
        reset(conn)
        for month in MONTHS:
            if month == "2025-04":
                continue  # 故意缺 4 月
            _upload(conn, month)
        return "已载入 2025 年 1、2、3、5 月报价：4 月缺月，5 月因此断链。"

    _require_demo(conn)

    if action == "backfill":
        if conn.execute("SELECT 1 FROM uploads WHERE month='2025-04'").fetchone():
            raise DemoError("4 月已有报价，无需补报（可先点「载入示例数据」重置）")
        _upload(conn, "2025-04")
        return "已补报 2025-04：4 月环比接通，5 月指数随之恢复。"

    if action == "break":
        _upload(conn, "2025-03", layout="break")
        return "已把 2025-03 换成完全不同摊位的报价：3 月起无可比报价对，指数清空。"

    if action == "repair":
        _upload(conn, "2025-03")
        return "已更正 2025-03 报价：只重算 3、4 月环比，指数自 3 月起重连。"

    if action == "correct":
        # 只更正部分摊位：环比不会被整体抵消，后续指数会真实变化
        lines = build_csv("2025-02").splitlines()
        rows = [lines[0]]
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = next(csv.reader([line]))
            if parts[1] == "A-01":
                parts[3] = f"{float(parts[3]) * 1.20:.2f}"
            rows.append(",".join(parts))
        raw = ("\n".join(rows) + "\n").encode("utf-8")
        services.process_upload(conn, raw, "demo-2025-02-corrected.csv")
        return (
            "已把 2025-02 中 A-01 摊位报价上调 20%：只重算 2、3 月环比，"
            "2 月及之后各月指数随之更新。"
        )

    if action == "stale":
        # 只改生效报价、不触发重算：指数缓存保留旧批次算出的结果，
        # 贡献明细页据此识别为「缓存过期」并拒绝展示混合结果。
        with db.transaction(conn):
            cur = conn.execute(
                """
                UPDATE prices SET price = price * 1.30
                WHERE month = '2025-02'
                  AND rowid = (
                      SELECT rowid FROM prices
                      WHERE month = '2025-02'
                      ORDER BY stall, item LIMIT 1
                  )
                """
            )
            if cur.rowcount == 0:
                raise DemoError("2025-02 没有可篡改的报价，请重新载入示例数据")
        return (
            "已直接篡改 2025-02 的一条生效报价但未重算指数："
            "指数缓存与报价不一致，贡献明细页将标为「过期」且不展示混合结果；"
            "重新上传 2025-02（如点「更正」演示）触发重算后恢复。"
        )

    raise DemoError("未知的演示操作")
