# -*- coding: utf-8 -*-
"""业务工具：文件校验与保存、文件名缩略、日志记录、权限装饰器、表单解析"""
import math
import re
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import current_app, flash, g, has_request_context, redirect, request, url_for
from flask_login import current_user

from extensions import db
from models import OperationLog

# 选项字典 / 语义映射已拆分到 dictionary.py；这里保留同名转出口，
# 既有的 `from utils import option_items` 等写法无需改动，行为完全一致。
from dictionary import (  # noqa: E402,F401
    STATUS_KIND_COLORS,
    clear_option_cache,
    is_transfer_method,
    level_color,
    merge_local_choice,
    option_default,
    option_extra,
    option_is_protected,
    option_items,
    option_usage,
    order_with_other_last,
    status_color,
    status_kind,
    status_names_by_kind,
    transfer_methods,
)

# 单笔金额上限（1 万亿元）。仅用于拦截 1e308 这类明显异常的输入，
# 正常业务金额远低于该量级，不影响任何合法数据的解析结果。
AMOUNT_LIMIT = 1e12


# ============================== 文件相关 ==============================
def allowed_file(filename: str) -> tuple[bool, str]:
    """严格校验后缀名：先查黑名单，再查白名单"""
    if not filename or "." not in filename:
        return False, "文件缺少扩展名"
    ext = filename.rsplit(".", 1)[1].lower()
    if ext in current_app.config["DANGEROUS_EXTENSIONS"]:
        return False, f"禁止上传 .{ext} 类型的可执行/脚本文件"
    if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
        return False, f"不支持的文件类型 .{ext}"
    return True, ext


def save_upload(file_storage) -> tuple[dict | None, str]:
    """保存上传文件：生成 UUID 文件名防止同名覆盖，返回 (元信息, 错误信息)"""
    original_name = file_storage.filename or ""
    original_name = original_name.replace("\\", "/").split("/")[-1]
    ok, result = allowed_file(original_name)
    if not ok:
        return None, f"{original_name}：{result}"

    ext = result
    store_name = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    abs_path = upload_dir / store_name
    file_storage.save(abs_path)

    size = abs_path.stat().st_size
    # 原始文件名仅用于展示，物理路径一律使用 UUID，杜绝路径穿越
    return {
        "file_original_name": original_name,
        "file_store_name": store_name,
        "file_path": str(abs_path),
        "file_ext": ext,
        "file_size": size,
        "uploader": (current_user.username if current_user.is_authenticated else "匿名"),
        "upload_time": datetime.now(),
    }, ""


def remove_file_on_disk(file_path: str) -> bool:
    """同步删除服务器磁盘文件，容忍文件已被手工删除的情况"""
    try:
        p = Path(file_path)
        if p.exists() and p.is_file():
            p.unlink()
            return True
    except OSError as exc:  # pragma: no cover
        current_app.logger.warning("删除磁盘文件失败 %s: %s", file_path, exc)
    return False


def resolve_abs_path(file_path: str) -> Path | None:
    """把数据库中的路径解析为 uploads 目录内的真实路径，越界一律拒绝"""
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    try:
        target = Path(file_path).resolve()
    except (OSError, ValueError):
        return None
    if upload_dir != target and upload_dir not in target.parents:
        return None
    return target


