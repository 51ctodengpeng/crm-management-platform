# -*- coding: utf-8 -*-
"""路由层：回收站（查看 / 恢复 / 彻底删除）"""
import json

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from extensions import db
from models import RecycleBin
from recycle import purge, restore
from utils import admin_required, log_action

bp = Blueprint("recycle", __name__)


@bp.route("/")
@login_required
@admin_required
def index():
    """回收站只对管理员开放：删除往往连带整条客户关系，恢复也应由管理员把关"""
    kind = (request.args.get("kind") or "").strip()
    keyword = (request.args.get("q") or "").strip()
    stmt = db.select(RecycleBin)
    if kind in ("customer", "project"):
        stmt = stmt.where(RecycleBin.kind == kind)
    if keyword:
        stmt = stmt.where(RecycleBin.title.contains(keyword))
    rows = db.session.scalars(stmt.order_by(RecycleBin.deleted_at.desc())).all()

    total_size = sum(r.file_size or 0 for r in rows)
    detail = {}
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except ValueError:
            payload = {}
        if row.kind == "customer":
            detail[row.id] = {
                "projects": len(payload.get("projects") or []),
                "followups": len(payload.get("followups") or []),
            }
        else:
            detail[row.id] = {
                "payments": len(payload.get("payments") or []),
                "plans": len(payload.get("plans") or []),
            }
    return render_template(
        "recycle/index.html", rows=rows, kind=kind, keyword=keyword,
        detail=detail, total_size=total_size,
    )


@bp.route("/<int:rid>/restore", methods=["POST"])
@login_required
@admin_required
def restore_row(rid: int):
    row = db.get_or_404(RecycleBin, rid)
    title = row.title
    try:
        name = restore(row)
    except ValueError as exc:
        db.session.rollback()
        flash(f"恢复失败：{exc}", "danger")
        return redirect(url_for("recycle.index"))
    except Exception as exc:                      # 结构不兼容等异常统一兜底
        db.session.rollback()
        flash(f"恢复失败：{exc}", "danger")
        return redirect(url_for("recycle.index"))
    log_action("恢复", row.kind_label, row.origin_id, f"从回收站恢复{row.kind_label}：{name}")
    flash(f"{row.kind_label}「{title}」已恢复", "success")
    target = "customer.list_customers" if row.kind == "customer" else "project.list_projects"
    return redirect(url_for(target))


@bp.route("/<int:rid>/purge", methods=["POST"])
@login_required
@admin_required
def purge_row(rid: int):
    row = db.get_or_404(RecycleBin, rid)
    title, kind_label = row.title, row.kind_label
    removed = purge(row)
    log_action("删除", kind_label, rid, f"从回收站彻底删除{kind_label}：{title}（清理 {removed} 个文件）")
    flash(f"已彻底删除{kind_label}「{title}」，清理 {removed} 个附件文件", "success")
    return redirect(url_for("recycle.index"))


@bp.route("/clear", methods=["POST"])
@login_required
@admin_required
def clear():
    """一键清空回收站（删除全部快照与对应磁盘文件）"""
    rows = db.session.scalars(db.select(RecycleBin)).all()
    count, files = 0, 0
    for row in rows:
        files += purge(row)
        count += 1
    log_action("删除", "系统", None, f"清空回收站：{count} 条记录、{files} 个文件")
    flash(f"回收站已清空（{count} 条记录，清理 {files} 个文件）", "success")
    return redirect(url_for("recycle.index"))
