# -*- coding: utf-8 -*-
"""路由层：账号管理（管理员专属）——审批注册、启用禁用、角色调整、重置密码"""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import case, func, or_

from extensions import db
from models import User
from utils import admin_required, current_per_page, log_action, password_problem

bp = Blueprint("user", __name__)


def _pending_count() -> int:
    return db.session.scalar(
        db.select(func.count(User.id)).where(User.status == "pending")
    ) or 0


def _guard_last_admin(target: User) -> str | None:
    """保护措施：不允许把系统里最后一个管理员降级/停用/删除"""
    if not target.is_admin:
        return None
    remaining = db.session.scalar(
        db.select(func.count(User.id)).where(User.role == "admin", User.id != target.id)
    ) or 0
    if remaining == 0:
        return "系统必须保留至少一名管理员，该操作已被拒绝"
    return None


@bp.route("/")
@admin_required
def list_users():
    page = request.args.get("page", 1, type=int)
    per_page = current_per_page()
    keyword = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    role = (request.args.get("role") or "").strip()

    stmt = db.select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                User.username.like(like),
                User.real_name.like(like),
                User.email.like(like),
                User.phone.like(like),
                User.department.like(like),
            )
        )
    if status:
        stmt = stmt.where(User.status == status)
    if role:
        stmt = stmt.where(User.role == role)

    # 待审批的排最前，其余按注册时间倒序
    stmt = stmt.order_by(
        case((User.status == "pending", 0), else_=1),
        User.create_time.desc(),
        User.id.desc(),
    )
    pagination = db.paginate(stmt, page=page, per_page=per_page, error_out=False)

    counts = dict(
        db.session.execute(db.select(User.status, func.count(User.id)).group_by(User.status)).all()
    )
    return render_template(
        "user/list.html",
        pagination=pagination,
        keyword=keyword,
        status=status,
        role=role,
        per_page=per_page,
        counts=counts,
        pending_count=_pending_count(),
    )


