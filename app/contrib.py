"""月度环比贡献明细。

指数调查员用它解释某月环比系数由哪些「同摊同品种」报价对构成：

- 贡献 = 该对（本月价 / 上月价）÷ 可比报价对数；
  所有贡献之和即该月环比系数（等权平均）。
- 明细按贡献绝对值降序排列，绝对值相同按摊位、品种排序。

一致性原则（沿用指数缓存的设计）：
- 指数缓存 index_months 记录了算结果时所用的上传批次 source_upload_id。
- 明细只能使用与缓存同一批次的报价。若当前报价的批次与缓存不一致，
  或按当前报价重算的对数/系数与缓存值对不上，则缓存已过期：
  只展示「过期」说明，不返回任何混合结果。
- 缺月、无可比报价对、断链等情形同样不做分解，只给出原因。

所有读取包在单个读事务内，保证缓存与报价来自同一次快照。
"""

from __future__ import annotations

import sqlite3

from . import calc, db

CONTRIB_DECIMALS = 10
RATIO_DECIMALS = 6
PRICE_DECIMALS = 2

# 状态码 → 中文说明
REASON = {
    "base": "基月没有环比，无需分解。",
    "missing": "该月没有已提交报价（缺月），环比不可分解。",
    "no_pairs": "本月与上月没有同摊位同品种的可比报价对，环比不可分解。",
    "broken": "环比链已断（断链：前序月份缺月或无可比报价对），该月环比不可分解。",
    "stale": (
        "指数缓存与当前报价不一致，缓存已过期。请重新上传该月或相邻月份报价"
        "触发重算后再查看，本页不展示混合结果。"
    ),
}


def _index_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        r["month"]: r
        for r in conn.execute("SELECT * FROM index_months").fetchall()
    }


def _batch_is_current(
    conn: sqlite3.Connection, month: str, source_upload_id: int | None
) -> bool:
    """该月当前生效报价是否全部来自缓存引用的那一个上传批次。"""
    if source_upload_id is None:
        return False
    ids = db.distinct_price_upload_ids(conn, month)
    if not ids:
        return False
    return ids == {source_upload_id}


