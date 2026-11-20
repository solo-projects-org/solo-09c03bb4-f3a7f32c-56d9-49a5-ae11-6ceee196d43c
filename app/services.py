"""报价 CSV 解析与上传业务逻辑。"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from . import calc, db

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

HEADER_ALIASES = {
    "month": {"month", "月份", "年月", "报价月份"},
    "stall": {"stall", "摊位", "摊位号", "摊主"},
    "item": {"item", "品种", "蔬菜品种", "菜品"},
    "price": {"price", "价格", "报价", "每公斤报价", "单价", "元/公斤"},
}

MAX_ROWS = 20000
MAX_MONTHS = 600  # 基月到最新报价月最多 50 年


class UploadError(Exception):
    """可直接展示给用户的校验错误。"""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class ParsedRow:
    line: int
    month: str
    stall: str
    item: str
    price: float


@dataclass
class ParseResult:
    month: str
    rows: list[ParsedRow] = field(default_factory=list)


def decode_csv(raw: bytes) -> str:
    if not raw:
        raise UploadError("文件为空")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("无法识别文件编码，请另存为 UTF-8 编码的 CSV")


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿")


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames:
        key = _normalize_header(raw).lower()
        for canonical, aliases in HEADER_ALIASES.items():
            if key in aliases:
                mapping[canonical] = raw
                break
    missing = [c for c in ("month", "stall", "item", "price") if c not in mapping]
    if missing:
        zh = {"month": "月份", "stall": "摊位", "item": "品种", "price": "价格"}
        raise UploadError(
            "CSV 缺少必需列：" + "、".join(zh[c] for c in missing),
            ["表头需包含：月份, 摊位, 品种, 价格（或每公斤报价）"],
        )
    return mapping


def _parse_price(text: str, line: int) -> float:
    value = (text or "").strip().replace("￥", "").replace("¥", "").replace(",", "")
    if not value:
        raise UploadError(f"第 {line} 行：价格为空")
    try:
        price = float(value)
    except ValueError:
        raise UploadError(f"第 {line} 行：价格「{text.strip()}」不是数字") from None
    if price != price or price in (float("inf"), float("-inf")):
        raise UploadError(f"第 {line} 行：价格「{text.strip()}」不是有效数字")
    if price <= 0:
        raise UploadError(f"第 {line} 行：价格必须大于 0，实际为 {text.strip()}")
    return price


def parse_csv(raw: bytes) -> ParseResult:
    text = decode_csv(raw)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise UploadError("CSV 没有表头")
        mapping = _map_headers(list(reader.fieldnames))
        rows: list[ParsedRow] = []
        seen: dict[tuple[str, str], int] = {}
        duplicates: list[str] = []
        bad_months: list[str] = []
        months: set[str] = set()

        for offset, rec in enumerate(reader, start=2):
            if None in rec:
                raise UploadError(f"第 {offset} 行：列数与表头不一致")
            values = [ (rec.get(col) or "").strip() for col in rec ]
            if not any(values):
                continue  # 跳过完全空行
            month = (rec.get(mapping["month"]) or "").strip()
            stall = (rec.get(mapping["stall"]) or "").strip()
            item = (rec.get(mapping["item"]) or "").strip()
            price_text = rec.get(mapping["price"]) or ""

            if not MONTH_RE.match(month):
                bad_months.append(f"第 {offset} 行：月份「{month}」应为 YYYY-MM")
                continue
            if not stall:
                raise UploadError(f"第 {offset} 行：摊位为空")
            if not item:
                raise UploadError(f"第 {offset} 行：品种为空")
            price = _parse_price(price_text, offset)

            key = (stall, item)
            if key in seen:
                duplicates.append(
                    f"摊位「{stall}」品种「{item}」出现在第 {seen[key]} 行和第 {offset} 行"
                )
                continue
            seen[key] = offset
            months.add(month)
            rows.append(ParsedRow(offset, month, stall, item, price))

            if len(rows) > MAX_ROWS:
                raise UploadError(f"单次上传不能超过 {MAX_ROWS} 条报价")
    except csv.Error as exc:
        raise UploadError(f"CSV 解析失败：{exc}") from None

    if bad_months:
        raise UploadError("部分行月份格式不正确", bad_months[:20])
    if duplicates:
        raise UploadError(
            "同一摊位同一品种出现多条报价（同月同摊同品种只收一条）",
            duplicates[:20],
        )
    if not rows:
        raise UploadError("文件中没有有效报价")
    if len(months) > 1:
        raise UploadError(
            "一次只能上传一个月的报价，文件中包含：" + "、".join(sorted(months)),
        )

    return ParseResult(month=rows[0].month, rows=rows)


def base_month(conn) -> str | None:
    return db.get_meta(conn, "base_month")


def process_upload(conn, raw: bytes, filename: str) -> dict:
    """解析 + 整月替换 + 重算指数，单事务完成。"""
    parsed = parse_csv(raw)
    month = parsed.month

    with db.transaction(conn):
        existing_base = base_month(conn)
        if existing_base is None:
            new_base = month
        else:
            new_base = existing_base
            if month < new_base:
                raise UploadError(
                    f"{month} 早于基月 {new_base}，基月之前的报价不接收"
                )
        latest = max(filter(None, (month, db.max_uploaded_month(conn))))
        if len(db.month_range(new_base, latest)) > MAX_MONTHS:
            raise UploadError(f"基月到最新报价月的跨度不能超过 {MAX_MONTHS} 个月")

        cur = conn.execute(
            "INSERT INTO uploads(month, filename, row_count, uploaded_at, is_base_month) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                month,
                filename or "upload.csv",
                len(parsed.rows),
                calc.now_iso(),
                1 if month == new_base else 0,
            ),
        )
        upload_id = cur.lastrowid

        conn.execute("DELETE FROM prices WHERE month = ?", (month,))
        conn.executemany(
            "INSERT INTO prices(upload_id, month, stall, item, price) "
            "VALUES (?, ?, ?, ?, ?)",
            [(upload_id, month, r.stall, r.item, r.price) for r in parsed.rows],
        )

        if existing_base is None:
            db.set_meta(conn, "base_month", new_base)

        calc.recompute(
            conn,
            new_base,
            changed_month=None if existing_base is None else month,
            latest_month=latest,
        )

    return {
        "month": month,
        "rows": len(parsed.rows),
        "upload_id": upload_id,
        "base_month": new_base,
        "replaced": existing_base is not None,
    }