@bp.route("/<int:uid>")
@admin_required
def detail(uid: int):
    user = db.get_or_404(User, uid)
    return render_template("user/detail.html", user=user)


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new_user():
    """管理员直接创建账号（区别于自助注册：无需审批，可直接指定角色与数据范围）"""
    from routes.auth import USERNAME_RE

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        real_name = (request.form.get("real_name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        role = request.form.get("role") or "user"
        status = request.form.get("status") or "active"
        data_scope = request.form.get("data_scope") or "assigned"

        errors = []
        if not USERNAME_RE.match(username):
            errors.append("账号需为 3-32 位字母、数字或 _ @ . - 组合")
        elif db.session.scalar(db.select(User).where(User.username == username)):
            errors.append(f"账号「{username}」已存在，请换一个")
        if not real_name:
            errors.append("请填写姓名")
        problem = password_problem(password)
        if problem:
            errors.append(f"初始密码不符合要求：{problem}")
        elif password != confirm:
            errors.append("两次输入的密码不一致")
        if role not in ("admin", "user"):
            role = "user"
        if status not in ("active", "disabled"):
            status = "active"
        if data_scope not in ("all", "assigned"):
            data_scope = "assigned"

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("user/form.html", form=request.form)

        user = User(
            username=username,
            real_name=real_name,
            email=(request.form.get("email") or "").strip(),
            phone=(request.form.get("phone") or "").strip(),
            wechat=(request.form.get("wechat") or "").strip(),
            department=(request.form.get("department") or "").strip(),
            role=role,
            status=status,
            data_scope="all" if role == "admin" else data_scope,
            can_create=bool(request.form.get("can_create")) or role == "admin",
            approved_by=current_user.username,
            approve_time=datetime.now(),
            review_remark="管理员创建",
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_action(
            "新增", "账号", user.id,
            f"管理员创建账号：{username}（{user.real_name}，角色 {user.role_label}，"
            f"数据范围 {user.scope_label}）",
        )
        flash(f"账号「{username}」创建成功，可直接使用初始密码登录", "success")
        return redirect(url_for("user.detail", uid=user.id))

    return render_template("user/form.html", form={"role": "user", "status": "active",
                                                     "data_scope": "assigned"})


@bp.route("/<int:uid>/approve", methods=["POST"])
@admin_required
def approve(uid: int):
    """通过审批：置为已启用"""
    user = db.get_or_404(User, uid)
    if user.status == "active":
        flash("该账号已是启用状态", "info")
        return redirect(url_for("user.list_users", status="pending"))

    user.status = "active"
    user.approved_by = current_user.username
    user.approve_time = datetime.now()
    user.review_remark = (request.form.get("review_remark") or "").strip() or "审批通过"
    db.session.commit()
    log_action("通过", "账号", user.id, f"审批通过账号：{user.username}（{user.display_name}）")
    flash(f"已通过「{user.display_name}」的注册申请，该账号现在可以登录", "success")
    nxt = request.form.get("next")
    return redirect(nxt or url_for("user.list_users", status="pending"))


@bp.route("/<int:uid>/reject", methods=["POST"])
@admin_required
def reject(uid: int):
    """拒绝注册申请（可附原因，登录时会原样提示）"""
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        flash("不能拒绝自己的账号", "danger")
        return redirect(url_for("user.list_users"))
    if user.is_admin:
        guard = _guard_last_admin(user)
        if guard:
            flash(guard, "danger")
            return redirect(url_for("user.list_users"))

    user.status = "rejected"
    user.approved_by = current_user.username
    user.approve_time = datetime.now()
    user.review_remark = (request.form.get("review_remark") or "").strip() or "不符合使用要求"
    db.session.commit()
    log_action("拒绝", "账号", user.id, f"拒绝账号申请：{user.username}（原因：{user.review_remark}）")
    flash(f"已拒绝「{user.display_name}」的注册申请", "warning")
    return redirect(url_for("user.list_users", status="pending"))


@bp.route("/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_status(uid: int):
    """启用 / 停用 账号（审批通过但暂时不需要权限时使用）"""
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        flash("不能停用自己的账号", "danger")
        return redirect(url_for("user.list_users"))

    if user.status == "active":
        guard = _guard_last_admin(user)
        if guard:
            flash(guard, "danger")
            return redirect(url_for("user.list_users"))
        user.status = "disabled"
        action, msg = "禁用", f"已停用账号「{user.display_name}」"
    else:
        user.status = "active"
        user.approved_by = current_user.username
        user.approve_time = datetime.now()
        action, msg = "启用", f"已启用账号「{user.display_name}」"

    db.session.commit()
    log_action(action, "账号", user.id, f"{action}账号：{user.username}")
    flash(msg, "success")
    return redirect(request.form.get("next") or url_for("user.list_users"))


@bp.route("/<int:uid>/role", methods=["POST"])
@admin_required
def change_role(uid: int):
    """切换管理员 / 普通用户"""
    user = db.get_or_404(User, uid)
    new_role = request.form.get("role") or "user"
    if new_role not in ("admin", "user"):
        flash("角色参数不合法", "danger")
        return redirect(url_for("user.list_users"))
    if user.id == current_user.id and new_role != "admin":
        flash("不能取消自己的管理员权限", "danger")
        return redirect(url_for("user.list_users"))
    if user.role == new_role:
        flash("角色未发生变化", "info")
        return redirect(url_for("user.list_users"))
    if new_role != "admin":
        guard = _guard_last_admin(user)
        if guard:
            flash(guard, "danger")
            return redirect(url_for("user.list_users"))

    user.role = new_role
    db.session.commit()
    label = "管理员" if new_role == "admin" else "普通用户"
    log_action("修改", "账号", user.id, f"将 {user.username} 的角色调整为{label}")
    flash(f"「{user.display_name}」的角色已调整为{label}", "success")
    return redirect(request.form.get("next") or url_for("user.list_users"))


@bp.route("/<int:uid>/unlock", methods=["POST"])
@admin_required
def unlock(uid: int):
    """管理员手工解除登录锁定（用户被锁后等不及 15 分钟时可立即放行）"""
    user = db.get_or_404(User, uid)
    user.reset_failures()
    db.session.commit()
    log_action("解锁", "账号", user.id, f"管理员解除了 {user.username} 的登录锁定")
    flash(f"已解除「{user.display_name}」的登录锁定", "success")
    return redirect(request.form.get("next") or url_for("user.list_users"))


@bp.route("/<int:uid>/reset-password", methods=["POST"])
@admin_required
def reset_password(uid: int):
    """管理员重置密码，重置后必须修改才能继续使用"""
    user = db.get_or_404(User, uid)
    new_password = (request.form.get("new_password") or "").strip()
    problem = password_problem(new_password)
    if problem:
        flash(f"新密码不符合要求：{problem}", "danger")
        return redirect(url_for("user.list_users"))

    user.set_password(new_password)
    db.session.commit()
    log_action("修改", "账号", user.id, f"管理员重置了 {user.username} 的密码")
    flash(f"已重置「{user.display_name}」的密码，请通过安全渠道告知本人", "success")
    return redirect(request.form.get("next") or url_for("user.list_users"))


@bp.route("/<int:uid>/profile", methods=["POST"])
@admin_required
def edit_profile(uid: int):
    """管理员维护成员基础信息（含登录账号名）"""
    from routes.auth import USERNAME_RE

    user = db.get_or_404(User, uid)
    new_username = (request.form.get("username") or "").strip()

    # ---------- 登录账号名：支持修改，需校验格式与唯一性 ----------
    if new_username and new_username != user.username:
        if not USERNAME_RE.match(new_username):
            flash("账号名需为 3-32 位字母、数字或 _ @ . - 组合", "danger")
            return redirect(url_for("user.detail", uid=user.id))
        taken = db.session.scalar(
            db.select(User).where(User.username == new_username, User.id != user.id)
        )
        if taken:
            flash(f"账号名「{new_username}」已被占用，请换一个", "danger")
            return redirect(url_for("user.detail", uid=user.id))
        old_username = user.username
        user.username = new_username
        log_action(
            "修改", "账号", user.id,
            f"修改登录账号：{old_username} → {new_username}",
            username=old_username,
        )
        flash(f"登录账号已由「{old_username}」改为「{new_username}」，请提醒本人使用新账号登录", "success")
    elif not new_username:
        flash("账号名不能为空", "danger")
        return redirect(url_for("user.detail", uid=user.id))

    user.real_name = (request.form.get("real_name") or "").strip()
    user.email = (request.form.get("email") or "").strip()
    user.phone = (request.form.get("phone") or "").strip()
    user.wechat = (request.form.get("wechat") or "").strip()[:64]
    user.department = (request.form.get("department") or "").strip()
    db.session.commit()
    log_action("修改", "账号", user.id, f"更新了 {user.username} 的账号资料")
    flash("账号资料已更新", "success")
    return redirect(url_for("user.detail", uid=user.id))


@bp.route("/<int:uid>/delete", methods=["POST"])
@admin_required
def delete(uid: int):
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        flash("不能删除自己的账号", "danger")
        return redirect(url_for("user.list_users"))
    guard = _guard_last_admin(user)
    if guard:
        flash(guard, "danger")
        return redirect(url_for("user.list_users"))

    username = user.username
    db.session.delete(user)
    db.session.commit()
    log_action("删除", "账号", uid, f"删除账号：{username}")
    flash(f"账号「{username}」已删除", "success")
    return redirect(url_for("user.list_users"))
