# -*- coding: utf-8 -*-
"""操作日志的客户归属地：把「客户属于哪个省 / 市」带到日志里展示

设计取舍：
- 日志表只存 target_type + target_id，不冗余存一份客户地区。地区属于客户主档，
  客户改了地址后历史行同步反映，不会出现「日志里是旧地址、客户页是新地址」两套口径；
- 因此这里只在渲染日志列表时**一次性批量关联**：整页至多 5 条查询，不做逐行查询，
  也不写回日志表（旧日志不需要回填）；
- 关联不到客户时（对象已删除 / 该类型不涉及客户）返回空，由页面统一显示「—」。

对象类型到客户的映射：
    客户 → 本身
    项目 → project.customer_id
    附件 → attachment.project_id → project.customer_id
    收款 → payment.project_id → project.customer_id
其余类型（账号 / 系统 / 选项 / 系统设置 …）不涉及客户，展示为「—」。
"""
from __future__ import annotations

from extensions import db
from models import Attachment, Customer, Payment, Project

# 能直接或间接归属到客户的对象类型
CUSTOMER_TARGET = "客户"
PROJECT_TARGET = "项目"
# 挂靠在项目下、可经 project_id 反查客户的对象类型
PROJECT_CHILD_TARGETS = ("附件", "收款")


def format_region(province, city) -> str:
    """省 + 市 拼成「广东省深圳市」（紧凑写法，中间不加空格/斜杠）

    规则：
    - 省、市都有 → 直接相连，如「广东省深圳市」；
    - 只有省或只有市 → 显示现有的那一段，不补占位、不猜缺的那一级；
    - 一段已包含另一段（「北京市」+「北京市」、「上海」+「上海市」）→ 只保留较长的，
      避免拼出「北京市北京市」这类重复；
    - 两段都为空 → 返回空串，默认文案（「—」）由调用方决定。
    """
    prov = (province or "").strip()
    city = (city or "").strip()
    if not prov:
        return city
    if not city:
        return prov
    if city.startswith(prov) or prov.startswith(city):
        return city if len(city) >= len(prov) else prov
    return f"{prov}{city}"


def _group_targets(logs) -> dict[str, set[int]]:
    """按对象类型聚合本页日志的对象 ID"""
    grouped: dict[str, set[int]] = {}
    for log in logs:
        target_id = getattr(log, "target_id", None)
        if not target_id:
            continue
        target_type = (getattr(log, "target_type", "") or "").strip()
        grouped.setdefault(target_type, set()).add(int(target_id))
    return grouped


def _load_regions(customer_ids: set[int]) -> dict[int, str]:
    """批量取客户省 / 市，返回 {客户 id: 展示文本}；未填写省市的客户不出现在结果里"""
    if not customer_ids:
        return {}
    rows = db.session.execute(
        db.select(Customer.id, Customer.province, Customer.city)
        .where(Customer.id.in_(customer_ids))
    ).all()
    regions: dict[int, str] = {}
    for cid, province, city in rows:
        text = format_region(province, city)
        if text:
            regions[cid] = text
    return regions


def resolve_log_regions(logs) -> dict[int, str]:
    """返回 {日志 id: 省市文本}，供日志列表按行渲染

    解析不到的日志（对象已删除、客户未填省市、类型不涉及客户）不会出现在字典里，
    模板侧统一回落「—」，因此调用方不需要区分这些情况。
    """
    logs = [log for log in (logs or []) if getattr(log, "id", None)]
    if not logs:
        return {}

    grouped = _group_targets(logs)
    project_ids: set[int] = set(grouped.get(PROJECT_TARGET, ()))
    customer_ids: set[int] = set(grouped.get(CUSTOMER_TARGET, ()))
    # (对象类型, 对象 id) → 项目 id，用于附件 / 收款反查所属项目
    child_project: dict[tuple[str, int], int] = {}

    for label, model in zip(PROJECT_CHILD_TARGETS, (Attachment, Payment)):
        ids = grouped.get(label)
        if not ids:
            continue
        for row_id, project_id in db.session.execute(
            db.select(model.id, model.project_id).where(model.id.in_(ids))
        ).all():
            if project_id:
                child_project[(label, row_id)] = int(project_id)
                project_ids.add(int(project_id))

    project_customer: dict[int, int] = {}
    if project_ids:
        project_customer = {
            pid: cid
            for pid, cid in db.session.execute(
                db.select(Project.id, Project.customer_id).where(Project.id.in_(project_ids))
            ).all()
            if cid
        }
        customer_ids |= set(project_customer.values())

    regions = _load_regions(customer_ids)

    result: dict[int, str] = {}
    for log in logs:
        target_type = (getattr(log, "target_type", "") or "").strip()
        target_id = getattr(log, "target_id", None)
        if not target_id:
            continue
        if target_type == CUSTOMER_TARGET:
            customer_id = int(target_id)
        elif target_type == PROJECT_TARGET:
            customer_id = project_customer.get(int(target_id))
        elif target_type in PROJECT_CHILD_TARGETS:
            project_id = child_project.get((target_type, int(target_id)))
            customer_id = project_customer.get(project_id) if project_id else None
        else:
            continue
        text = regions.get(customer_id) if customer_id else ""
        if text:
            result[log.id] = text
    return result
