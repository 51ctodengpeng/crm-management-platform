# -*- coding: utf-8 -*-
"""个人中心：维护个人资料（含微信号）、上传 / 删除头像

头像单独存放在 DATA_DIR/avatars，库内只存 UUID 文件名；
管理员可在账号详情页为他人更换头像，普通成员只能改自己的。
"""
from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from extensions import db
from models import User
from utils import avatar_path, log_action, remove_avatar, save_avatar

bp = Blueprint("profile", __name__)


def _safe_next(default_endpoint: str = "profile.edit", **values):
    """只接受站内相对路径，防止开放重定向"""
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default_endpoint, **values)


def _target_user() -> User:
    """取操作目标：默认自己；管理员可通过 uid 指定他人"""
    uid = request.form.get("uid", type=int) or current_user.id
    user = db.get_or_404(User, uid)
    if user.id != current_user.id and not current_user.is_admin:
        abort(403)
    return user


@bp.route("/", methods=["GET", "POST"])
@login_required
def edit():
    if request.method == "POST":
        current_user.real_name = (request.form.get("real_name") or "").strip()
        current_user.wechat = (request.form.get("wechat") or "").strip()[:64]
        current_user.phone = (request.form.get("phone") or "").strip()[:32]
        current_user.email = (request.form.get("email") or "").strip()[:128]
        current_user.department = (request.form.get("department") or "").strip()[:64]
        db.session.commit()
        log_action("修改", "账号", current_user.id, "更新个人资料")
        flash("个人资料已更新", "success")
        return redirect(url_for("profile.edit"))

    return render_template("profile/edit.html")


@bp.route("/avatar", methods=["POST"])
@login_required
def upload_avatar():
    user = _target_user()
    file = request.files.get("avatar")
    if not file or not file.filename:
        flash("请选择要上传的头像图片", "warning")
        return redirect(_safe_next())

    name, err = save_avatar(file)
    if err:
        flash(err, "danger")
        return redirect(_safe_next())

    old = user.avatar
    user.avatar = name
    db.session.commit()
    if old and old != name:
        remove_avatar(old)
    log_action("修改", "账号", user.id, f"更新 {user.username} 的头像")
    flash("头像已更新", "success")
    return redirect(_safe_next())


@bp.route("/avatar/delete", methods=["POST"])
@login_required
def delete_avatar():
    user = _target_user()
    old = user.avatar
    if not old:
        flash("当前没有设置头像", "info")
        return redirect(_safe_next())
    user.avatar = None
    db.session.commit()
    remove_avatar(old)
    log_action("修改", "账号", user.id, f"移除 {user.username} 的头像")
    flash("头像已移除，恢复为姓名首字母", "success")
    return redirect(_safe_next())


@bp.route("/avatar/<path:filename>")
@login_required
def avatar_file(filename: str):
    """头像文件服务：只允许 avatars 目录内的文件名"""
    path = avatar_path(filename)
    if not path or not path.is_file():
        abort(404)
    resp = send_file(path, as_attachment=False, max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp
