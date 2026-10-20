# -*- coding: utf-8 -*-
"""SMTP 邮件模块：配置读取 / 密码可逆加密 / 发信 / 连接测试 / 重要操作通知

设计取舍：
- 配置存 `setting` 表，管理员在「邮件设置」页维护，改配置不用改库结构。
- SMTP 密码必须能还原成明文交给 smtplib，所以只能可逆加密（哈希会登不上）。
  这里用 SECRET_KEY 派生的密钥做 SHA256-CTR 流加密 + HMAC 校验，仅依赖标准库；
  密钥只在 `.secret_key` / 环境变量里，单独拿到 crm.db 无法解出密码。
- 发信失败绝不影响主流程：所有调用点返回 (ok, message)，通知类邮件走后台线程。
"""
import hashlib
import hmac
import os
import smtplib
import ssl
import threading
from base64 import b64decode, b64encode
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from flask import current_app

from extensions import db
from models import Setting

ENC_PREFIX = "v1:"
_cfg_cache = {"at": 0.0, "data": None}


# ============================== 密码可逆加密 ==============================
def _key() -> bytes:
    raw = current_app.config.get("SECRET_KEY") or "insecure-fallback"
    return hashlib.sha256(("crm-smtp::" + str(raw)).encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:size])


def encrypt_secret(plain: str) -> str:
    """明文密码 -> v1:base64(nonce|tag|cipher)，空值返回空串"""
    if not plain:
        return ""
    key = _key()
    nonce = os.urandom(12)
    data = plain.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return ENC_PREFIX + b64encode(nonce + tag + cipher).decode("ascii")


def decrypt_secret(token: str) -> str:
    """密文 -> 明文；密钥变了 / 数据被篡改时返回空串（提示重新填写密码）"""
    if not token or not token.startswith(ENC_PREFIX):
        return token or ""
    try:
        blob = b64decode(token[len(ENC_PREFIX):].encode("ascii"))
        nonce, tag, cipher = blob[:12], blob[12:28], blob[28:]
        key = _key()
        if not hmac.compare_digest(tag, hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]):
            return ""
        data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
        return data.decode("utf-8")
    except Exception:
        return ""


# ============================== 配置读取 ==============================
def load_config(refresh: bool = False) -> dict:
    """读取 SMTP 配置（含解密后的明文密码），带 3 秒进程内缓存避免频繁查库"""
    import time
    now = time.time()
    if not refresh and _cfg_cache["data"] is not None and now - _cfg_cache["at"] < 3:
        return _cfg_cache["data"]

    defaults = dict(current_app.config.get("MAIL_DEFAULTS", {}))
    rows = {s.key: s.value for s in db.session.scalars(db.select(Setting)).all()}
    cfg = {k: rows.get(k, v) for k, v in defaults.items()}
    cfg["smtp_password"] = decrypt_secret(cfg.get("smtp_password_enc", ""))
    cfg["smtp_port"] = _to_int(cfg.get("smtp_port"), 465)

    for key in ("smtp_enabled", "email_register", "email_reset", "email_notify", "email_auto_activate"):
        cfg[key] = str(cfg.get(key, "0")).strip().lower() in ("1", "true", "on", "yes")

    _cfg_cache["data"] = cfg
    _cfg_cache["at"] = now
    return cfg


def invalidate_cache() -> None:
    _cfg_cache["data"] = None
    _cfg_cache["at"] = 0.0


def _to_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def is_ready(cfg: dict | None = None) -> bool:
    """总开关打开且关键项齐全，才允许发信"""
    cfg = cfg or load_config()
    return bool(cfg["smtp_enabled"] and cfg.get("smtp_host") and cfg.get("smtp_user"))


def feature_enabled(name: str) -> bool:
    """功能开关（邮箱注册 / 找回密码 / 通知）需要 SMTP 可用 + 开关打开"""
    cfg = load_config()
    return bool(cfg.get(name)) and is_ready(cfg)


