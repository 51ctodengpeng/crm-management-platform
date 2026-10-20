# -*- coding: utf-8 -*-
"""路由层：客户管理模块"""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_

from extensions import db
from models import Contact, Customer, OperationLog, Project, sync_primary_contact
from recycle import send_customer_to_bin
from permissions import (
    apply_customer_scope,
    apply_project_scope,
    can_create_data,
    can_delete_customer,
    can_edit_customer,
    customer_access_required,
    PERM_DELETE,
    PERM_EDIT,
    PERM_VIEW,
)
from utils import (
    current_per_page,
    owner_options,
    log_action,
    merge_local_choice,
    option_default,
    option_items,
    parse_date,
    resolve_custom_choice,
    resolve_source_channel,
)

bp = Blueprint("customer", __name__)

# 客户列表排序白名单（字段名 -> 展示名）
SORT_OPTIONS = {
    "updated": "最近修改",
    "created": "最近录入",
    "name": "公司名称",
    "level": "客户等级",
}


def _order_by(sort: str, order: str):
    """客户列表排序：默认按最后修改时间倒序，同值用 id 兜底保证翻页稳定"""
    desc = order != "asc"
    mapping = {
        "updated": Customer.updated_at,
        "created": Customer.created_at,
        "name": Customer.company_name,
        "level": Customer.customer_level,
    }
    column = mapping.get(sort, Customer.updated_at)
    return (column.desc() if desc else column.asc(), Customer.id.desc())


def _channel_options() -> list[str]:
    """来源渠道候选：后台「选项管理」中启用的条目。

    表单里的「自定义…」只作用于当前这条客户记录，不并入候选、不写进字典。
    """
    return option_items("customer_channel")


def _business_options() -> list[str]:
    """行业类型候选：后台「选项管理」中启用的条目（「其他」恒在最后）"""
    return option_items("customer_business")


def _level_options() -> list[str]:
    """客户等级候选：后台「选项管理」中启用的条目"""
    return option_items("customer_level")


def _apply_form(customer: Customer, form):
    """把表单写入客户对象（新增与编辑共用）"""
    customer.company_name = (form.get("company_name") or "").strip()
    customer.contact_name = (form.get("contact_name") or "").strip()
    customer.position = (form.get("position") or "").strip()[:32]
    customer.contact_phone = (form.get("contact_phone") or "").strip()
    customer.contact_email = (form.get("contact_email") or "").strip()
    customer.wechat = (form.get("wechat") or "").strip()
    # 所在地区：三级联动提交省/市/区，手动模式下提交整段文本；两处去重拼接
    province = (form.get("province") or "").strip()
    city = (form.get("city") or "").strip()
    district = (form.get("district") or "").strip()
    # 三级结构化落库：只保留层级自洽的组合（有区必有市、有市必有省），
    # 避免出现「省空、区有值」这类无法统计的脏数据；region 的拼接结果不受影响
    if not province:
        city, district = "", ""
    if not city:
        district = ""
    customer.province = province[:32] or None
    customer.city = city[:32] or None
    customer.district = district[:32] or None
    if province or city or district:
        parts: list[str] = []
        for value in (province, city, district):
            if value and value not in parts:
                parts.append(value)
        customer.region = " ".join(parts)[:64]
    else:
        customer.region = (form.get("region") or "").strip()[:64]
    customer.address = (form.get("address") or "").strip()
    # 行业类型支持「其他 / 自定义」：选了自定义取输入框，留空则记「其他」
    customer.business_type = resolve_custom_choice(form, "business_type", fallback="其他")
    customer.customer_level = (form.get("customer_level") or "普通").strip()
    customer.source_channel = resolve_source_channel(form)
    customer.remark = (form.get("remark") or "").strip()
    customer.owner = (form.get("owner") or "").strip()[:64]


def _sync_contact_from_form(customer: Customer, form) -> None:
    """把表单里的对接人字段同步到联系人子表

    - 已有联系人：更新主联系人（没有主联系人时更新第一条）；
    - 没有联系人但填了任意联系方式：自动建一条主联系人；
    - 全部留空且只有一条联系人：删掉这条空记录，避免详情页出现空卡片。
    """
    fields = {
        "name": (form.get("contact_name") or "").strip()[:64],
        "position": (form.get("position") or "").strip()[:32],
        "phone": (form.get("contact_phone") or "").strip()[:32],
        "email": (form.get("contact_email") or "").strip()[:128],
        "wechat": (form.get("wechat") or "").strip()[:64],
    }
    has_value = any(fields.values())
    primary = next((c for c in customer.contacts if c.is_primary), None)
    if primary is None and customer.contacts:
        primary = customer.contacts[0]
    if primary:
        if not has_value and len(customer.contacts) == 1:
            customer.contacts.remove(primary)
            return
        primary.name = fields["name"] or primary.name or "主要联系人"
        primary.position = fields["position"]
        primary.phone = fields["phone"]
        primary.email = fields["email"]
        primary.wechat = fields["wechat"]
        primary.is_primary = True
    elif has_value:
        customer.contacts.append(Contact(
            name=fields["name"] or "主要联系人", position=fields["position"],
            phone=fields["phone"], email=fields["email"], wechat=fields["wechat"],
            is_primary=True,
        ))
    sync_primary_contact(customer)



