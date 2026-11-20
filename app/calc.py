"""价格指数计算。

规则：
- 基月指数 = 100。
- 月环比系数 = 相邻两月「同摊位同品种」报价比值（本月/上月）的等权平均。
- 相邻月没有可比报价对（或中间缺月）→ 该月环比为空，且自该月起指数断链留空。
- 指数 = 上月指数 × 本月环比系数。

只重算受影响的环比：某月被补报或更正后，该月与下一月的环比需要重算，
其余月份的环比沿用上一次结果；指数则从基月向后重新串联。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import db

BASE_INDEX = 100.0
FACTOR_DECIMALS = 10
INDEX_DECIMALS = 6


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pair_factor(
    current: dict[tuple[str, str], float],
    previous: dict[tuple[str, str], float],
) -> tuple[int, float | None]:
    """返回（可比报价对数，环比系数）。"""
    ratios = [
        current[key] / previous[key]
        for key in current.keys() & previous.keys()
        if previous[key] > 0
    ]
    if not ratios:
        return 0, None
    return len(ratios), round(sum(ratios) / len(ratios), FACTOR_DECIMALS)


def _latest_upload_ids(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT month, MAX(id) AS upload_id FROM uploads GROUP BY month"
    ).fetchall()
    return {r["month"]: r["upload_id"] for r in rows}


def recompute(
    conn: sqlite3.Connection,
    base_month: str,
    changed_month: str | None = None,
    latest_month: str | None = None,
) -> list[str]:
    """重算并写入 index_months，返回页面展示的月份序列。

    调用方负责事务。changed_month 为空表示全量重算。
    """
    latest = latest_month or db.max_uploaded_month(conn)
    if latest is None or latest < base_month:
        return []
    months = db.month_range(base_month, latest)
    month_set = set(months)

    existing = {
        r["month"]: dict(r)
        for r in conn.execute("SELECT * FROM index_months").fetchall()
    }
    upload_ids = _latest_upload_ids(conn)

    # 需要重算环比系数的月份
    if changed_month is None:
        factor_months = {m for m in months if m > base_month}
    else:
        candidates = {changed_month, db.next_month(changed_month)}
        factor_months = {m for m in candidates if m in month_set and m > base_month}

    factors: dict[str, tuple[int, float | None]] = {}
    price_cache: dict[str, dict[tuple[str, str], float]] = {}

    def prices(m: str) -> dict[tuple[str, str], float]:
        if m not in price_cache:
            price_cache[m] = db.month_prices(conn, m)
        return price_cache[m]

    for m in months:
        if m == base_month:
            factors[m] = (0, None)
            continue
        if m in factor_months or m not in existing:
            factors[m] = pair_factor(prices(m), prices(db.previous_month(m)))
        else:
            prev = existing[m]
            factors[m] = (
                int(prev["pair_count"] or 0),
                prev["mom_factor"],
            )

    # 从基月向后串联指数
    index_values: dict[str, float | None] = {base_month: BASE_INDEX}
    for m in months[1:]:
        prev_index = index_values.get(db.previous_month(m))
        factor = factors[m][1]
        if prev_index is None or factor is None:
            index_values[m] = None
        else:
            index_values[m] = round(prev_index * factor, INDEX_DECIMALS)

    stamp = now_iso()
    rows = []
    for m in months:
        pair_count, factor = factors[m]
        upload_id = upload_ids.get(m)
        if upload_id is None:
            # 该月没有任何已提交报价：不展示环比（缺月）
            pair_count, factor = None, None
        rows.append(
            {
                "month": m,
                "is_base_month": 1 if m == base_month else 0,
                "has_upload": 1 if upload_id is not None else 0,
                "pair_count": pair_count if m != base_month else None,
                "mom_factor": factor,
                "index_value": index_values[m],
                "source_upload_id": upload_id,
                "computed_at": stamp,
            }
        )
    db.upsert_index_rows(conn, rows)

    # 清理范围外的旧结果（正常情况下不存在）
    conn.execute(
        "DELETE FROM index_months WHERE month < ? OR month > ?",
        (base_month, latest),
    )
    return months


def read_index(conn: sqlite3.Connection) -> list[dict]:
    """在一个读事务里读取指数快照，保证对数、环比、指数同源。"""
    conn.execute("BEGIN")
    try:
        rows = [dict(r) for r in db.all_months(conn)]
    finally:
        conn.execute("COMMIT")

    by_month = {r["month"]: r for r in rows}
    for row in rows:
        row["display_index"] = (
            f"{row['index_value']:.2f}" if row["index_value"] is not None else ""
        )
        row["display_factor"] = (
            f"{row['mom_factor']:.4f}" if row["mom_factor"] is not None else ""
        )
        if row["is_base_month"]:
            row["status"] = "base"
        elif not row.get("has_upload"):
            row["status"] = "missing"
        elif row["index_value"] is not None:
            row["status"] = "ok"
        elif row["pair_count"] == 0:
            prev = by_month.get(db.previous_month(row["month"]))
            prev_ok = prev is not None and prev["index_value"] is not None
            row["status"] = "no_pairs" if prev_ok else "broken"
        else:
            row["status"] = "broken"
    return rows
