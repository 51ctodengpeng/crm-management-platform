# -*- coding: utf-8 -*-
"""回收站：删除客户 / 项目时留存完整快照，支持原样恢复与彻底删除

为什么不做 deleted_at 软删除：
客户 / 项目的查询散落在列表、看板、导出、下拉、待办、统计等几十处，
软删除要求每处都显式过滤 `deleted_at IS NULL`，漏一处就会把已删数据算进
金额与排行，属于「静默错误」。快照方案让业务表保持干净，
恢复时按快照重建，附件物理文件（UUID 命名）留在 uploads 里，恢复后自动关联。
"""
import json
from datetime import date, datetime

from sqlalchemy import inspect as sa_inspect

from extensions import db
from models import (
    Attachment,
    Customer,
    FollowUp,
    Payment,
    PaymentPlan,
    Project,
    RecycleBin,
    refresh_customer_follow,
)
from utils import current_username, remove_file_on_disk


# ============================== 序列化 ==============================
def _dump(obj, keep_old_id: bool = False) -> dict:
    """把一个 ORM 对象转成可 JSON 化的字典（去掉主键，日期转 ISO 字符串）"""
    data = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        value = getattr(obj, attr.key)
        if isinstance(value, (datetime, date)):
            data[attr.key] = value.isoformat()
        else:
            data[attr.key] = value
    data.pop("id", None)
    if keep_old_id:
        data["_old_id"] = obj.id
    return data


def _load(model, data: dict, **overrides):
    """按字典重建 ORM 对象；列类型不匹配的脏数据直接丢弃，避免恢复整体失败"""
    columns = {attr.key: attr for attr in sa_inspect(model).mapper.column_attrs}
    kwargs = {}
    for key, value in data.items():
        if key in ("id", "_old_id") or key not in columns:
            continue
        if isinstance(value, str):
            try:
                py_type = columns[key].columns[0].type.python_type
            except (NotImplementedError, AttributeError):
                py_type = None
            if py_type in (datetime, date):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    value = None
        kwargs[key] = value
    kwargs.update(overrides)
    return model(**kwargs)


def _attachment_files(items) -> list[str]:
    return [a.get("file_path") for a in items if a.get("file_path")]


# ============================== 入站（删除时） ==============================
def snapshot_customer(customer: Customer) -> dict:
    payload = {
        "customer": _dump(customer),
        "followups": [_dump(f) for f in customer.follow_ups],
        "projects": [],
    }
    # 一条跟进可能同时挂客户与项目：客户级已收进 followups，
    # 项目块里只放「没在客户列表出现过」的，避免恢复后重复
    seen = {f.id for f in customer.follow_ups}
    for project in customer.projects:
        payload["projects"].append({
            "project": _dump(project),
            "attachments": [_dump(a) for a in project.attachments],
            "payments": [_dump(p) for p in project.payments],
            "plans": [_dump(pl, keep_old_id=True) for pl in project.payment_plans],
            "followups": [_dump(f) for f in project.follow_ups if f.id not in seen],
        })
    return payload


def snapshot_project(project: Project) -> dict:
    return {
        "project": _dump(project),
        "attachments": [_dump(a) for a in project.attachments],
        "payments": [_dump(p) for p in project.payments],
        "plans": [_dump(pl, keep_old_id=True) for pl in project.payment_plans],
        "followups": [_dump(f) for f in project.follow_ups],
    }


def _file_stats(payload: dict) -> tuple[list[str], int]:
    files, size = [], 0
    projects = payload.get("projects") or [payload]
    for item in projects:
        for att in item.get("attachments", []):
            files.append(att.get("file_path"))
            size += att.get("file_size") or 0
    return [f for f in files if f], size


def send_customer_to_bin(customer: Customer) -> RecycleBin:
    """把客户（含项目、附件、收款、计划、跟进）整份放入回收站"""
    payload = snapshot_customer(customer)
    files, size = _file_stats(payload)
    row = RecycleBin(
        kind="customer",
        title=customer.company_name,
        origin_id=customer.id,
        payload=json.dumps(payload, ensure_ascii=False),
        file_paths=json.dumps(files, ensure_ascii=False),
        file_count=len(files),
        file_size=size,
        deleted_by=current_username(),
    )
    db.session.add(row)
    return row


def send_project_to_bin(project: Project) -> RecycleBin:
    payload = snapshot_project(project)
    files, size = _file_stats({"project": payload["project"],
                               "attachments": payload["attachments"]})
    row = RecycleBin(
        kind="project",
        title=project.project_name,
        origin_id=project.id,
        payload=json.dumps(payload, ensure_ascii=False),
        file_paths=json.dumps(files, ensure_ascii=False),
        file_count=len(files),
        file_size=size,
        deleted_by=current_username(),
    )
    db.session.add(row)
    return row


# ============================== 恢复 ==============================
def _restore_project_block(block: dict, customer_id: int) -> Project:
    project = _load(Project, block["project"], customer_id=customer_id)
    db.session.add(project)
    db.session.flush()

    plan_map = {}
    for plan in block.get("plans", []):
        row = _load(PaymentPlan, plan, project_id=project.id)
        db.session.add(row)
        db.session.flush()
        plan_map[plan.get("_old_id")] = row.id

    for payment in block.get("payments", []):
        fields = dict(payment)
        old_plan = fields.get("plan_id")
        row = _load(Payment, fields, project_id=project.id)
        row.plan_id = plan_map.get(old_plan)
        db.session.add(row)

    for follow in block.get("followups", []):
        row = _load(FollowUp, follow, project_id=project.id)
        db.session.add(row)

    for att in block.get("attachments", []):
        db.session.add(_load(Attachment, att, project_id=project.id))
    return project


def restore(row: RecycleBin) -> str:
    """按快照恢复，返回给用户的提示文案"""
    payload = json.loads(row.payload or "{}")
    if row.kind == "customer":
        customer = _load(Customer, payload.get("customer") or {})
        db.session.add(customer)
        db.session.flush()
        for follow in payload.get("followups", []):
            db.session.add(_load(FollowUp, follow, customer_id=customer.id))
        for block in payload.get("projects", []):
            _restore_project_block(block, customer.id)
        db.session.flush()
        refresh_customer_follow(customer)
        title = customer.company_name
    elif row.kind == "project":
        customer = db.session.get(Customer, payload.get("project", {}).get("customer_id"))
        if not customer:
            raise ValueError("原客户已被删除，请先恢复该客户，或把项目恢复到其他客户下")
        _restore_project_block(payload, customer.id)
        title = payload.get("project", {}).get("project_name") or row.title
    else:
        raise ValueError("未知的回收站记录类型")

    db.session.delete(row)
    db.session.commit()
    return title


def purge(row: RecycleBin) -> int:
    """彻底删除：清理磁盘文件后移除回收站记录，返回清理的文件数"""
    try:
        files = json.loads(row.file_paths or "[]")
    except ValueError:
        files = []
    removed = 0
    for path in files:
        if remove_file_on_disk(path):
            removed += 1
    db.session.delete(row)
    db.session.commit()
    return removed
