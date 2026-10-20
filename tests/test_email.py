# -*- coding: utf-8 -*-
"""SMTP 邮件功能自测（在临时库上跑，完全离线：用假 SMTP 服务器替代真实连接）

    .venv/Scripts/python.exe tests/test_email.py

覆盖：配置保存与密码加密 / 连接测试（成功与认证失败）/ 管理员权限 /
      邮箱注册 + 验证激活 / 邮箱找回密码 / 重要操作邮件通知
"""
import os
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="crm_mail_"))
os.environ["CRM_DB_PATH"] = str(TMP / "crm.db")
os.environ["CRM_UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["CRM_AVATAR_DIR"] = str(TMP / "avatars")
os.environ["CRM_DATA_DIR"] = str(TMP)
# 本脚本在临时库上自举一个管理员，口令由环境变量注入，避免任何固定默认口令
os.environ["CRM_ADMIN_PASSWORD"] = "TestOnly_%s" % TMP.name[-6:]
ADMIN_PASS = os.environ["CRM_ADMIN_PASSWORD"]

import smtplib  # noqa: E402

from app import create_app  # noqa: E402
from bootstrap import bootstrap  # noqa: E402
from extensions import db  # noqa: E402
import mailer  # noqa: E402
from models import EmailToken, Setting, User  # noqa: E402

ok_count = 0
fail = []
SENT = []


def check(name, cond, extra=""):
    global ok_count
    if cond:
        ok_count += 1
        print(f"  [PASS] {name} {extra}")
    else:
        fail.append(name)
        print(f"  [FAIL] {name} {extra}")


class FakeSMTP:
    """假 SMTP：密码为 goodpass 才认证成功，其余抛认证错误（与真实行为一致）"""
    def __init__(self, *a, **k):
        pass

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if password != "goodpass":
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def sendmail(self, sender, to, message):
        SENT.append((sender, list(to), message))

    def quit(self):
        pass


app = create_app()
app.config["WTF_CSRF_ENABLED"] = False
with app.app_context():
    bootstrap(app)

client = app.test_client()


def login(username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def main():
    # ---------- 管理员登录 ----------
    r = login("admin", ADMIN_PASS)
    check("管理员登录成功", "经营概览" in r.text)

    # ---------- 邮件设置页可访问 ----------
    r = client.get("/settings/")
    check("系统设置页可访问", r.status_code == 200 and "SMTP 服务器" in r.text)
    check("设置页按分组展示", "基础信息" in r.text and "邮件功能" in r.text)

    # ---------- 基础设置：校验 + 生效 + 恢复默认 ----------
    r = client.post("/settings/basic", data={"site_name": "X", "default_per_page": "20"},
                    follow_redirects=True)
    check("站点名称过短被拦截", "站点名称需" in r.text)
    r = client.post("/settings/basic", data={
        "site_name": "运维客户台账", "site_subtitle": "测试副标题",
        "default_per_page": "50", "dashboard_notice": "本周六 22:00 系统维护",
        "allow_register": "1",
    }, follow_redirects=True)
    check("基础设置保存成功", "基础设置已保存" in r.text)
    check("站点名称已生效", "运维客户台账" in r.text)
    check("首页公告已生效", "本周六 22:00 系统维护" in client.get("/").text)
    r = client.post("/settings/reset/basic", follow_redirects=True)
    check("基础设置恢复默认", "已恢复默认值" in r.text and "客户管理系统" in r.text)

    # ---------- 保存配置：密码加密入库 ----------
    r = client.post("/settings/mail", data={
        "smtp_host": "smtp.test.local", "smtp_port": "465", "smtp_user": "notice@test.local",
        "smtp_sender_name": "测试发件人", "smtp_security": "ssl",
        "smtp_password": "goodpass", "smtp_enabled": "1",
    }, follow_redirects=True)
    check("邮件配置保存成功", "邮件服务配置已保存" in r.text)
    r = client.post("/settings/features", data={
        "email_register": "1", "email_reset": "1", "email_notify": "1",
        "email_auto_activate": "1", "notify_events": ["新增", "删除", "对账"],
    }, follow_redirects=True)
    check("功能开关保存成功", "功能开关已保存" in r.text)

    r = client.post("/settings/mail", data={
        "smtp_host": "smtp.test.local", "smtp_port": "abc", "smtp_user": "bad-email",
        "smtp_security": "ssl", "smtp_password": "goodpass", "smtp_enabled": "1",
    }, follow_redirects=True)
    check("端口非法被拦截", "端口需为" in r.text)
    check("邮箱格式非法被拦截", "发件邮箱格式不正确" in r.text)

    with app.app_context():
        raw = Setting.get("smtp_password_enc")
        check("密码非明文入库", raw and "goodpass" not in raw and raw.startswith("v1:"))
        with app.test_request_context():
            check("密码可解密还原", mailer.decrypt_secret(raw) == "goodpass")
        cfg = mailer.load_config(refresh=True)
        check("配置项已生效", cfg["smtp_host"] == "smtp.test.local" and cfg["smtp_enabled"] is True)
        check("通知事件已保存", cfg["email_notify_events"] == "新增,删除,对账")

    # ---------- 连接测试：认证失败 / 成功 ----------
    mailer.smtplib.SMTP_SSL = FakeSMTP
    mailer.smtplib.SMTP = FakeSMTP

    r = client.post("/settings/mail/test", data={
        "smtp_host": "smtp.test.local", "smtp_port": "465", "smtp_user": "notice@test.local",
        "smtp_security": "ssl", "smtp_password": "wrongpass",
    }, follow_redirects=True)
    check("密码错误时提示认证失败", "认证失败" in r.text)

    SENT.clear()
    r = client.post("/settings/mail/test", data={
        "smtp_host": "smtp.test.local", "smtp_port": "465", "smtp_user": "notice@test.local",
        "smtp_security": "ssl", "smtp_password": "goodpass",
    }, follow_redirects=True)
    check("连接测试成功提示", "连接测试成功" in r.text)
    check("测试邮件已投递", len(SENT) == 1 and "notice@test.local" in SENT[0][1])

    # ---------- 关闭总开关时，依赖功能自动失效 ----------
    client.post("/settings/mail", data={
        "smtp_host": "smtp.test.local", "smtp_port": "465", "smtp_user": "notice@test.local",
        "smtp_security": "ssl",
    }, follow_redirects=True)
    r = client.post("/settings/features", data={"email_register": "1"}, follow_redirects=True)
    check("未启用 SMTP 时不能开邮箱注册", "依赖发信服务" in r.text)
    check("总开关关闭时依赖项被自动置为关闭",
          "已启用" not in client.get("/settings/").text.split('id="mail"')[1][:400])

    # 恢复启用状态
    client.post("/settings/mail", data={
        "smtp_host": "smtp.test.local", "smtp_port": "465", "smtp_user": "notice@test.local",
        "smtp_security": "ssl", "smtp_enabled": "1",
    }, follow_redirects=True)
    client.post("/settings/features", data={
        "email_register": "1", "email_reset": "1", "email_notify": "1",
        "email_auto_activate": "1", "notify_events": ["新增", "删除", "对账"],
    }, follow_redirects=True)

    # ---------- 邮箱注册：必填校验 + 验证激活 ----------
    client.get("/logout")
    r = client.post("/register", data={
        "username": "mailuser", "real_name": "邮件用户", "password": "test123456",
        "confirm_password": "test123456",
    }, follow_redirects=True)
    check("邮箱注册时邮箱必填", "请填写邮箱用于接收验证邮件" in r.text)

    SENT.clear()
    r = client.post("/register", data={
        "username": "mailuser", "real_name": "邮件用户", "contact_email": "mailuser@test.local",
        "password": "test123456", "confirm_password": "test123456",
    }, follow_redirects=True)
    check("注册后提示已发验证邮件", "验证邮件" in r.text)
    check("验证邮件已投递", len(SENT) == 1 and "mailuser@test.local" in SENT[0][1])

    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "mailuser"))
        check("新账号初始待审批", user is not None and user.status == "pending")
        token = db.session.scalar(
            db.select(EmailToken).where(EmailToken.user_id == user.id,
                                        EmailToken.purpose == "verify")
        )
        check("验证令牌已生成", token is not None and len(token.token) == 64)
        verify_token = token.token

    r = client.get(f"/verify/{verify_token}", follow_redirects=True)
    check("邮箱验证成功", "邮箱验证成功" in r.text)
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "mailuser"))
        check("验证后账号已激活", user.status == "active" and user.email_verified)
    r = client.get(f"/verify/{verify_token}", follow_redirects=True)
    check("验证链接不能复用", "无效或已过期" in r.text)

    # ---------- 邮箱找回密码 ----------
    SENT.clear()
    r = client.post("/forgot", data={"email": "mailuser@test.local"}, follow_redirects=True)
    check("找回密码提示统一", "重置链接已发送" in r.text)
    check("重置邮件已投递", len(SENT) == 1 and "mailuser@test.local" in SENT[0][1])

    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "mailuser"))
        row = db.session.scalar(
            db.select(EmailToken).where(EmailToken.user_id == user.id,
                                        EmailToken.purpose == "reset")
        )
        check("重置令牌已生成", row is not None)
        reset_token = row.token

    r = client.post(f"/reset/{reset_token}", data={
        "new_password": "newpass123", "confirm_password": "newpass123",
    }, follow_redirects=True)
    check("重置密码成功", "密码已重置" in r.text)
    r = login("mailuser", "newpass123")
    check("新密码可登录", "经营概览" in r.text)
    r = client.get(f"/reset/{reset_token}", follow_redirects=True)
    check("重置链接不能复用", "无效或已过期" in r.text)

    # ---------- 重要操作邮件通知 ----------
    client.get("/logout")
    login("admin", ADMIN_PASS)
    with app.app_context():
        admin_user = db.session.scalar(db.select(User).where(User.username == "admin"))
        admin_user.email = "admin@test.local"     # 通知只发给有邮箱的管理员
        db.session.commit()
    SENT.clear()
    with app.test_request_context():
        from utils import log_action
        log_action("删除", "项目", 999, "测试通知：删除项目")
        time.sleep(0.8)             # 通知是后台线程发送，稍等
    check("重要操作触发邮件通知", len(SENT) >= 1)
    if SENT:
        from email import message_from_string
        msg = message_from_string(SENT[0][2])
        body = ""
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception:
                    pass
        check("通知内容含操作详情", "测试通知：删除项目" in body)

    SENT.clear()
    with app.test_request_context():
        from utils import log_action
        log_action("登录", "系统", 1, "不该通知的普通操作")
        time.sleep(0.5)
    check("非白名单操作不发通知", len(SENT) == 0)

    # ---------- 普通用户不能进邮件设置 ----------
    with app.app_context():
        u = db.session.scalar(db.select(User).where(User.username == "mailuser"))
        u.role = "user"
        db.session.commit()
    client.get("/logout")
    login("mailuser", "newpass123")
    r = client.get("/settings/", follow_redirects=True)
    check("普通用户无权访问系统设置", "仅管理员" in r.text)

    print("\n" + "=" * 50)
    print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print("  - " + name)
        return 1
    print("SMTP 邮件功能自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
