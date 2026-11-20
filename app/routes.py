"""路由。"""

from __future__ import annotations

import csv
import io
import logging

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import calc, db, demo, services

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


def _conn():
    if "conn" not in g:
        g.conn = db.connect(current_app.config["DATABASE"])
    return g.conn


@bp.teardown_app_request
def close_conn(_exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


def _snapshot() -> dict:
    conn = _conn()
    rows = calc.read_index(conn)
    return {
        "rows": rows,
        "base_month": services.base_month(conn),
        "latest_month": rows[-1]["month"] if rows else None,
        "uploads": db.recent_uploads(conn, 12),
        "demo_enabled": current_app.config["DEMO_ENABLED"],
    }


@bp.get("/")
def home():
    return render_template("index.html", **_snapshot())


@bp.app_errorhandler(413)
def too_large(_err):
    flash("文件超过 4 MB，请拆分后再上传", "error")
    return redirect(url_for("main.home"))


@bp.get("/healthz")
def healthz():
    _conn().execute("SELECT 1")
    return {"status": "ok"}


@bp.post("/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.home"))
    raw = file.read()
    try:
        result = services.process_upload(_conn(), raw, file.filename)
    except services.UploadError as exc:
        log.warning("上传被拒绝：%s", exc.message)
        flash(exc.message, "error")
        for detail in exc.details:
            flash(detail, "detail")
        return redirect(url_for("main.home"))
    except Exception:
        log.exception("上传失败，已回滚")
        flash("写入失败，已回滚，原有报价和指数保持不变", "error")
        return redirect(url_for("main.home"))

    verb = "替换" if result["replaced"] else "载入"
    flash(
        f"已{verb} {result['month']} 的 {result['rows']} 条报价"
        + ("，该月为基月" if result["month"] == result["base_month"] else ""),
        "success",
    )
    return redirect(url_for("main.home"))


@bp.post("/demo/<action>")
def run_demo(action: str):
    if not current_app.config["DEMO_ENABLED"]:
        flash("演示功能已关闭", "error")
        return redirect(url_for("main.home"))
    try:
        message = demo.run(action, _conn())
    except (services.UploadError, demo.DemoError) as exc:
        detail = getattr(exc, "message", None) or str(exc)
        flash(detail, "error")
        for line in getattr(exc, "details", []):
            flash(line, "detail")
    except Exception:
        log.exception("演示操作失败")
        flash("演示操作失败，已回滚", "error")
    else:
        flash(message, "success")
    return redirect(url_for("main.home"))


@bp.get("/index.csv")
def download_index():
    rows = calc.read_index(_conn())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月份", "可比报价对数", "环比系数", "价格指数", "状态"])
    status_zh = {
        "base": "基月",
        "ok": "正常",
        "missing": "缺月",
        "no_pairs": "无可比报价对",
        "broken": "断链",
    }
    for r in rows:
        writer.writerow(
            [
                r["month"],
                "" if r["pair_count"] is None else r["pair_count"],
                r["display_factor"],
                r["display_index"],
                status_zh.get(r["status"], r["status"]),
            ]
        )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=price-index.csv"},
    )
