# -*- coding: utf-8 -*-
"""路由层：管理后台 · 系统设置（集中管理站点配置与邮件配置）

分组：
  basic     基本信息（站点名称、副标题、公告、默认每页条数、开放注册）
  mail      SMTP 发信服务（服务器、端口、账号、密码、发信人、加密方式）
  features  邮件功能开关（邮箱注册、自动激活、找回密码、重要操作通知）

约定：
- 所有配置存 `setting` 表，页面永远展示「当前生效值」；
- 每个分组独立表单，独立校验、独立保存，互不影响；
- 每个分组都有「恢复默认」，默认值集中在 config.py，避免散落在模板里；
- SMTP 密码入库前加密（mailer.encrypt_secret），页面只回显是否已保存。
"""
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

import mailer
from config import Config
from extensions import db
from models import Setting
from utils import admin_required, log_action

bp = Blueprint("settings", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")

TOGGLE_KEYS = ("smtp_enabled", "email_register", "email_reset", "email_notify",
               "email_auto_activate")

# 分组 -> 属于该组的配置键（重置默认值时按组清理）
GROUP_KEYS = {
    "basic": ("site_name", "site_subtitle", "default_per_page",
              "dashboard_notice", "allow_register"),
    "mail": ("smtp_host", "smtp_port", "smtp_user", "smtp_password_enc",
             "smtp_security", "smtp_sender_name", "smtp_enabled"),
    "features": ("email_register", "email_reset", "email_notify",
                 "email_auto_activate", "email_notify_events"),
}


def _defaults() -> dict:
    """全部默认值：站点设置 + 邮件设置"""
    data = dict(Config.SITE_DEFAULTS)
    data.update(Config.MAIL_DEFAULTS)
    return data


def _current() -> dict:
    """当前生效配置（未保存过的键回落默认值）"""
    defaults = _defaults()
    rows = {s.key: s.value for s in db.session.scalars(db.select(Setting)).all()}
    cfg = {k: rows.get(k, v) for k, v in defaults.items()}
    cfg["smtp_password"] = mailer.decrypt_secret(cfg.get("smtp_password_enc", ""))
    cfg["has_password"] = bool(cfg.get("smtp_password_enc"))
    try:
        cfg["smtp_port"] = int(str(cfg.get("smtp_port") or 465))
    except ValueError:
        cfg["smtp_port"] = 465
    try:
        cfg["default_per_page"] = int(str(cfg.get("default_per_page") or 20))
    except ValueError:
        cfg["default_per_page"] = 20
    for key in TOGGLE_KEYS + ("allow_register",):
        cfg[key] = str(cfg.get(key, "0")).strip().lower() in ("1", "true", "on", "yes")
    events = str(cfg.get("email_notify_events") or "")
    cfg["notify_events_list"] = [e.strip() for e in events.split(",") if e.strip()]
    return cfg


def _render(cfg=None, errors=None, error_group=""):
    cfg = cfg or _current()
    return render_template(
        "settings/index.html",
        cfg=cfg,
        errors=errors or {},
        error_group=error_group,
        security_options=Config.MAIL_SECURITY,
        notify_events=Config.NOTIFY_EVENTS,
        per_page_options=Config.PER_PAGE_OPTIONS,
        site_name_range=(Config.SITE_NAME_MIN, Config.SITE_NAME_MAX),
    )


# ============================== 基础信息 ==============================
@bp.route("/", methods=["GET"])
@login_required
@admin_required
def index():
    return _render()


@bp.route("/basic", methods=["POST"])
@login_required
@admin_required
def save_basic():
    site_name = (request.form.get("site_name") or "").strip()
    subtitle = (request.form.get("site_subtitle") or "").strip()
    notice = (request.form.get("dashboard_notice") or "").strip()[:500]
    per_page = request.form.get("default_per_page", "20")
    allow_register = request.form.get("allow_register") == "1"

    errors = {}
    if not (Config.SITE_NAME_MIN <= len(site_name) <= Config.SITE_NAME_MAX):
        errors["site_name"] = f"站点名称需 {Config.SITE_NAME_MIN}~{Config.SITE_NAME_MAX} 个字符"
    if len(subtitle) > 60:
        errors["site_subtitle"] = "副标题不超过 60 个字符"
    if str(per_page) not in [str(x) for x in Config.PER_PAGE_OPTIONS]:
        errors["default_per_page"] = "每页条数只能选择 10 / 20 / 50 / 100"

    if errors:
        snap = _current()
        snap.update({"site_name": site_name, "site_subtitle": subtitle,
                     "dashboard_notice": notice,
                     "default_per_page": int(per_page) if per_page.isdigit() else 20,
                     "allow_register": allow_register})
        flash("基础设置未保存：" + "；".join(errors.values()), "danger")
        return _render(snap, errors, "basic")

    Setting.set("site_name", site_name)
    Setting.set("site_subtitle", subtitle)
    Setting.set("dashboard_notice", notice)
    Setting.set("default_per_page", per_page)
    Setting.set("allow_register", "1" if allow_register else "0")
    db.session.commit()
    log_action("修改", "系统设置", None, "更新基础设置")
    flash("基础设置已保存并生效", "success")
    return redirect(url_for("settings.index") + "#basic")


# ============================== 邮件服务 ==============================
@bp.route("/mail", methods=["POST"])
@login_required
@admin_required
def save_mail():
    form = request.form
    host = (form.get("smtp_host") or "").strip()
    port_raw = (form.get("smtp_port") or "").strip()
    user = (form.get("smtp_user") or "").strip()
    sender = (form.get("smtp_sender_name") or "").strip() or "客户管理系统"
    security = form.get("smtp_security") or "ssl"
    password = form.get("smtp_password") or ""
    clear_pwd = form.get("clear_password") == "1"
    enable = form.get("smtp_enabled") == "1"

    errors = {}
    if enable and not host:
        errors["smtp_host"] = "启用发信前必须填写 SMTP 服务器"
    if host and not HOST_RE.match(host):
        errors["smtp_host"] = "只填域名或 IP，例如 smtp.qq.com"
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        port = 465
        errors["smtp_port"] = "端口需为 1 ~ 65535 的整数"
    if enable and not user:
        errors["smtp_user"] = "启用发信前必须填写发件邮箱"
    if user and not EMAIL_RE.match(user):
        errors["smtp_user"] = "发件邮箱格式不正确"
    if security not in Config.MAIL_SECURITY:
        errors["smtp_security"] = "加密方式不合法"
    if enable and not password and not Setting.get("smtp_password_enc"):
        errors["smtp_password"] = "启用发信前请填写 SMTP 密码 / 授权码"
    if len(sender) > 32:
        errors["smtp_sender_name"] = "发信人名称不超过 32 个字符"

    if errors:
        snap = _current()
        snap.update({"smtp_host": host, "smtp_port": port, "smtp_user": user,
                     "smtp_sender_name": sender, "smtp_security": security,
                     "smtp_enabled": enable})
        flash("邮件配置未保存：" + "；".join(errors.values()), "danger")
        return _render(snap, errors, "mail")

    Setting.set("smtp_host", host)
    Setting.set("smtp_port", port)
    Setting.set("smtp_user", user)
    Setting.set("smtp_sender_name", sender)
    Setting.set("smtp_security", security)
    if clear_pwd:
        Setting.set("smtp_password_enc", "")
    elif password:
        Setting.set("smtp_password_enc", mailer.encrypt_secret(password))
    Setting.set("smtp_enabled", "1" if enable else "0")
    if not enable:
        # 关闭总开关时，依赖它的功能一并关闭，避免"看起来开着但发不出去"
        for key in ("email_register", "email_reset", "email_notify"):
            Setting.set(key, "0")
    db.session.commit()
    mailer.invalidate_cache()
    log_action("修改", "系统设置", None, "更新 SMTP 邮件配置")
    flash("邮件服务配置已保存" + ("，密码已更新" if password else ""), "success")
    return redirect(url_for("settings.index") + "#mail")


@bp.route("/mail/test", methods=["POST"])
@login_required
@admin_required
def mail_test():
    """连接测试：用表单里当前填写的参数直接连（不必先保存），收件人默认发件邮箱"""
    form = request.form
    cfg = {
        "smtp_host": (form.get("smtp_host") or "").strip(),
        "smtp_port": int(form.get("smtp_port") or 465) if (form.get("smtp_port") or "").isdigit() else 465,
        "smtp_user": (form.get("smtp_user") or "").strip(),
        "smtp_security": form.get("smtp_security") or "ssl",
        "smtp_sender_name": (form.get("smtp_sender_name") or "").strip() or "客户管理系统",
        "smtp_password": form.get("smtp_password") or "",
        "smtp_enabled": True,
        "email_notify_events": "",
    }
    if not cfg["smtp_password"]:
        cfg["smtp_password"] = Setting.get("smtp_password_enc") and \
            mailer.decrypt_secret(Setting.get("smtp_password_enc")) or ""

    to = (form.get("test_recipient") or "").strip() or cfg["smtp_user"]
    if not EMAIL_RE.match(to or ""):
        flash("请填写正确的测试收件邮箱", "danger")
        return redirect(url_for("settings.index") + "#mail")
    if not cfg["smtp_host"] or not cfg["smtp_user"]:
        flash("请先填写 SMTP 服务器与发件邮箱再测试", "danger")
        return redirect(url_for("settings.index") + "#mail")

    ok, msg = mailer.send_test(to, cfg)
    flash(("连接测试成功：" if ok else "连接测试失败：") + msg, "success" if ok else "danger")
    if ok:
        log_action("测试", "系统设置", None, f"SMTP 连接测试成功（收件人 {to}）")
    return redirect(url_for("settings.index") + "#mail")


# ============================== 邮件功能开关 ==============================
@bp.route("/features", methods=["POST"])
@login_required
@admin_required
def save_features():
    form = request.form
    want = {k: form.get(k) == "1" for k in
            ("email_register", "email_reset", "email_notify", "email_auto_activate")}

    errors = {}
    if any(want.values()):
        mail_cfg = mailer.load_config(refresh=True)
        if not (mail_cfg.get("smtp_enabled") and mail_cfg.get("smtp_host")
                and mail_cfg.get("smtp_user")):
            errors["features"] = "这些功能依赖发信服务，请先在「邮件服务」中启用并保存 SMTP 配置"

    events = [e for e in form.getlist("notify_events") if e in Config.NOTIFY_EVENTS]
    if want["email_notify"] and not events:
        errors["notify_events"] = "开启邮件通知时至少要选择一个通知事件"

    if errors:
        snap = _current()
        snap.update(want)
        snap["notify_events_list"] = events
        flash("功能开关未保存：" + "；".join(errors.values()), "danger")
        return _render(snap, errors, "features")

    for key, val in want.items():
        Setting.set(key, "1" if val else "0")
    Setting.set("email_notify_events", ",".join(events))
    db.session.commit()
    mailer.invalidate_cache()
    log_action("修改", "系统设置", None,
               "更新邮件功能开关：" + "、".join(k for k, v in want.items() if v) or "全部关闭")
    flash("邮件功能开关已保存", "success")
    return redirect(url_for("settings.index") + "#features")


# ============================== 恢复默认 ==============================
@bp.route("/reset/<group>", methods=["POST"])
@login_required
@admin_required
def reset_group(group: str):
    if group not in GROUP_KEYS:
        flash("未知的设置分组", "danger")
        return redirect(url_for("settings.index"))

    defaults = _defaults()
    for key in GROUP_KEYS[group]:
        if key in defaults:
            Setting.set(key, defaults[key])
        else:
            row = db.session.get(Setting, key)
            if row is not None:
                db.session.delete(row)
    db.session.commit()
    mailer.invalidate_cache()
    label = {"basic": "基础设置", "mail": "邮件服务", "features": "邮件功能开关"}.get(group, group)
    log_action("重置", "系统设置", None, f"将「{label}」恢复为默认值")
    flash(f"「{label}」已恢复默认值", "success")
    return redirect(url_for("settings.index") + f"#{group}")