# ============================== 发信 ==============================
def _connect(cfg: dict):
    host = cfg["smtp_host"]
    port = cfg["smtp_port"]
    security = cfg.get("smtp_security") or "ssl"
    timeout = current_app.config.get("MAIL_TIMEOUT", 12)

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
    if cfg.get("smtp_user"):
        server.login(cfg["smtp_user"], cfg.get("smtp_password") or "")
    return server


def _friendly_error(exc: Exception) -> str:
    """把 SMTP 异常翻译成管理员看得懂的话"""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "认证失败：邮箱账号或授权码不正确（QQ/163 需使用「授权码」而非登录密码）"
    if isinstance(exc, (smtplib.SMTPConnectError, ConnectionRefusedError)):
        return f"连接被拒绝：请检查服务器地址与端口是否正确（{text}）"
    if isinstance(exc, ssl.SSLError):
        return f"SSL 握手失败：端口与加密方式可能不匹配（SSL 常用 465，STARTTLS 常用 587）——{text}"
    if isinstance(exc, TimeoutError) or "timed out" in low:
        return "连接超时：服务器无响应，请检查网络、端口或防火墙"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "服务器主动断开：多为加密方式与端口不匹配"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"收件人被拒绝：{text}"
    if "getaddrinfo" in low or "name or service not known" in low:
        return f"域名解析失败：请检查 SMTP 服务器地址（{text}）"
    return text


def build_message(cfg: dict, to: str, subject: str, html: str, text: str = "") -> MIMEMultipart:
    sender_name = cfg.get("smtp_sender_name") or "客户管理系统"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(sender_name, "utf-8")), cfg.get("smtp_user", "")))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text or _strip_html(html), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", html or "").strip()


def send_mail(to: str, subject: str, html: str, text: str = "") -> tuple[bool, str]:
    """同步发信，返回 (是否成功, 提示)。任何异常都被吞掉转成提示。"""
    cfg = load_config()
    if not is_ready(cfg):
        return False, "邮件服务未启用或配置不完整，请先在「邮件设置」中完成配置"
    if not to or "@" not in to:
        return False, "收件邮箱格式不正确"

    server = None
    try:
        msg = build_message(cfg, to, subject, html, text)
        server = _connect(cfg)
        server.sendmail(cfg["smtp_user"], [to], msg.as_string())
        return True, f"已发送到 {to}"
    except Exception as exc:                      # noqa: BLE001 - 统一转成可读提示
        return False, _friendly_error(exc)
    finally:
        try:
            if server is not None:
                server.quit()
        except Exception:
            pass


def send_test(to: str, cfg: dict | None = None) -> tuple[bool, str]:
    """连接测试：用给定配置（未保存也可测）发一封测试邮件"""
    global _cfg_cache
    if cfg is not None:
        backup = _cfg_cache["data"]
        _cfg_cache["data"] = cfg
        try:
            return send_mail(
                to,
                "【测试】客户管理系统 SMTP 连接测试",
                _wrap("SMTP 连接测试成功",
                      f"这是一封测试邮件，收到即表示发件配置可用。<br>"
                      f"发送时间：{datetime.now():%Y-%m-%d %H:%M:%S}<br>"
                      f"发件账号：{cfg.get('smtp_user', '')}"),
            )
        finally:
            _cfg_cache["data"] = backup
    return send_mail(
        to,
        "【测试】客户管理系统 SMTP 连接测试",
        _wrap("SMTP 连接测试成功", "这是一封测试邮件，收到即表示发件配置可用。"),
    )


# ============================== 邮件模板 ==============================
def _wrap(title: str, body_html: str, footer: str = "") -> str:
    return f"""\
<div style="margin:0;padding:24px;background:#f2f6fc;font-family:-apple-system,'Segoe UI',Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;
              box-shadow:0 6px 24px rgba(16,42,80,.08);">
    <div style="padding:18px 24px;background:linear-gradient(135deg,#006eff,#3fb6ff);color:#fff;">
      <div style="font-size:15px;font-weight:700;">客户管理系统</div>
      <div style="font-size:12px;opacity:.9;margin-top:2px;">{title}</div>
    </div>
    <div style="padding:22px 24px;color:#243b53;font-size:14px;line-height:1.75;">
      {body_html}
    </div>
    <div style="padding:14px 24px;border-top:1px solid #eef3fa;color:#8a9bb0;font-size:12px;line-height:1.7;">
      {footer or "本邮件由系统自动发送，请勿直接回复。"}<br>
      由 PowerShell 中文社区团队开发并提供技术支持 · www.powershell.com.cn
    </div>
  </div>
</div>"""


