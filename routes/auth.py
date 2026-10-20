# -*- coding: utf-8 -*-
"""路由层：登录 / 注册 / 退出 / 修改密码（含审批状态拦截）+ 邮箱验证 / 找回密码"""
import re
import secrets
import threading
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

import mailer
from extensions import db
from models import EmailToken, User
from utils import log_action, password_problem

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_@.\-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------- 登录失败节流（按来源 IP） ----------
# 账号级锁定（连续 5 次密码错误锁 15 分钟）挡住的是"猜某一个账号"，
# 但换成"很多账号各试几次"就绕过去了，所以再加一层来源 IP 的失败计数。
# 只统计失败、不统计成功，正常使用（含自测脚本反复登录）不会被误伤。
# 计数放在进程内存里：单机单进程足够；若将来多实例部署可换成 Redis。
LOGIN_FAIL_WINDOW = 300          # 统计窗口（秒）
LOGIN_FAIL_LIMIT = 30            # 窗口内同一 IP 允许的失败次数

_login_fails: dict[str, list[float]] = {}
_login_fails_lock = threading.Lock()


def _recent_fails(ip: str, now: float | None = None) -> list[float]:
    """取该 IP 在统计窗口内的失败时间戳（顺带清理过期记录，避免字典无限增长）"""
    now = now or datetime.now().timestamp()
    hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_FAIL_WINDOW]
    if hits:
        _login_fails[ip] = hits
    else:
        _login_fails.pop(ip, None)
    return hits


def login_blocked(ip: str) -> bool:
    with _login_fails_lock:
        return len(_recent_fails(ip)) >= LOGIN_FAIL_LIMIT


def record_login_failure(ip: str) -> None:
    with _login_fails_lock:
        now = datetime.now().timestamp()
        _login_fails[ip] = _recent_fails(ip, now) + [now]


def _find_user(username: str):
    return db.session.scalar(db.select(User).where(User.username == username))


def _safe_next(nxt: str | None) -> str | None:
    """把 ?next= 收敛成「站内相对路径」，其余一律返回 None。

    只放行以单个 `/` 开头的路径，并额外拦掉两类绕过：
      1. 反斜杠：按 URL 规范，浏览器解析 http(s) 时会把 `\\` 视作 `/`，
         于是 Location: /\\evil.com 会被当成 //evil.com 跳到外站；
      2. 带协议或主机名的绝对地址（含 //host 这种协议相对写法）。
    合法的站内跳转（/project/3、/finance/?status=pending）行为完全不变。
    """
    if not nxt:
        return None
    nxt = nxt.strip()
    if not nxt or "\\" in nxt or "\r" in nxt or "\n" in nxt:
        return None
    if not nxt.startswith("/") or nxt.startswith("//"):
        return None
    parts = urlsplit(nxt)
    if parts.scheme or parts.netloc:
        return None
    return nxt


def _find_by_email(email: str):
    return db.session.scalar(
        db.select(User).where(db.func.lower(User.email) == (email or "").lower())
    )


