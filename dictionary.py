# -*- coding: utf-8 -*-
"""选项字典与语义映射

系统里所有「预设 + 自定义」下拉的数据源与语义标记都集中在这里，包括：
- 选项读写（option_item 表，后台「选项管理」维护，config 里的列表只做首次种子）
- 选项排序（「其他」恒在最后）、当前记录值的临时并入（仅本次渲染）
- 与显示文案解耦的语义：项目状态的 pending/running/...、付款方式的 transfer 标记、
  客户等级与附件分类的徽标颜色

历史上这些函数都堆在 utils.py 里，与文件、表单、日志、权限工具混在一起。
拆出来的原因很直接：它们只依赖 option_item 表与配置，与"文件怎么存""日志怎么写"无关，
混在一起会让 utils.py 越来越难读，也让改字典逻辑时容易误伤无关代码。
utils.py 保留了同名转出口，既有 `from utils import option_items` 无需改动。
"""
from flask import g

from config import Config
from extensions import db


# ============================== 选项排序 ==============================
def order_with_other_last(options, other: str = "其他") -> list[str]:
    """把「其他」固定到选项列表最底部，其余保持原顺序并去重。

    未包含「其他」时不强行追加，交由调用方决定候选项来源。
    """
    seen, rest = set(), []
    has_other = False
    for item in options or []:
        name = (item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name == other:
            has_other = True
        else:
            rest.append(name)
    if has_other:
        rest.append(other)
    return rest


def merge_local_choice(options, value: str | None, other: str = "其他") -> list[str]:
    """把「当前这条记录 / 当前这次筛选」的值临时并入候选，仅本次渲染有效。

    自定义选项只属于当次操作：不进全局配置、不写回预设列表、也不影响其他记录的下拉。
    这里只在列表页回显筛选条件时使用——否则用户带着 ?type=自定义值 刷新后，
    下拉框会因为没有对应 option 而显示成「全部」。
    """
    items = list(options or [])
    name = (value or "").strip()
    if not name or name in items:
        return order_with_other_last(items, other)
    rest = [i for i in items if i != other]
    rest.append(name)
    if other in items:
        rest.append(other)
    return order_with_other_last(rest, other)


# ============================== 选项字典（后台可维护） ==============================
def _option_cache() -> dict:
    """请求内缓存：同一次请求里多个下拉共用一次查询结果"""
    cache = getattr(g, "_option_cache", None)
    if cache is None:
        cache = {}
        g._option_cache = cache
    return cache


def clear_option_cache() -> None:
    """选项增删改后清掉本次请求内的缓存，保证同一请求后续读取拿到新值"""
    g._option_cache = {}


def option_items(category: str, include_disabled: bool = False) -> list[str]:
    """读取某类选项（已按排序号排列，「其他」恒在最后）。

    数据源是后台维护的 option_item 表；该类别一条记录都没有时（首次运行、
    升级前的老库）才回落到 config 里的预设值，保证任何情况下表单都有可选内容。
    全部停用 ≠ 没有配置，因此这里先取全量再按 enabled 过滤，不会误回落到预设。
    """
    key = (category, include_disabled)
    cache = _option_cache()
    if key in cache:
        return cache[key]

    meta = Config.OPTION_CATEGORIES.get(category, {})
    names: list[str] = []
    try:
        from models import OptionItem
        rows = db.session.scalars(
            db.select(OptionItem)
            .where(OptionItem.category == category)
            .order_by(OptionItem.sort_order, OptionItem.id)
        ).all()
        if rows:
            names = [r.name for r in rows if include_disabled or r.enabled]
        else:
            names = list(meta.get("defaults") or [])
    except Exception:
        db.session.rollback()
        names = list(meta.get("defaults") or [])

    names = order_with_other_last(names)
    cache[key] = names
    return names


def option_default(category: str) -> str:
    """新建表单默认选中项：取显式默认值；默认值被停用/删除时回落第一个候选"""
    meta = Config.OPTION_CATEGORIES.get(category, {})
    options = option_items(category)
    try:
        from models import OptionItem
        row = db.session.scalars(
            db.select(OptionItem).where(
                OptionItem.category == category,
                OptionItem.is_default.is_(True),
                OptionItem.enabled.is_(True),
            )
        ).first()
        if row is not None and row.name in options:
            return row.name
    except Exception:
        db.session.rollback()
    fallback = meta.get("default_value") or ""
    if fallback in options:
        return fallback
    return options[0] if options else ""


def option_usage(category: str, name: str) -> int:
    """统计某个选项值在业务表里的使用条数（用于删除前的拦截提示）

    某些选项会落在多个字段里（如付款方式同时存在于项目约定与收款记录），
    这里逐列累加，避免只统计到一半就允许删除。
    """
    from models import option_columns

    if not name:
        return 0
    total = 0
    try:
        for column in option_columns(category):
            table = column.class_
            stmt = db.select(db.func.count()).select_from(table).where(column == name)
            total += int(db.session.scalar(stmt) or 0)
    except Exception:
        db.session.rollback()
        return 0
    return total


def option_extra(category: str, name: str) -> str:
    """读取选项的语义标记（如付款方式的 transfer），读不到时按预设置兜底"""
    try:
        from models import OptionItem
        row = db.session.scalars(
            db.select(OptionItem).where(
                OptionItem.category == category, OptionItem.name == name
            )
        ).first()
        if row is not None:
            return (row.extra or "").strip()
    except Exception:
        db.session.rollback()
    return ((Config.OPTION_CATEGORIES.get(category, {}).get("extras") or {})
            .get(name, ""))


def transfer_methods() -> list[str]:
    """需要流水号并参与转账对账的付款方式（启用中的）"""
    return [name for name in option_items("payment_method")
            if option_extra("payment_method", name) == "transfer"]


def is_transfer_method(name: str) -> bool:
    """某个付款方式是否需要流水号；停用的历史方式仍按标记判断，不影响历史展示"""
    return option_extra("payment_method", (name or "").strip()) == "transfer"


def option_is_protected(category: str, name: str) -> bool:
    """内置值（如「其他」）受保护：不可删除 / 停用 / 改名"""
    meta = Config.OPTION_CATEGORIES.get(category, {})
    return name in (meta.get("protected") or [])


# ============================== 语义映射（与中文名称解耦） ==============================
STATUS_KIND_COLORS = {
    "pending": "gray",
    "running": "orange",
    "success": "green",
    "failed": "red",
    "other": "gray",
}


def status_kind(name: str) -> str:
    """项目状态的语义：pending / running / success / failed / other"""
    kind = (option_extra("project_status", (name or "").strip()) or "").strip()
    return kind if kind in STATUS_KIND_COLORS else "other"


def status_color(name: str) -> str:
    """项目状态徽标颜色，按语义取色"""
    return STATUS_KIND_COLORS.get(status_kind(name), "gray")


def status_names_by_kind(kind: str) -> list[str]:
    """某语义下的全部状态名（可能因自定义出现多个）"""
    return [name for name in option_items("project_status") if status_kind(name) == kind]


def level_color(name: str) -> str:
    """客户等级徽标颜色（选项 extra 里维护）"""
    color = (option_extra("customer_level", (name or "").strip()) or "").strip()
    return color if color in ("red", "gray", "orange", "blue", "green", "cyan") else "gray"