def _render_form(customer, form, duplicates=None):
    """客户表单渲染（新增 / 编辑共用），duplicates 非空时页面顶部给出查重提示"""
    return render_template(
        "customer/form.html", customer=customer, form=form,
        business_options=_business_options(), channel_options=_channel_options(),
        default_level=option_default("customer_level"),
        duplicates=duplicates or [],
    )


def _norm_name(name: str) -> str:
    """公司名归一化：忽略空格、大小写与中英文括号差异，用于查重"""
    import re
    return re.sub(r"[\s（）()]+", "", (name or "").strip().lower())


def find_duplicates(company_name: str, phone: str = "", exclude_id: int | None = None) -> list:
    """查重：公司名归一化相同，或联系电话相同（填了才比）

    只查有权限看到的客户，避免普通成员通过提示反查出无权限的数据。
    """
    name_key = _norm_name(company_name)
    phone = (phone or "").strip()
    if not name_key and not phone:
        return []
    stmt = apply_customer_scope(db.select(Customer))
    if exclude_id:
        stmt = stmt.where(Customer.id != exclude_id)
    rows = db.session.scalars(stmt.limit(500)).all()
    hits = []
    for row in rows:
        if _norm_name(row.company_name) == name_key:
            hits.append(row)
        elif phone and (row.contact_phone or "").strip() == phone:
            hits.append(row)
    return hits[:5]