# ============================== 头像 ==============================
def save_avatar(file_storage) -> tuple[str | None, str]:
    """保存头像：仅限图片、≤ 2MB、UUID 物理名。返回 (文件名, 错误信息)"""
    original = (file_storage.filename or "").replace("\\", "/").split("/")[-1]
    if "." not in original:
        return None, "头像文件缺少扩展名"
    ext = original.rsplit(".", 1)[1].lower()
    allowed = current_app.config["ALLOWED_AVATAR_EXTENSIONS"]
    if ext not in allowed:
        return None, "头像仅支持 " + " / ".join(sorted(allowed)) + " 格式"

    store_name = f"{uuid.uuid4().hex}.{ext}"
    folder = Path(current_app.config["AVATAR_FOLDER"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / store_name
    file_storage.save(target)

    if target.stat().st_size > current_app.config["MAX_AVATAR_SIZE"]:
        target.unlink(missing_ok=True)
        limit = current_app.config["MAX_AVATAR_SIZE"] // 1024 // 1024
        return None, f"头像文件不能超过 {limit}MB"
    return store_name, ""


def remove_avatar(filename: str):
    """删除头像物理文件（只允许 avatars 目录内的文件名）"""
    if not filename:
        return
    folder = Path(current_app.config["AVATAR_FOLDER"]).resolve()
    try:
        target = (folder / Path(filename).name).resolve()
    except (OSError, ValueError):
        return
    if target.parent == folder and target.is_file():
        try:
            target.unlink()
        except OSError as exc:  # pragma: no cover
            current_app.logger.warning("删除头像失败 %s: %s", filename, exc)


def avatar_path(filename: str) -> Path | None:
    """解析头像物理路径，越界返回 None"""
    if not filename:
        return None
    folder = Path(current_app.config["AVATAR_FOLDER"]).resolve()
    try:
        target = (folder / Path(filename).name).resolve()
    except (OSError, ValueError):
        return None
    return target if target.parent == folder else None


# ============================== 文件名缩略 ==============================
def _visual_width(text: str) -> int:
    """按显示宽度计长：中日韩字符算 2，其余算 1"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def shorten_filename(name: str, width: int = 34) -> str:
    """长文件名智能缩略：保留文件名开头与扩展名，中间用省略号替代。

    例：'2026年度AD域控维保服务合同及技术附件清单最终确认版.pdf'
        -> '2026年度AD域控维保…….pdf'
    短文件名原样返回，不改变任何字符。
    """
    name = (name or "").strip()
    if not name or _visual_width(name) <= width:
        return name

    stem, dot, ext = name.rpartition(".")
    if not dot:                      # 没有扩展名
        return _cut(name, width) + "…"
    if not stem:                     # 形如 ".gitignore"
        return _cut(name, width) + "…"

    ext = ext.strip()
    keep = width - _visual_width(ext) - 2
    if keep < 6:                     # 扩展名过长时优先保扩展名
        keep = 6
    return f"{_cut(stem, keep)}….{ext}"


def _cut(text: str, max_width: int) -> str:
    """按显示宽度截断（不切断代理对）"""
    out, used = [], 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if used + w > max_width:
            break
        out.append(ch)
        used += w
    return "".join(out)


# ============================== 表单解析 ==============================
def resolve_custom_choice(form, field: str, custom_field: str | None = None,
                          fallback: str = "") -> str:
    """解析「下拉预设 + 自定义」字段。

    - 选中哨兵值（__custom__）时取自定义输入框的值；
    - 下拉为空但填了自定义值时，同样取自定义值（兼容未执行脚本的表单）；
    - 自定义为空时回落到 fallback（通常是「其他」）。
    """
    flag = current_app.config.get("CUSTOM_CHANNEL_FLAG", "__custom__")
    value = (form.get(field) or "").strip()
    custom = (form.get(custom_field or f"{field}_custom") or "").strip()
    if value == flag or (not value and custom):
        value = custom or fallback
    return value[:32]


def resolve_source_channel(form) -> str:
    """解析来源渠道：下拉预设值 或 用户自定义填写（前端哨兵值 __custom__）"""
    return resolve_custom_choice(form, "source_channel", "source_channel_custom")


# ============================== 表单候选 ==============================
def owner_options() -> list[str]:
    """负责人候选：启用中的账号显示名（用于客户/项目归属与业绩统计）"""
    from models import User

    names: list[str] = []
    for u in db.session.scalars(
        db.select(User).where(User.status == "active").order_by(User.username)
    ).all():
        name = (u.display_name or u.username or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def clean_owner(value: str) -> str:
    """负责人只做长度与空白清理；允许留空表示未分配"""
    return (value or "").strip()[:64]


def month_series(months: int = 6) -> list[str]:
    """最近 N 个月的 YYYY-MM 列表（含当月，升序）"""
    today = date.today()
    result = []
    year, month = today.year, today.month
    for _ in range(months):
        result.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(result))


def todo_summary(user=None) -> dict:
    """汇总当前账号的待办（请求内缓存，供顶栏铃铛 / 概览 / 待办页共用）

    口径：
      - 跟进到期：客户的下次跟进日期 <= 今天
      - 项目到期：结束日期在 7 天内，且状态不是已完成 / 已终止
      - 应收逾期：项目结束日期 + 账期 < 今天，且仍有未收金额
      - 待对账：已登记但未对账的收款笔数
    统计受数据范围约束，普通成员只会看到自己有权查看的数据。
    """
    from sqlalchemy import func

    from models import Customer, Payment, Project
    from permissions import PERM_VIEW, apply_customer_scope, apply_project_scope

    u = user if user is not None else current_user
    cache_key = f"_todo_summary_{getattr(u, 'id', 0)}"
    cached = g.get(cache_key)
    if cached is not None:
        return cached

    today = date.today()
    soon = today + timedelta(days=7)

    follow_rows = db.session.scalars(
        apply_customer_scope(
            db.select(Customer).where(
                Customer.next_follow_at.isnot(None),
                Customer.next_follow_at <= today,
            )
        ).order_by(Customer.next_follow_at, Customer.id)
    ).all()

    closed = status_names_by_kind("success") + status_names_by_kind("failed")
    proj_stmt = db.select(Project).where(
        Project.end_date.isnot(None), Project.end_date <= soon
    )
    if closed:
        proj_stmt = proj_stmt.where(~Project.project_status.in_(closed))
    project_rows = db.session.scalars(
        apply_project_scope(proj_stmt, PERM_VIEW).order_by(Project.end_date, Project.id)
    ).all()

    ar_rows = []
    open_projects = db.session.scalars(
        apply_project_scope(
            db.select(Project).where(Project.end_date.isnot(None)), PERM_VIEW
        )
    ).all()
    for p in open_projects:
        if (p.receivable or 0) - (p.matched_amount or 0) <= 0.005:
            continue
        credit = p.credit_days if p.credit_days is not None else 30
        due = p.end_date + timedelta(days=max(0, int(credit)))
        if due < today:
            ar_rows.append((p, due, (today - due).days))
    ar_rows.sort(key=lambda item: item[1])

    pending_pay = db.session.scalar(
        apply_project_scope(
            db.select(func.count(Payment.id)).join(
                Project, Project.id == Payment.project_id
            ).where(Payment.status == "pending"),
            PERM_VIEW,
        )
    ) or 0

    data = {
        "follow_rows": follow_rows,
        "follow_count": len(follow_rows),
        "project_rows": project_rows,
        "project_count": len(project_rows),
        "ar_rows": ar_rows,
        "ar_count": len(ar_rows),
        "ar_amount": round(sum((p.receivable or 0) - (p.matched_amount or 0) for p, _, _ in ar_rows), 2),
        "pending_pay": int(pending_pay),
        "total": len(follow_rows) + len(project_rows) + len(ar_rows),
        "today": today,
    }
    setattr(g, cache_key, data)
    return data


def parse_date(value: str):
    """解析 yyyy-MM-dd，空值返回 None"""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value: str) -> float:
    """解析金额文本：容忍千分位与货币符号，非法值一律按 0 处理。

    `float()` 本身接受 `inf` / `nan` / `1e400` 这类非有限值，直接入库会让
    合计变成 inf、让 `金额 > 应收` 这种比较恒为 False（nan 参与比较永远为假），
    从而绕过超收校验。因此这里先做有限性判断，再把绝对值夹到业务上限，
    保证进入数据库的一律是可比较、可求和的有限数。
    """
    raw = (value or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    if not raw:
        return 0.0
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(-AMOUNT_LIMIT, min(AMOUNT_LIMIT, number)), 2)


# ============================== 密码强度 ==============================
def password_problem(password: str) -> str | None:
    """校验新密码强度，返回问题描述；None 表示通过

    规则：长度 ≥ PASSWORD_MIN_LENGTH（默认 8），且同时包含字母与数字。
    不强制特殊字符——内部系统更看重可记忆性，避免用户写在便签上。
    """
    from config import Config
    pwd = password or ""
    min_len = Config.PASSWORD_MIN_LENGTH
    if len(pwd) < min_len:
        return f"密码长度至少 {min_len} 位"
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"\d", pwd):
        return "密码需同时包含字母和数字"
    if pwd.lower() in {"12345678", "123456789", "password", "qwerty123", "abcd1234",
                       "11111111", "abc12345"}:
        return "该密码过于常见，请换一个更安全的组合"
    return None


# ============================== 日志与权限 ==============================
def current_username() -> str:
    """当前操作人账号；未登录或非请求上下文时返回「系统」"""
    try:
        if current_user and current_user.is_authenticated:
            return current_user.username
    except Exception:
        pass
    return "系统"


def log_action(action: str, target_type: str, target_id: int | None = None, detail: str = "",
               username: str | None = None):
    """写入操作日志

    除动作与对象外，同时记录本次请求的来源，便于事后识别「谁、用什么设备」发起的操作：
    - client_type ← User-Agent 归一化后的「大类 · 客户端 · 系统」
    来源取自请求本身，取不到时留空，日志照常写入。
    """
    try:
        if username is None:
            username = current_user.username if current_user.is_authenticated else "匿名"
        ip = client_type = ""
        if has_request_context():
            from clientinfo import client_type_of
            ip = request.remote_addr or ""
            client_type = client_type_of(request)
        entry = OperationLog(
            username=username,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip=ip,
            client_type=client_type,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:  # pragma: no cover - 日志失败不影响主流程
        db.session.rollback()
        current_app.logger.warning("写日志失败: %s", exc)
        return

    # 重要操作邮件通知：白名单 + 开关在 mailer 内部判断，发送走后台线程，失败不影响主流程
    try:
        import mailer
        mailer.notify_action(action, detail or f"{target_type}#{target_id}", username)
    except Exception:
        pass


def admin_required(func):
    """仅管理员可执行的视图装饰器（已内含登录校验）"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.full_path))
        if not getattr(current_user, "is_admin", False):
            flash("该功能仅管理员可操作", "danger")
            return redirect(url_for("index"))
        return func(*args, **kwargs)
    return wrapper


def current_per_page(default: int = 20) -> int:
    """读取每页条数：URL 参数 > 系统设置里的默认值 > 兜底 20，只允许 10/20/50/100"""
    options = (10, 20, 50, 100)
    if "per_page" not in request.args:
        try:
            from models import Setting
            default = int(Setting.get("default_per_page", str(default)))
        except Exception:
            pass
    try:
        value = int(request.args.get("per_page", default))
    except (TypeError, ValueError):
        value = default
    return value if value in options else (default if default in options else 20)


def site_setting(key: str, default: str = "") -> str:
    """读取系统设置（异常时回落默认值，避免设置表未就绪时炸页面）"""
    try:
        from models import Setting
        return Setting.get(key, default)
    except Exception:
        return default