def send_verify_mail(user, link: str) -> tuple[bool, str]:
    body = (f"<p>{user.display_name}，你好：</p>"
            f"<p>请点击下面的按钮完成邮箱验证，验证后即可使用邮箱找回密码并接收重要通知。</p>"
            f'<p style="margin:22px 0;"><a href="{link}" '
            f'style="display:inline-block;padding:10px 22px;background:#006eff;color:#fff;'
            f'border-radius:8px;text-decoration:none;font-weight:600;">验证邮箱</a></p>'
            f'<p style="font-size:12.5px;color:#6b7c93;">按钮无效时请复制链接到浏览器打开：<br>'
            f'<span style="word-break:break-all;">{link}</span></p>'
            f'<p style="font-size:12.5px;color:#6b7c93;">链接 24 小时内有效，若非本人操作请忽略本邮件。</p>')
    return send_mail(user.email, "请验证你的邮箱 · 客户管理系统", _wrap("邮箱验证", body))


def send_reset_mail(user, link: str) -> tuple[bool, str]:
    body = (f"<p>{user.display_name}，你好：</p>"
            f"<p>我们收到了重置登录密码的请求，点击下面的按钮设置新密码：</p>"
            f'<p style="margin:22px 0;"><a href="{link}" '
            f'style="display:inline-block;padding:10px 22px;background:#006eff;color:#fff;'
            f'border-radius:8px;text-decoration:none;font-weight:600;">重置密码</a></p>'
            f'<p style="font-size:12.5px;color:#6b7c93;">链接 30 分钟内有效且只能使用一次；'
            f'如非本人操作，请忽略本邮件，密码不会改变。</p>')
    return send_mail(user.email, "重置登录密码 · 客户管理系统", _wrap("找回密码", body))


def send_notice(title: str, lines: list[str], to: list[str]) -> None:
    """重要操作通知：后台线程发送，失败只记日志，绝不阻塞请求"""
    if not to:
        return
    body = "".join(f"<p style='margin:0 0 6px;'>{line}</p>" for line in lines)
    html = _wrap(title, body)
    # 子线程没有请求 / 应用上下文，必须先把 app 取出来，在 thread 里重新 push
    app = current_app._get_current_object()

    def worker():
        with app.app_context():
            for addr in to:
                try:
                    send_mail(addr, f"【{title}】客户管理系统", html)
                except Exception:
                    continue

    threading.Thread(target=worker, daemon=True).start()


def admin_recipients() -> list[str]:
    """通知收件人：启用中且填了邮箱的管理员"""
    from models import User
    rows = db.session.scalars(
        db.select(User).where(User.role == "admin", User.status == "active")
    ).all()
    return [u.email.strip() for u in rows if (u.email or "").strip()]


def notify_action(action: str, detail: str, actor: str = "") -> None:
    """按「通知事件」白名单决定是否发通知（由 utils.log_action 调用）"""
    try:
        cfg = load_config()
        if not (cfg.get("email_notify") and is_ready(cfg)):
            return
        events = [e.strip() for e in str(cfg.get("email_notify_events") or "").split(",") if e.strip()]
        if action not in events:
            return
        lines = [
            f"<strong>操作：</strong>{action}",
            f"<strong>内容：</strong>{detail}",
            f"<strong>操作人：</strong>{actor or '系统'}",
            f"<strong>时间：</strong>{datetime.now():%Y-%m-%d %H:%M:%S}",
        ]
        title = {"删除": "重要操作：数据被删除", "对账": "收款对账提醒",
                 "审批": "账号审批提醒"}.get(action, "系统操作通知")
        send_notice(title, lines, admin_recipients())
    except Exception:
        pass                    # 通知是附加能力，任何异常都不影响主流程