@bp.route("/")
@login_required
def list_customers():
    page = request.args.get("page", 1, type=int)
    per_page = current_per_page()
    keyword = (request.args.get("q") or "").strip()
    level = (request.args.get("level") or "").strip()
    business = (request.args.get("business") or "").strip()
    channel = (request.args.get("channel") or "").strip()
    owner = (request.args.get("owner") or "").strip()
    sort = (request.args.get("sort") or "updated").strip()
    order = (request.args.get("order") or "desc").strip().lower()
    created_from = parse_date(request.args.get("created_from"))
    created_to = parse_date(request.args.get("created_to"))

    stmt = db.select(Customer)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Customer.company_name.like(like),
                Customer.contact_name.like(like),
                Customer.contact_phone.like(like),
                Customer.wechat.like(like),
                Customer.contact_email.like(like),
                Customer.region.like(like),
                Customer.address.like(like),
                Customer.remark.like(like),
                # 自定义行业/渠道不进入全局候选，靠关键字仍可检索到
                Customer.business_type.like(like),
                Customer.source_channel.like(like),
            )
        )
    if level:
        stmt = stmt.where(Customer.customer_level == level)
    if business:
        stmt = stmt.where(Customer.business_type == business)
    if channel:
        stmt = stmt.where(Customer.source_channel == channel)
    if owner:
        stmt = stmt.where(Customer.owner == owner)
    if created_from:
        stmt = stmt.where(Customer.created_at >= datetime.combine(created_from, datetime.min.time()))
    if created_to:
        # 结束日期按「当天 23:59:59」包含当天
        stmt = stmt.where(Customer.created_at <= datetime.combine(created_to, datetime.max.time()))

    # 数据范围：普通成员只能看到其被授权项目所属的客户
    stmt = apply_customer_scope(stmt)
    stmt = stmt.order_by(*_order_by(sort, order))
    pagination = db.paginate(stmt, page=page, per_page=per_page, error_out=False)

    # 本页金额合计，便于快速核对
    page_amount = round(sum(c.total_amount for c in pagination.items), 2)
    return render_template(
        "customer/list.html",
        pagination=pagination,
        keyword=keyword,
        level=level,
        business=business,
        channel=channel,
        owner=owner,
        owner_filter_options=merge_local_choice(owner_options(), owner),
        per_page=per_page,
        page_amount=page_amount,
        # 批量操作下拉：只用启用中的字典项
        channel_options=_channel_options(),
        business_options=_business_options(),
        level_options=_level_options(),
        # 筛选下拉：当前筛选值仅本次回显，不并入全局候选
        filter_channel_options=merge_local_choice(_channel_options(), channel),
        filter_business_options=merge_local_choice(_business_options(), business),
        filter_level_options=merge_local_choice(_level_options(), level),
        default_level=option_default("customer_level"),
        sort=sort, order=order,
        created_from=request.args.get("created_from") or "",
        created_to=request.args.get("created_to") or "",
        sort_options=SORT_OPTIONS,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_customer():
    if not can_create_data():
        flash("没有新增客户的权限，如需开通请联系管理员在「权限管理」中授权", "danger")
        return redirect(url_for("customer.list_customers"))

    if request.method == "POST":
        if not (request.form.get("company_name") or "").strip():
            flash("客户公司名称不能为空", "danger")
            return _render_form(None, request.form)

        # 查重：命中时先不保存，把疑似重复的客户列出来由用户确认（确认按钮带 force=1）
        duplicates = find_duplicates(request.form.get("company_name"),
                                     request.form.get("contact_phone"))
        if duplicates and request.form.get("force") != "1":
            flash("系统中已存在相似客户，请确认是否重复录入", "warning")
            return _render_form(None, request.form, duplicates)

        c = Customer()
        _apply_form(c, request.form)
        _sync_contact_from_form(c, request.form)
        db.session.add(c)
        db.session.commit()
        log_action("新增", "客户", c.id,
                   f"新增客户：{c.company_name}"
                   + (f"（来源：{c.source_channel}）" if c.source_channel else ""))
        flash(f"客户「{c.company_name}」创建成功", "success")
        return redirect(url_for("customer.detail", cid=c.id))

    return _render_form(None, {})


@bp.route("/<int:cid>")
@login_required
@customer_access_required(PERM_VIEW)
def detail(cid: int):
    c = db.get_or_404(Customer, cid)
    projects = db.session.scalars(
        apply_project_scope(
            db.select(Project).where(Project.customer_id == cid), PERM_VIEW
        ).order_by(Project.id.desc())
    ).all()
    # 该客户全部附件数量，方便判断资料是否齐全
    attachment_count = sum(p.attachment_count for p in projects)
    # 本对象的操作记录：来源同「操作日志」，按 target_type + target_id 过滤
    object_logs = db.session.scalars(
        db.select(OperationLog)
        .where(OperationLog.target_type == "客户", OperationLog.target_id == cid)
        .order_by(OperationLog.create_time.desc())
        .limit(8)
    ).all()
    return render_template(
        "customer/detail.html",
        customer=c,
        projects=projects,
        attachment_count=attachment_count,
        object_logs=object_logs,
    )


@bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
@customer_access_required(PERM_EDIT)
def edit_customer(cid: int):
    c = db.get_or_404(Customer, cid)

    if request.method == "POST":
        if not (request.form.get("company_name") or "").strip():
            flash("客户公司名称不能为空", "danger")
            return _render_form(c, request.form)

        duplicates = find_duplicates(request.form.get("company_name"),
                                     request.form.get("contact_phone"), exclude_id=c.id)
        if duplicates and request.form.get("force") != "1":
            flash("系统中已存在相似客户，请确认是否为同一家", "warning")
            return _render_form(c, request.form, duplicates)

        _apply_form(c, request.form)
        _sync_contact_from_form(c, request.form)
        db.session.commit()
        log_action("修改", "客户", c.id, f"修改客户：{c.company_name}")
        flash("客户信息已更新", "success")
        return redirect(url_for("customer.detail", cid=c.id))

    return _render_form(c, {})


@bp.route("/<int:cid>/contact/add", methods=["POST"])
@login_required
@customer_access_required(PERM_EDIT)
def add_contact(cid: int):
    customer = db.get_or_404(Customer, cid)
    name = (request.form.get("name") or "").strip()[:64]
    if not name:
        flash("联系人姓名不能为空", "danger")
        return redirect(url_for("customer.detail", cid=cid) + "#contacts")
    is_primary = request.form.get("is_primary") == "1" or not customer.contacts
    if is_primary:
        for row in customer.contacts:
            row.is_primary = False
    row = Contact(
        customer_id=cid, name=name,
        position=(request.form.get("position") or "").strip()[:32],
        phone=(request.form.get("phone") or "").strip()[:32],
        email=(request.form.get("email") or "").strip()[:128],
        wechat=(request.form.get("wechat") or "").strip()[:64],
        remark=(request.form.get("remark") or "").strip()[:255],
        is_primary=is_primary,
        sort_order=len(customer.contacts),
    )
    db.session.add(row)
    db.session.flush()
    db.session.refresh(customer)
    sync_primary_contact(customer)
    db.session.commit()
    log_action("新增", "客户", cid, f"客户「{customer.company_name}」新增联系人：{name}")
    flash(f"已添加联系人 {name}" + ("（设为主联系人）" if is_primary else ""), "success")
    return redirect(url_for("customer.detail", cid=cid) + "#contacts")


@bp.route("/<int:cid>/contact/<int:fid>/primary", methods=["POST"])
@login_required
@customer_access_required(PERM_EDIT)
def set_primary_contact(cid: int, fid: int):
    customer = db.get_or_404(Customer, cid)
    target = db.get_or_404(Contact, fid)
    if target.customer_id != cid:
        flash("联系人与客户不匹配", "danger")
        return redirect(url_for("customer.detail", cid=cid))
    for row in customer.contacts:
        row.is_primary = row.id == fid
    sync_primary_contact(customer)
    db.session.commit()
    log_action("修改", "客户", cid, f"客户「{customer.company_name}」主联系人改为：{target.name}")
    flash(f"已将 {target.name} 设为主联系人", "success")
    return redirect(url_for("customer.detail", cid=cid) + "#contacts")


@bp.route("/<int:cid>/contact/<int:fid>/delete", methods=["POST"])
@login_required
@customer_access_required(PERM_EDIT)
def delete_contact(cid: int, fid: int):
    customer = db.get_or_404(Customer, cid)
    target = db.get_or_404(Contact, fid)
    if target.customer_id != cid:
        flash("联系人与客户不匹配", "danger")
        return redirect(url_for("customer.detail", cid=cid))
    name = target.name
    was_primary = target.is_primary
    customer.contacts.remove(target)
    db.session.flush()
    db.session.refresh(customer)
    if was_primary and customer.contacts:
        customer.contacts[0].is_primary = True
    sync_primary_contact(customer)
    db.session.commit()
    log_action("删除", "客户", cid, f"客户「{customer.company_name}」删除联系人：{name}")
    flash(f"已删除联系人 {name}", "success")
    return redirect(url_for("customer.detail", cid=cid) + "#contacts")


@bp.route("/<int:cid>/delete", methods=["POST"])
@login_required
@customer_access_required(PERM_DELETE)
def delete_customer(cid: int):
    c = db.get_or_404(Customer, cid)
    name = c.company_name
    # 先整份快照进回收站（含项目/附件/收款/计划/跟进），再删业务数据；
    # 附件物理文件保留在 uploads 里，恢复时按快照重新关联
    send_customer_to_bin(c)
    db.session.delete(c)
    db.session.commit()
    log_action("删除", "客户", cid, f"删除客户：{name}（连带项目与附件，可在回收站恢复）")
    flash(f"客户「{name}」及其项目、附件已移入回收站，可由管理员恢复", "success")
    return redirect(url_for("customer.list_customers"))


@bp.route("/batch", methods=["POST"])
@login_required
def batch():
    """批量管理：删除 / 修改客户等级 / 修改行业类型

    删除客户会连带删除项目与附件，因此仅「全部数据」范围的账号可执行；
    修改等级 / 行业按客户是否有可编辑项目逐条校验。
    """
    ids = request.form.getlist("ids", type=int)
    action = (request.form.get("action") or "").strip()
    value = (request.form.get("value") or "").strip()[:32]

    if not ids:
        flash("请先勾选要操作的客户", "warning")
        return redirect(url_for("customer.list_customers"))

    customers = [c for c in (db.session.get(Customer, i) for i in ids) if c]
    if not customers:
        flash("选中的客户已不存在", "warning")
        return redirect(url_for("customer.list_customers"))

    done, skipped = 0, 0

    if action == "delete":
        names = []
        for c in customers:
            if not can_delete_customer(c):
                skipped += 1
                continue
            send_customer_to_bin(c)
            names.append(c.company_name)
            db.session.delete(c)
            done += 1
        db.session.commit()
        if done:
            preview = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
            log_action("删除", "客户", None,
                       f"批量删除 {done} 个客户：{preview}（连带项目与附件，可在回收站恢复）")
        flash(
            f"已将 {done} 个客户及其项目、附件移入回收站"
            + (f"，{skipped} 个因无删除权限被跳过" if skipped else ""),
            "success" if not skipped else "warning",
        )
        return redirect(url_for("customer.list_customers"))

    if action not in ("level", "business") or not value:
        flash("请选择批量操作类型与目标值", "danger")
        return redirect(url_for("customer.list_customers"))

    for c in customers:
        if not can_edit_customer(c):
            skipped += 1
            continue
        if action == "level":
            c.customer_level = value
        else:
            c.business_type = value
        done += 1
    db.session.commit()

    label = "客户等级" if action == "level" else "行业类型"
    if done:
        log_action("修改", "客户", None, f"批量将 {done} 个客户的{label}改为「{value}」")
    flash(
        f"已将 {done} 个客户的{label}改为「{value}」"
        + (f"，{skipped} 个因无修改权限被跳过" if skipped else ""),
        "success" if not skipped else "warning",
    )
    return redirect(url_for("customer.list_customers"))
