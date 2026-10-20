# -*- coding: utf-8 -*-
"""路由层：权限管理（管理员专属）

两层配置：
1. 账号级：数据范围（全部 / 仅授权项目）+ 是否允许新增客户项目；
2. 项目级：为每个项目单独勾选 查看 / 修改 / 删除，权限位向下包含。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_

from extensions import db
from models import Customer, Project, ProjectPermission, User
from permissions import clear_permission_cache
from utils import admin_required, log_action

bp = Blueprint("perm", __name__)

VALID_SCOPES = {"all", "assigned"}


def _perm_stats() -> dict[int, dict]:
    """一次性统计各成员的项目授权分布，避免逐行查询"""
    stats: dict[int, dict] = {}
    for perm in db.session.scalars(db.select(ProjectPermission)).all():
        item = stats.setdefault(
            perm.user_id, {"total": 0, "view": 0, "edit": 0, "delete": 0}
        )
        item["total"] += 1
        if perm.can_view:
            item["view"] += 1
        if perm.can_edit:
            item["edit"] += 1
        if perm.can_delete:
            item["delete"] += 1
    return stats


def _project_options() -> list[Project]:
    """授权表里的项目清单：按客户名、项目名排序，便于按客户成组授权"""
    return list(
        db.session.scalars(
            db.select(Project)
            .join(Customer, Project.customer_id == Customer.id)
            .order_by(Customer.company_name, Project.project_name)
        ).all()
    )


@bp.route("/")
@admin_required
def list_perms():
    keyword = (request.args.get("q") or "").strip()
    stmt = db.select(User).where(User.role != "admin")
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(User.username.like(like), User.real_name.like(like), User.department.like(like))
        )
    members = db.session.scalars(stmt.order_by(User.username)).all()

    stats = _perm_stats()
    total_projects = db.session.scalar(db.select(db.func.count(Project.id))) or 0
    return render_template(
        "perm/list.html",
        members=members,
        stats=stats,
        total_projects=total_projects,
        keyword=keyword,
    )


@bp.route("/<int:uid>", methods=["GET", "POST"])
@admin_required
def edit_perms(uid: int):
    user = db.get_or_404(User, uid)
    if user.is_admin:
        flash("管理员拥有全部数据与全部操作权限，无需单独配置", "info")
        return redirect(url_for("perm.list_perms"))

    if request.method == "POST":
        return _save(user)

    projects = _project_options()
    perms = {p.project_id: p for p in user.project_permissions}
    customers = db.session.scalars(
        db.select(Customer).order_by(Customer.company_name)
    ).all()
    return render_template(
        "perm/edit.html",
        user=user,
        projects=projects,
        perms=perms,
        customers=customers,
        granted_count=sum(1 for p in perms.values() if p.can_view),
    )


def _save(user: User):
    scope = (request.form.get("data_scope") or "assigned").strip()
    if scope not in VALID_SCOPES:
        scope = "assigned"
    old_scope = user.data_scope or "assigned"
    user.data_scope = scope
    user.can_create = bool(request.form.get("can_create"))

    existing = {p.project_id: p for p in user.project_permissions}
    granted = revoked = 0
    for project in _project_options():
        view = bool(request.form.get(f"p{project.id}_view"))
        edit = bool(request.form.get(f"p{project.id}_edit"))
        delete = bool(request.form.get(f"p{project.id}_delete"))
        view, edit, delete = ProjectPermission.normalize(view, edit, delete)

        perm = existing.get(project.id)
        if not (view or edit or delete):
            if perm is not None:
                db.session.delete(perm)
                revoked += 1
            continue

        if perm is None:
            perm = ProjectPermission(user_id=user.id, project_id=project.id)
            db.session.add(perm)
        if (perm.can_view, perm.can_edit, perm.can_delete) != (view, edit, delete):
            granted += 1
            perm.can_view, perm.can_edit, perm.can_delete = view, edit, delete
            perm.granted_by = current_user.username

    db.session.commit()
    clear_permission_cache(user)

    scope_label = "全部数据" if scope == "all" else "仅授权项目"
    log_action(
        "授权",
        "账号",
        user.id,
        f"调整 {user.username} 的数据范围为「{scope_label}」，"
        f"新增/变更 {granted} 项、回收 {revoked} 项项目权限，"
        f"新增客户项目={'允许' if user.can_create else '不允许'}",
    )
    flash(
        f"已保存「{user.display_name}」的权限：数据范围 {scope_label}，"
        f"本次变更 {granted} 项、回收 {revoked} 项",
        "success",
    )
    return redirect(url_for("perm.edit_perms", uid=user.id))


@bp.route("/<int:uid>/reset", methods=["POST"])
@admin_required
def reset_perms(uid: int):
    """清空该成员的全部项目授权，并回到「仅授权项目」范围"""
    user = db.get_or_404(User, uid)
    if user.is_admin:
        flash("管理员无需配置授权", "info")
        return redirect(url_for("perm.list_perms"))

    count = len(user.project_permissions)
    for perm in list(user.project_permissions):
        db.session.delete(perm)
    user.data_scope = "assigned"
    db.session.commit()
    clear_permission_cache(user)
    log_action("授权", "账号", user.id, f"清空 {user.username} 的全部项目授权（{count} 项）")
    flash(f"已清空「{user.display_name}」的 {count} 项项目授权", "success")
    return redirect(url_for("perm.edit_perms", uid=user.id))