def _issue_token(user, purpose: str) -> str:
    """生成一次性令牌，同时把该用户同用途的旧令牌作废（只用最新一封邮件里的链接）"""
    now = datetime.now()
    ttl = current_app.config.get("EMAIL_TOKEN_TTL", {}).get(purpose, 30)
    old = db.session.scalars(
        db.select(EmailToken).where(
            EmailToken.user_id == user.id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
    ).all()
    for row in old:
        row.used_at = now                       # 作废旧链接

    token = secrets.token_hex(32)
    db.session.add(EmailToken(
        user_id=user.id,
        token=token,
        purpose=purpose,
        expires_at=now + timedelta(minutes=ttl),
    ))
    db.session.commit()
    return token


def _load_token(token: str, purpose: str):
    row = db.session.scalar(db.select(EmailToken).where(EmailToken.token == token))
    if not row or row.purpose != purpose or not row.is_usable:
        return None
    return row


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        ip = request.remote_addr or "?"
        if login_blocked(ip):
            flash("登录尝试过于频繁，请稍后再试", "danger")
            return render_template("auth/login.html")

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = _find_user(username)

        # 锁定检查要放在密码校验之前，否则锁定期间仍会不断消耗算力猜密码
        if user and user.is_locked:
            flash(f"密码连续错误次数过多，账号已临时锁定，请约 {user.lock_remain_minutes} 分钟后再试", "danger")
            return render_template("auth/login.html")

        if not user or not user.check_password(password):
            record_login_failure(ip)
            limit = current_app.config.get("LOGIN_MAX_FAILURES", 5)
            minutes = current_app.config.get("LOGIN_LOCK_MINUTES", 15)
            if user:
                just_locked = user.register_failure(limit, minutes)
                db.session.commit()
                if just_locked:
                    log_action("锁定", "账号", user.id,
                               f"账号 {username} 连续 {limit} 次密码错误，已锁定 {minutes} 分钟")
                    flash(f"密码连续错误 {limit} 次，账号已锁定 {minutes} 分钟", "danger")
                else:
                    remain = max(0, limit - (user.failed_count or 0))
                    flash(f"账号或密码错误，还可尝试 {remain} 次", "danger")
            else:
                flash("账号或密码错误", "danger")
            return render_template("auth/login.html")

        # 审批状态校验：不同状态给出明确、可操作的提示
        if user.status == "pending":
            flash("账号已提交注册，正在等待管理员审批，通过后即可登录", "warning")
        elif user.status == "rejected":
            reason = f"：{user.review_remark}" if user.review_remark else ""
            flash(f"注册申请未通过{reason}，如需使用请联系管理员", "danger")
        elif user.status == "disabled":
            flash("账号已被停用，请联系管理员恢复", "danger")
        else:
            user.reset_failures()
            # 先换新会话再登录，避免会话固定攻击（clear 放在 login_user 之后会连登录态一起清掉）
            session.clear()
            login_user(user, remember=bool(request.form.get("remember")))
            user.last_login = datetime.now()
            db.session.commit()
            session["last_seen"] = datetime.now().timestamp()
            session.permanent = True
            log_action("登录", "系统", user.id, f"用户 {username} 登录成功")
            nxt = _safe_next(request.args.get("next"))
            if nxt:
                return redirect(nxt)
            return redirect(url_for("index"))

        return render_template("auth/login.html")

    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    """注册：提交后状态为待审批，需管理员在「账号管理」中通过才能登录"""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    from models import Setting
    # 环境变量关掉注册时优先级最高（公网部署常用），否则以后台开关为准
    allow = Setting.get_bool("allow_register", current_app.config.get("ALLOW_REGISTER", True))
    if not current_app.config.get("ALLOW_REGISTER", True):
        allow = False
    if not allow:
        flash("系统暂未开放注册，请联系管理员开通账号", "warning")
        return redirect(url_for("auth.login"))

    form = {}
    if request.method == "POST":
        form = request.form
        username = (request.form.get("username") or "").strip()
        real_name = (request.form.get("real_name") or "").strip()
        email = (request.form.get("contact_email") or "").strip() or \
            (request.form.get("email") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        wechat = (request.form.get("wechat") or "").strip()
        department = (request.form.get("department") or "").strip()
        reason = (request.form.get("apply_reason") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        errors = []
        email_register = mailer.feature_enabled("email_register")
        if not USERNAME_RE.match(username):
            errors.append("账号需为 3-32 位字母、数字或 _ @ . - 组合")
        elif _find_user(username):
            errors.append(f"账号「{username}」已被注册，请换一个")
        if not real_name:
            errors.append("请填写姓名")
        if email and not EMAIL_RE.match(email):
            errors.append("邮箱格式不正确")
        if email_register:
            if not email:
                errors.append("已启用邮箱注册，请填写邮箱用于接收验证邮件")
            elif email and _find_by_email(email):
                errors.append("该邮箱已被其他账号使用，请换一个或直接找回密码")
        if phone and not re.match(r"^[0-9+\-\s]{6,20}$", phone):
            errors.append("手机号格式不正确")
        problem = password_problem(password)
        if problem:
            errors.append(f"密码不符合要求：{problem}")
        elif password != confirm:
            errors.append("两次输入的密码不一致")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/register.html", form=form,
                                   email_register=mailer.feature_enabled("email_register"))

        user = User(
            username=username,
            real_name=real_name,
            email=email,
            phone=phone,
            wechat=wechat,
            department=department,
            apply_reason=reason,
            role="user",
            status="pending",
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_action("注册", "账号", user.id, f"新账号注册待审批：{username}（{real_name}）",
                   username=username)

        if email_register:
            token = _issue_token(user, "verify")
            link = url_for("auth.verify_email", token=token, _external=True)
            sent, msg = mailer.send_verify_mail(user, link)
            if sent:
                flash(f"验证邮件已发送至 {email}，请在 24 小时内完成验证", "success")
            else:
                flash(f"验证邮件发送失败：{msg}。可联系管理员在「账号管理」中手动通过申请", "warning")

        return redirect(url_for("auth.register_done", username=username))

    return render_template("auth/register.html", form=form,
                           email_register=mailer.feature_enabled("email_register"))


# ============================== 邮箱验证 / 找回密码 ==============================
@bp.route("/verify/<token>")
def verify_email(token: str):
    row = _load_token(token, "verify")
    if not row:
        flash("验证链接无效或已过期，请让管理员重新发送验证邮件", "danger")
        return redirect(url_for("auth.login"))

    user = row.user
    row.used_at = datetime.now()
    user.email_verified = True

    activated = False
    cfg = mailer.load_config()
    if cfg.get("email_auto_activate") and user.status == "pending":
        user.status = "active"
        user.approve_time = datetime.now()
        user.approved_by = "邮箱验证"
        activated = True
    db.session.commit()
    log_action("审批", "账号", user.id,
               f"邮箱验证通过：{user.username}" + ("，自动激活" if activated else "，等待管理员审批"),
               username=user.username)
    flash("邮箱验证成功" + ("，账号已激活，请登录" if activated else "，请等待管理员审批"),
          "success")
    return redirect(url_for("auth.login"))


@bp.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    """找回密码：输入邮箱 -> 发送重置链接；无论邮箱是否存在都返回同样的提示，避免枚举账号"""
    if not mailer.feature_enabled("email_reset"):
        flash("系统未开启邮箱找回密码，请联系管理员重置密码", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not EMAIL_RE.match(email):
            flash("请输入正确的邮箱地址", "danger")
            return render_template("auth/forgot.html")
        user = _find_by_email(email)
        if user and user.status == "active":
            token = _issue_token(user, "reset")
            link = url_for("auth.reset_password", token=token, _external=True)
            ok, msg = mailer.send_reset_mail(user, link)
            if not ok:
                flash(f"邮件发送失败：{msg}，请联系管理员重置密码", "danger")
                return render_template("auth/forgot.html")
        flash("如果该邮箱已绑定账号，重置链接已发送，请查收（含垃圾邮件箱）", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    row = _load_token(token, "reset")
    if not row:
        flash("重置链接无效或已过期，请重新申请", "danger")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        problem = password_problem(new)
        if problem:
            flash(f"新密码不符合要求：{problem}", "danger")
        elif new != confirm:
            flash("两次输入的密码不一致", "danger")
        else:
            user = row.user
            user.set_password(new)
            # 重置密码通常意味着原密码已遗忘，此时失败计数与临时锁定必须一并清掉，
            # 否则用户改完密码仍要等锁定到期，看起来像"重置没生效"。
            user.reset_failures()
            row.used_at = datetime.now()
            db.session.commit()
            log_action("重置密码", "账号", user.id, f"通过邮箱重置密码：{user.username}",
                       username=user.username)
            flash("密码已重置，请用新密码登录", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/reset.html", token=token, email=row.user.email)


@bp.route("/register/done")
def register_done():
    """注册成功等待审批页"""
    return render_template("auth/register_done.html", username=request.args.get("username", ""))


@bp.route("/logout")
@login_required
def logout():
    log_action("退出", "系统", current_user.id, f"用户 {current_user.username} 退出登录")
    logout_user()
    flash("已安全退出", "success")
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not current_user.check_password(old):
            flash("原密码不正确", "danger")
        elif password_problem(new):
            flash(f"新密码不符合要求：{password_problem(new)}", "danger")
        elif new != confirm:
            flash("两次输入的新密码不一致", "danger")
        else:
            current_user.set_password(new)
            db.session.commit()
            log_action("修改", "账号", current_user.id, "修改登录密码")
            flash("密码修改成功，请重新登录", "success")
            logout_user()
            return redirect(url_for("auth.login"))

    return render_template("auth/change_password.html")
