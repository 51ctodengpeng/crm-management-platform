# -*- coding: utf-8 -*-
"""路由层：管理后台 · 选项管理（下拉字典的增删改排与启停）

设计约定：
- 只有管理员能进入（admin_required），普通成员的表单只读取「启用中」的选项；
- 选项按 category 分组存在 option_item 表，config.OPTION_CATEGORIES 只提供
  展示名、预设值与受保护值，不再作为运行时数据源；
- 「其他」等 builtin 值不可删除 / 停用 / 改名，且恒定排在最底部；
- 删除前先统计引用条数：仍被业务数据使用时拒绝删除，引导改为「停用」，
  避免历史记录变成孤儿值；
- 改名时可选同步历史数据（默认同步），不同步则旧记录保留原值，语义不变；
- 所有写操作都写操作日志，便于追溯谁改了字典。
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from config import Config
from extensions import db
from models import OptionItem, option_columns
from utils import (
    admin_required,
    clear_option_cache,
    log_action,
    option_is_protected,
    option_usage,
)

bp = Blueprint("option", __name__)

NAME_MAX = 32
REMARK_MAX = 120

# 受保护值 / 内置值不允许改名，名称校验也复用同一套规则
RESERVED_NAMES = {"请选择", "自定义", "自定义…"}


def _categories() -> list[dict]:
    """给模板用的类别清单：按 config 里的声明顺序，附带选项与使用量"""
    groups: list[dict] = []
    for key, meta in Config.OPTION_CATEGORIES.items():
        items = db.session.scalars(
            db.select(OptionItem)
            .where(OptionItem.category == key)
            .order_by(OptionItem.sort_order, OptionItem.id)
        ).all()
        rows = [
            {"item": it, "usage": option_usage(key, it.name)}
            for it in items
        ]
        groups.append({
            "key": key,
            "label": meta["label"],
            "extra_kind": meta.get("extra_kind", ""),
            "group": meta.get("group", ""),
            "desc": meta.get("desc", ""),
            "protected": meta.get("protected", []),
            "rows": rows,
            "enabled_count": sum(1 for r in rows if r["item"].enabled),
            "total": len(rows),
        })
    return groups


def _render(status: int = 200):
    return render_template("option/index.html", categories=_categories()), status


def _next_sort(category: str) -> int:
    last = db.session.scalar(
        db.select(db.func.max(OptionItem.sort_order)).where(OptionItem.category == category)
    )
    return int(last or 0) + 10


def _validate_name(category: str, name: str, exclude_id: int | None = None):
    """返回 (是否合法, 提示)"""
    if not name:
        return False, "选项名称不能为空"
    if len(name) > NAME_MAX:
        return False, f"选项名称不能超过 {NAME_MAX} 个字符"
    if name in RESERVED_NAMES:
        return False, "「请选择」「自定义」为系统保留文案，请换一个名称"
    exists = db.session.scalars(
        db.select(OptionItem).where(
            OptionItem.category == category, OptionItem.name == name
        )
    ).first()
    if exists is not None and exists.id != exclude_id:
        return False, f"该类别下已存在选项「{name}」"
    return True, ""


STATUS_KINDS = ("pending", "running", "success", "failed", "other")
LEVEL_COLORS = ("red", "gray", "orange", "blue", "green", "cyan")


def _extra_from_form(category: str, current: str = "") -> str:
    """解析选项的语义标记（extra）

    - payment_method：transfer 表示需流水号并参与转账对账
    - project_status：pending/running/success/failed/other，用于概览统计与配色
    - customer_level：badge 颜色，改名后仍保持视觉层级
    """
    if category == "payment_method":
        return "transfer" if request.form.get("is_transfer") == "1" else ""
    if category == "project_status":
        kind = (request.form.get("status_kind") or "").strip()
        return kind if kind in STATUS_KINDS else current
    if category == "customer_level":
        color = (request.form.get("level_color") or "").strip()
        return color if color in LEVEL_COLORS else current
    return current


def _clear_default(category: str, keep_id: int | None = None) -> None:
    """保证同一类别只有一个默认值"""
    rows = db.session.scalars(
        db.select(OptionItem).where(
            OptionItem.category == category, OptionItem.is_default.is_(True)
        )
    ).all()
    for row in rows:
        if row.id != keep_id:
            row.is_default = False


@bp.route("/")
@admin_required
def index():
    return _render()


@bp.route("/add", methods=["POST"])
@admin_required
def add():
    category = (request.form.get("category") or "").strip()
    if category not in Config.OPTION_CATEGORIES:
        flash("未知的选项类别", "danger")
        return redirect(url_for("option.index"))

    name = (request.form.get("name") or "").strip()[:NAME_MAX]
    remark = (request.form.get("remark") or "").strip()[:REMARK_MAX]
    enabled = request.form.get("enabled", "1") == "1"
    is_default = request.form.get("is_default") == "1"

    ok, msg = _validate_name(category, name)
    if not ok:
        flash(msg, "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")

    item = OptionItem(
        category=category, name=name, remark=remark,
        sort_order=_next_sort(category), enabled=enabled,
        is_default=is_default, builtin=False,
        extra=_extra_from_form(category),
    )
    db.session.add(item)
    db.session.flush()
    if is_default:
        _clear_default(category, keep_id=item.id)
    db.session.commit()
    clear_option_cache()
    log_action("新增", "选项", item.id,
               f"新增{Config.OPTION_CATEGORIES[category]['label']}：{name}")
    flash(f"已新增「{name}」，前台表单即时生效", "success")
    return redirect(url_for("option.index") + f"#cat-{category}")


@bp.route("/<int:item_id>/edit", methods=["POST"])
@admin_required
def edit(item_id: int):
    item = db.get_or_404(OptionItem, item_id)
    category = item.category
    label = Config.OPTION_CATEGORIES.get(category, {}).get("label", category)

    new_name = (request.form.get("name") or "").strip()[:NAME_MAX]
    remark = (request.form.get("remark") or "").strip()[:REMARK_MAX]
    # 编辑表单不提交 enabled 时保持原状态，启停统一走 toggle 入口，避免改名把停用项改活
    enabled_raw = request.form.get("enabled")
    enabled = item.enabled if enabled_raw is None else enabled_raw == "1"
    is_default = request.form.get("is_default", "0") == "1"
    cascade = request.form.get("cascade", "1") == "1"

    if item.builtin and new_name != item.name:
        flash(f"「{item.name}」是内置选项，名称不可修改", "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")
    if item.builtin and not enabled:
        flash(f"「{item.name}」是兜底选项，不能停用", "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")

    ok, msg = _validate_name(category, new_name, exclude_id=item.id)
    if not ok:
        flash(msg, "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")

    old_name = item.name
    affected = 0
    if new_name != old_name and cascade:
        for column in option_columns(category):
            table = column.class_
            affected += int(db.session.execute(
                db.update(table).where(column == old_name).values({column: new_name})
            ).rowcount or 0)

    item.name = new_name
    item.remark = remark
    item.enabled = enabled
    item.extra = _extra_from_form(category, item.extra or "")
    if is_default and enabled:
        item.is_default = True
        _clear_default(category, keep_id=item.id)
    elif not is_default:
        item.is_default = False
    db.session.commit()
    clear_option_cache()

    detail = f"修改{label}：{old_name}"
    if new_name != old_name:
        detail += f" → {new_name}"
        if affected:
            detail += f"（同步更新 {affected} 条历史记录）"
    if not enabled:
        detail += "（已停用）"
    log_action("修改", "选项", item.id, detail)

    if new_name != old_name and not cascade and option_usage(category, old_name):
        flash(
            f"已改名，但历史记录仍保留旧值「{old_name}」；如需统一，请再次编辑并勾选「同步更新历史记录」",
            "warning",
        )
    else:
        flash("选项已更新" + (f"，同步更新 {affected} 条历史记录" if affected else ""), "success")
    return redirect(url_for("option.index") + f"#cat-{category}")


@bp.route("/<int:item_id>/toggle", methods=["POST"])
@admin_required
def toggle(item_id: int):
    item = db.get_or_404(OptionItem, item_id)
    category = item.category
    if item.builtin and item.enabled:
        flash(f"「{item.name}」是兜底选项，不能停用", "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")

    item.enabled = not item.enabled
    if not item.enabled:
        item.is_default = False
    db.session.commit()
    clear_option_cache()
    log_action("修改", "选项", item.id,
               f"{'启用' if item.enabled else '停用'}{Config.OPTION_CATEGORIES[category]['label']}：{item.name}")
    flash(
        f"已{'启用' if item.enabled else '停用'}「{item.name}」"
        + ("" if item.enabled else "；前台表单不再显示该选项，历史记录不受影响"),
        "success",
    )
    return redirect(url_for("option.index") + f"#cat-{category}")


@bp.route("/<int:item_id>/delete", methods=["POST"])
@admin_required
def delete(item_id: int):
    item = db.get_or_404(OptionItem, item_id)
    category = item.category
    label = Config.OPTION_CATEGORIES.get(category, {}).get("label", category)

    if item.protected or option_is_protected(category, item.name):
        flash(f"「{item.name}」是系统兜底选项，不能删除", "danger")
        return redirect(url_for("option.index") + f"#cat-{category}")

    used = option_usage(category, item.name)
    if used:
        flash(
            f"「{item.name}」仍被 {used} 条{label}数据使用，已阻止删除；"
            f"可改为「停用」，停用后新表单不再出现，历史记录保持不变",
            "warning",
        )
        return redirect(url_for("option.index") + f"#cat-{category}")

    name = item.name
    db.session.delete(item)
    db.session.commit()
    clear_option_cache()
    log_action("删除", "选项", item_id, f"删除{label}：{name}")
    flash(f"已删除「{name}」", "success")
    return redirect(url_for("option.index") + f"#cat-{category}")


@bp.route("/<int:item_id>/move", methods=["POST"])
@admin_required
def move(item_id: int):
    """上移 / 下移一位：只交换排序号，不影响其他类别"""
    item = db.get_or_404(OptionItem, item_id)
    category = item.category
    direction = (request.form.get("direction") or "up").strip()
    if direction not in ("up", "down"):
        direction = "up"

    siblings = db.session.scalars(
        db.select(OptionItem)
        .where(OptionItem.category == category)
        .order_by(OptionItem.sort_order, OptionItem.id)
    ).all()
    # 「其他」恒在最后：内置值不参与移动
    movable = [it for it in siblings if not it.builtin]
    index = next((i for i, it in enumerate(movable) if it.id == item.id), -1)
    if item.builtin or index < 0:
        flash("内置兜底选项固定在最底部，不能调整顺序", "warning")
        return redirect(url_for("option.index") + f"#cat-{category}")

    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(movable):
        return redirect(url_for("option.index") + f"#cat-{category}")

    for pos, it in enumerate(movable):
        it.sort_order = (pos + 1) * 10
    movable[index].sort_order, movable[target].sort_order = \
        movable[target].sort_order, movable[index].sort_order

    # 内置项永远排在最后
    tail = (len(movable) + 1) * 10
    for it in siblings:
        if it.builtin:
            it.sort_order = tail

    db.session.commit()
    clear_option_cache()
    log_action("修改", "选项", item.id,
               f"调整{Config.OPTION_CATEGORIES[category]['label']}顺序：{item.name}")
    return redirect(url_for("option.index") + f"#cat-{category}")