def contribution_detail(
    conn: sqlite3.Connection, month: str
) -> dict:
    """在单个读事务内构建某月贡献明细。

    返回 dict，字段：
    - month / prev_month / selectable_months / selected
    - decomposable: 是否可分解
    - status: ok / stale / base / missing / no_pairs / broken / unknown
    - reason: 不可分解时的中文说明
    - rows: 明细列表（仅 decomposable 时非空）
    - pair_count / avg_factor: 汇总（取自指数缓存）
    - stale_info: 过期诊断细节
    - current / previous: 引用批次与缓存元信息
    """
    conn.execute("BEGIN")
    try:
        rows_index = conn.execute(
            "SELECT month FROM index_months ORDER BY month"
        ).fetchall()
        selectable = [r["month"] for r in rows_index]

        result: dict = {
            "month": month,
            "prev_month": db.previous_month(month),
            "selectable_months": selectable,
            "selected": month if month in selectable else None,
            "decomposable": False,
            "status": "unknown",
            "reason": "",
            "rows": [],
            "pair_count": None,
            "avg_factor": None,
            "stale_info": [],
            "current": None,
            "previous": None,
        }

        if month not in selectable:
            result["status"] = "unknown"
            result["reason"] = (
                f"没有 {month} 的指数缓存，请先通过上传或演示载入该月份报价。"
            )
            return result

        by_month = _index_map(conn)
        row = by_month[month]
        prev_month = db.previous_month(month)
        prev_row = by_month.get(prev_month)

        result["pair_count"] = row["pair_count"]
        result["avg_factor"] = row["mom_factor"]

        # 1) 结构性不可分解情形（缓存自身语义）
        if row["is_base_month"]:
            result["status"] = "base"
            result["reason"] = REASON["base"]
            return result
        if not row["has_upload"]:
            result["status"] = "missing"
            result["reason"] = REASON["missing"]
            return result
        if row["pair_count"] == 0:
            if prev_row is not None and prev_row["index_value"] is not None:
                result["status"] = "no_pairs"
                result["reason"] = REASON["no_pairs"]
            else:
                result["status"] = "broken"
                result["reason"] = REASON["broken"]
            return result
        if row["index_value"] is None or row["mom_factor"] is None:
            result["status"] = "broken"
            result["reason"] = REASON["broken"]
            return result

        # 2) 一致性核对：缓存引用的上传批次必须仍是当前报价批次
        stale_info: list[str] = []
        current_ok = _batch_is_current(conn, month, row["source_upload_id"])
        if not current_ok:
            stale_info.append(
                f"本月 {month} 的报价批次与指数缓存引用的批次 "
                f"#{row['source_upload_id']} 不一致"
            )
        prev_ok = False
        if prev_row is not None and prev_row["has_upload"]:
            prev_ok = _batch_is_current(
                conn, prev_month, prev_row["source_upload_id"]
            )
            if not prev_ok:
                stale_info.append(
                    f"上月 {prev_month} 的报价批次与其指数缓存引用的批次 "
                    f"#{prev_row['source_upload_id']} 不一致"
                )
        else:
            stale_info.append(f"上月 {prev_month} 缺少指数缓存或报价，无法配对")

        # 3) 只用缓存引用批次的报价重算，值必须与缓存对得上
        cur_prices = (
            db.month_prices_for_upload(conn, month, row["source_upload_id"])
            if current_ok
            else {}
        )
        prev_prices = (
            db.month_prices_for_upload(
                conn, prev_month, prev_row["source_upload_id"]
            )
            if prev_ok
            else {}
        )
        recomputed_count, recomputed_factor = calc.pair_factor(
            cur_prices, prev_prices
        )
        value_ok = (
            current_ok
            and prev_ok
            and recomputed_count == row["pair_count"]
            and recomputed_factor is not None
            and abs(recomputed_factor - row["mom_factor"]) < 1e-9
        )
        if not value_ok and current_ok and prev_ok:
            stale_info.append(
                f"按缓存批次重算 {month} 对 {prev_month} 的环比"
                f"（对数 {recomputed_count}、系数 {recomputed_factor}）"
                f"与缓存值（对数 {row['pair_count']}、系数 {row['mom_factor']}）"
                f"不一致：{month} 或 {prev_month} 的报价已被改动"
            )

        if stale_info:
            result["status"] = "stale"
            result["reason"] = REASON["stale"]
            result["stale_info"] = stale_info
            result["current"] = {
                "source_upload_id": row["source_upload_id"],
                "computed_at": row["computed_at"],
            }
            return result

        # 4) 一致：构建贡献明细（数据全部来自缓存引用的批次，无混合结果）
        pair_count = row["pair_count"]
        details = []
        for key in cur_prices.keys() & prev_prices.keys():
            stall, item = key
            prev_price = prev_prices[key]
            if prev_price <= 0:
                continue
            cur_price = cur_prices[key]
            ratio = cur_price / prev_price
            contribution = ratio / pair_count
            details.append(
                {
                    "stall": stall,
                    "item": item,
                    "prev_price": prev_price,
                    "cur_price": cur_price,
                    "ratio": ratio,
                    "contribution": contribution,
                    "display_prev_price": f"{prev_price:.{PRICE_DECIMALS}f}",
                    "display_cur_price": f"{cur_price:.{PRICE_DECIMALS}f}",
                    "display_ratio": f"{ratio:.{RATIO_DECIMALS}f}",
                    "display_contribution": f"{contribution:.{CONTRIB_DECIMALS}f}",
                }
            )

        # 贡献绝对值降序；同值按摊位、品种排序
        details.sort(
            key=lambda d: (-abs(d["contribution"]), d["stall"], d["item"])
        )

        result["decomposable"] = True
        result["status"] = "ok"
        result["rows"] = details
        result["pair_count"] = pair_count
        result["avg_factor"] = row["mom_factor"]
        result["display_avg_factor"] = (
            f"{row['mom_factor']:.{calc.FACTOR_DECIMALS}f}"
        )
        result["current"] = {
            "source_upload_id": row["source_upload_id"],
            "computed_at": row["computed_at"],
        }
        result["previous"] = {
            "source_upload_id": prev_row["source_upload_id"],
            "computed_at": prev_row["computed_at"],
        }
        return result
    finally:
        conn.execute("COMMIT")
