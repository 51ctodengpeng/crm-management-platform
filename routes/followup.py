# -*- coding: utf-8 -*-
"""路由层：客户跟进记录

一次沟通 = 一条 follow_up：
- 必须挂在客户下，可再指定具体项目；
- 记录沟通方式、内容、本次日期与下次跟进日期；
- 新增/删除后重算客户上的 last_follow_at / next_follow_at，供列表与待办使用；
- 权限沿用客户编辑权限（能改客户资料的人才能写跟进），避免只读账号篡改过程记录。
"""
from datetime import date

from flask import Blueprint, flash, redirect, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import Customer, FollowUp, Project, refresh_customer_follow
from permissions import can_edit_customer, can_edit_project, has_project_permission, PERM_EDIT
from utils import log_action, option_default, option_items, parse_date

bp = Blueprint("followup", __name__)


def _back(next_url: str, customer: Customer, project: Project | None = None) -> str:
    """返回来源页面：优先用表单里的 next（站内路径），否则回客户/项目详情"""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    if project is not None:
        return url_for("project.detail", pid=project.id) + "#followups"
    return url_for("customer.detail", cid=customer.id) + "#followups"


def _can_write(customer: Customer, project: Project | None) -> bool:
    if can_edit_customer(customer):
        return True
    # 仅被授权某个项目、但没有客户编辑权限时，允许在自己的项目下写跟进
    return bool(project is not None and can_edit_project(project))


@bp.route("/add", methods=["POST"])
@login_required
def add():
    customer_id = request.form.get("customer_id", type=int)
    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer is None:
        flash("客户不存在或已被删除", "danger")
        return redirect(url_for("customer.list_customers"))

    project_id = request.form.get("project_id", type=int)
    project = db.session.get(Project, project_id) if project_id else None
    if project is not None and project.customer_id != customer.id:
        flash("所选项目不属于该客户，请重新选择", "danger")
        return redirect(_back("", customer))

    if not _can_write(customer, project):
        flash("没有该客户的编辑权限，无法写入跟进记录", "danger")
        return redirect(_back("", customer, project))

    content = (request.form.get("content") or "").strip()
    if len(content) < 2:
        flash("请填写跟进内容（至少 2 个字），便于日后回顾", "danger")
        return redirect(_back(request.form.get("next", ""), customer, project))

    followed_at = parse_date(request.form.get("followed_at")) or date.today()
    next_at = parse_date(request.form.get("next_at"))
    if next_at and next_at < followed_at:
        flash("下次跟进日期不能早于本次跟进日期", "danger")
        return redirect(_back(request.form.get("next", ""), customer, project))

    methods = option_items("followup_method")
    method = (request.form.get("method") or "").strip()
    if method not in methods:
        method = option_default("followup_method") or (methods[0] if methods else "电话")

    row = FollowUp(
        customer_id=customer.id,
        project_id=project.id if project else None,
        method=method,
        content=content[:2000],
        followed_at=followed_at,
        next_at=next_at,
        owner=current_user.display_name or current_user.username,
        created_by=current_user.username,
    )
    db.session.add(row)
    db.session.flush()
    refresh_customer_follow(customer)
    db.session.commit()
    log_action(
        "跟进", "客户", customer.id,
        f"客户「{customer.company_name}」新增{method}跟进"
        + (f"（项目：{project.project_name}）" if project else "")
        + (f"，下次跟进 {next_at:%Y-%m-%d}" if next_at else ""),
    )
    flash("跟进记录已保存" + (f"，下次跟进 {next_at:%Y-%m-%d}" if next_at else ""), "success")
    return redirect(_back(request.form.get("next", ""), customer, project))


@bp.route("/<int:fid>/delete", methods=["POST"])
@login_required
def delete(fid: int):
    row = db.get_or_404(FollowUp, fid)
    customer = row.customer
    project = row.project
    if not _can_write(customer, project):
        flash("没有该客户的编辑权限，无法删除跟进记录", "danger")
        return redirect(_back("", customer, project))

    detail = f"客户「{customer.company_name}」删除跟进记录（{row.followed_at} {row.method}）"
    db.session.delete(row)
    db.session.flush()
    refresh_customer_follow(customer)
    db.session.commit()
    log_action("删除", "客户", customer.id, detail)
    flash("跟进记录已删除", "success")
    return redirect(_back(request.form.get("next", ""), customer, project))
