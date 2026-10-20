# -*- coding: utf-8 -*-
"""路由层：项目管理模块"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import or_

from config import Config
from extensions import db
from models import (
    Attachment,
    Customer,
    OperationLog,
    Project,
    normalize_tax_rate,
)
from permissions import (
    PERM_DELETE,
    PERM_EDIT,
    PERM_VIEW,
    apply_customer_scope,
    apply_project_scope,
    can_create_data,
    can_edit_project,
    has_project_permission,
    project_access_required,
)
from recycle import send_project_to_bin
from utils import (
    current_per_page,
    owner_options,
    log_action,
    merge_local_choice,
    option_default,
    option_items,
    order_with_other_last,
    parse_amount,
    parse_date,
    resolve_custom_choice,
)

bp = Blueprint("project", __name__)

# 列表排序白名单：只允许这些字段，避免把 URL 参数直接拼进 order_by
SORT_OPTIONS = {
    "created": "最近录入",
    "amount": "含税金额",
    "end_date": "结束日期",
    "sign_date": "签订日期",
    "name": "项目名称",
}


def _order_by(sort: str, order: str):
    """按白名单把排序参数翻译成 SQLAlchemy 排序表达式（默认最近录入在前）"""
    desc = order != "asc"
    mapping = {
        "created": Project.created_at,
        "amount": Project.tax_included_amount,
        "end_date": Project.end_date,
        "sign_date": Project.sign_date,
        "name": Project.project_name,
    }
    column = mapping.get(sort, Project.created_at)
    primary = column.desc() if desc else column.asc()
    # 次级键保证同值行顺序稳定，翻页不会重复或漏行
    return (primary, Project.id.desc())


def _type_options() -> list[str]:
    """项目类型候选：后台「选项管理」中启用的条目，「其他」固定在最底部。

    表单里的「自定义…」只作用于保存它的那条项目记录（存进 project_type 字段），
    不并入这里的候选，因此不会影响其他项目、筛选器或全局字典。
    """
    return option_items("project_type")


def _status_options() -> list[str]:
    """项目状态候选：后台「选项管理」中启用的条目"""
    return option_items("project_status")


def _clean_choice(value: str, options, fallback: str) -> str:
    """下拉选项归一化：只接受字典内的值，脏数据回落到默认值"""
    value = (value or "").strip()[:16]
    return value if value in options else fallback


def _credit_days(value) -> int:
    """账期天数：0~365，非法输入回落 30 天"""
    try:
        days = int(str(value or "").strip())
    except ValueError:
        return 30
    return max(0, min(365, days))


def _contract_fields(form) -> dict:
    """合同要素（新增与编辑共用）"""
    return {
        "owner": (form.get("owner") or "").strip()[:64],
        "contract_no": (form.get("contract_no") or "").strip()[:64] or None,
        "sign_date": parse_date(form.get("sign_date")),
        "payment_terms": (form.get("payment_terms") or "").strip() or None,
        "credit_days": _credit_days(form.get("credit_days")),
    }


def _apply_amount(project: Project, form) -> None:
    """按用户最后编辑的那一侧写入金额与税点，保证双向计算结果一致。

    前端联动脚本会把最后编辑的输入框记录到 amount_basis：
      - net   编辑的是「合同金额（不含税）」→ 正算含税价格
      - gross 编辑的是「含税价格」        → 反算合同金额
    服务端以 amount_basis 为准重算另一侧，不直接采信前端算好的数字。
    """
    rate = normalize_tax_rate(form.get("tax_rate"))
    basis = (form.get("amount_basis") or "net").strip()
    net = parse_amount(form.get("project_amount"))
    gross = parse_amount(form.get("tax_included_amount"))

    if basis == "gross" and gross:
        project.apply_gross(gross, rate)
    else:
        # 声明按含税价提交但含税价为空时，退回按合同金额正算，避免金额被清空
        project.apply_net(net, rate)


@bp.route("/")
@login_required
def list_projects():
    page = request.args.get("page", 1, type=int)
    per_page = current_per_page()
    keyword = (request.args.get("q") or "").strip()
    ptype = (request.args.get("type") or "").strip()
    status = (request.args.get("status") or "").strip()
    service = (request.args.get("service") or "").strip()
    owner = (request.args.get("owner") or "").strip()
    customer_id = request.args.get("customer_id", type=int)
    # 排序与区间筛选
    sort = (request.args.get("sort") or "created").strip()
    order = (request.args.get("order") or "desc").strip().lower()
    amount_min = parse_amount(request.args.get("amount_min"))
    amount_max = parse_amount(request.args.get("amount_max"))
    end_from = parse_date(request.args.get("end_from"))
    end_to = parse_date(request.args.get("end_to"))

    stmt = db.select(Project).join(Customer, Project.customer_id == Customer.id)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Project.project_name.like(like),
                Customer.company_name.like(like),
                Project.project_summary.like(like),
                # 自定义类型/状态不进入全局候选，靠关键字仍可检索到
                Project.project_type.like(like),
                Project.project_status.like(like),
                Project.contract_no.like(like),
            )
        )
    if ptype:
        stmt = stmt.where(Project.project_type == ptype)
    if status:
        stmt = stmt.where(Project.project_status == status)
    if service:
        stmt = stmt.where(Project.service_mode == service)
    if owner:
        stmt = stmt.where(Project.owner == owner)
    if customer_id:
        stmt = stmt.where(Project.customer_id == customer_id)
    if amount_min > 0:
        stmt = stmt.where(Project.tax_included_amount >= amount_min)
    if amount_max > 0:
        stmt = stmt.where(Project.tax_included_amount <= amount_max)
    if end_from:
        stmt = stmt.where(Project.end_date >= end_from)
    if end_to:
        stmt = stmt.where(Project.end_date <= end_to)

    # 数据范围：普通成员只能看到被授权的项目
    stmt = apply_project_scope(stmt, PERM_VIEW)
    stmt = stmt.order_by(*_order_by(sort, order))
    pagination = db.paginate(stmt, page=page, per_page=per_page, error_out=False)

    total_amount = sum(p.project_amount or 0 for p in pagination.items)
    total_gross = sum(p.tax_included_amount or 0 for p in pagination.items)
    return render_template(
        "project/list.html",
        pagination=pagination,
        keyword=keyword,
        ptype=ptype,
        status=status,
        service=service,
        owner=owner,
        owner_filter_options=merge_local_choice(owner_options(), owner),
        # 批量下拉：启用中的字典项；筛选下拉：额外带上当前筛选值，仅本次回显
        status_options=_status_options(), type_options=_type_options(),
        filter_status_options=merge_local_choice(_status_options(), status),
        filter_type_options=merge_local_choice(_type_options(), ptype),
        service_options=option_items("service_mode"),
        filter_service_options=merge_local_choice(option_items("service_mode"), service),
        customer_id=customer_id,
        per_page=per_page,
        page_amount=round(total_amount, 2),
        page_gross=round(total_gross, 2),
        sort=sort, order=order,
        amount_min=request.args.get("amount_min") or "",
        amount_max=request.args.get("amount_max") or "",
        end_from=request.args.get("end_from") or "",
        end_to=request.args.get("end_to") or "",
        sort_options=SORT_OPTIONS,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_project():
    # 新增属于全局操作，不落在任何单个项目授权上，由「允许新增客户/项目」开关控制
    if not can_create_data():
        flash("没有新增项目的权限，如需开通请联系管理员在「权限管理」中授权", "danger")
        return redirect(url_for("project.list_projects"))

    customers = db.session.scalars(
        apply_customer_scope(db.select(Customer)).order_by(Customer.company_name)
    ).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        project_name = (request.form.get("project_name") or "").strip()

        if not customer_id or not db.session.get(Customer, customer_id):
            flash("请选择所属客户", "danger")
        elif not project_name:
            flash("项目名称不能为空", "danger")
        else:
            start_date = parse_date(request.form.get("start_date"))
            end_date = parse_date(request.form.get("end_date"))
            if start_date and end_date and end_date < start_date:
                flash("结束日期不能早于开始日期，请检查项目周期", "danger")
                return render_template(
                    "project/form.html", project=None, customers=customers, form=request.form,
                    status_options=_status_options(), type_options=_type_options(),
                    default_status=option_default("project_status"),
                    default_service=option_default("service_mode"),
                    default_payment=option_default("payment_method"),
                )
            p = Project(
                customer_id=customer_id,
                project_name=project_name,
                project_type=resolve_custom_choice(form=request.form, field="project_type",
                                                   fallback="其他") or "其他",
                tax_rate=normalize_tax_rate(request.form.get("tax_rate")),
                start_date=start_date,
                end_date=end_date,
                project_status=resolve_custom_choice(form=request.form, field="project_status",
                                                     fallback="其他") or "待实施",
                service_mode=_clean_choice(request.form.get("service_mode"),
                                           option_items("service_mode"),
                                           option_default("service_mode") or "远程服务"),
                payment_method=_clean_choice(request.form.get("payment_method"),
                                             option_items("payment_method"),
                                             option_default("payment_method") or "支付软件转账"),
                project_summary=(request.form.get("project_summary") or "").strip(),
                **_contract_fields(request.form),
            )
            _apply_amount(p, request.form)
            db.session.add(p)
            db.session.commit()
            log_action("新增", "项目", p.id, f"新增项目：{p.project_name}")
            flash(f"项目「{p.project_name}」创建成功，可继续上传附件", "success")
            return redirect(url_for("project.detail", pid=p.id))

        return render_template(
            "project/form.html", project=None, customers=customers, form=request.form,
            status_options=_status_options(), type_options=_type_options(),
            default_status=option_default("project_status"),
            default_service=option_default("service_mode"),
            default_payment=option_default("payment_method"),
        )

    pre_customer = request.args.get("customer_id", type=int)
    return render_template(
        "project/form.html",
        project=None,
        customers=customers,
        form={"customer_id": pre_customer} if pre_customer else {},
        status_options=_status_options(), type_options=_type_options(),
        default_status=option_default("project_status"),
        default_service=option_default("service_mode"),
        default_payment=option_default("payment_method"),
    )


@bp.route("/<int:pid>")
@login_required
@project_access_required(PERM_VIEW)
def detail(pid: int):
    p = db.get_or_404(Project, pid)
    # 附件、收款、附件等操作都会写 target_type=项目 + 本项目 id，这里统一取出来内嵌展示
    object_logs = db.session.scalars(
        db.select(OperationLog)
        .where(OperationLog.target_type == "项目", OperationLog.target_id == pid)
        .order_by(OperationLog.create_time.desc())
        .limit(8)
    ).all()
    return render_template("project/detail.html", project=p, object_logs=object_logs)


@bp.route("/<int:pid>/edit", methods=["GET", "POST"])
@login_required
@project_access_required(PERM_EDIT)
def edit_project(pid: int):
    p = db.get_or_404(Project, pid)
    customers = db.session.scalars(
        apply_customer_scope(db.select(Customer)).order_by(Customer.company_name)
    ).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        project_name = (request.form.get("project_name") or "").strip()
        if not customer_id or not project_name:
            flash("所属客户与项目名称均为必填", "danger")
            return render_template(
                "project/form.html", project=p, customers=customers, form=request.form,
                status_options=_status_options(), type_options=_type_options()
            )

        start_date = parse_date(request.form.get("start_date"))
        end_date = parse_date(request.form.get("end_date"))
        if start_date and end_date and end_date < start_date:
            flash("结束日期不能早于开始日期，请检查项目周期", "danger")
            return render_template(
                "project/form.html", project=p, customers=customers, form=request.form,
                status_options=_status_options(), type_options=_type_options(),
                default_status=option_default("project_status"),
                default_service=option_default("service_mode"),
                default_payment=option_default("payment_method"),
            )

        p.customer_id = customer_id
        p.project_name = project_name
        p.project_type = resolve_custom_choice(form=request.form, field="project_type",
                                               fallback="其他") or "其他"
        _apply_amount(p, request.form)
        p.start_date = start_date
        p.end_date = end_date
        p.project_status = resolve_custom_choice(form=request.form, field="project_status",
                                                 fallback="其他") or "待实施"
        p.service_mode = _clean_choice(request.form.get("service_mode"),
                                       option_items("service_mode"),
                                       option_default("service_mode") or "远程服务")
        p.payment_method = _clean_choice(request.form.get("payment_method"),
                                         option_items("payment_method"),
                                         option_default("payment_method") or "支付软件转账")
        p.project_summary = (request.form.get("project_summary") or "").strip()
        for field, value in _contract_fields(request.form).items():
            setattr(p, field, value)
        db.session.commit()
        log_action("修改", "项目", p.id, f"修改项目：{p.project_name}")
        flash("项目信息已更新", "success")
        return redirect(url_for("project.detail", pid=p.id))

    return render_template("project/form.html", project=p, customers=customers, form={},
                           status_options=_status_options(), type_options=_type_options(),
                           default_status=option_default("project_status"),
                           default_service=option_default("service_mode"),
                           default_payment=option_default("payment_method"))


@bp.route("/<int:pid>/delete", methods=["POST"])
@login_required
@project_access_required(PERM_DELETE)
def delete_project(pid: int):
    p = db.get_or_404(Project, pid)
    name = p.project_name
    # 整份快照进回收站（含附件/收款/回款计划/跟进），附件文件留在磁盘上等恢复
    send_project_to_bin(p)
    db.session.delete(p)
    db.session.commit()
    log_action("删除", "项目", pid, f"删除项目：{name}（连带附件，可在回收站恢复）")
    flash(f"项目「{name}」及其附件已移入回收站，可由管理员恢复", "success")
    return redirect(url_for("project.list_projects"))


@bp.route("/batch", methods=["POST"])
@login_required
def batch():
    """批量管理：删除 / 修改状态 / 修改类型

    逐条校验权限：无权限的记录会被跳过并统计，避免整批操作误伤。
    """
    ids = request.form.getlist("ids", type=int)
    action = (request.form.get("action") or "").strip()
    value = (request.form.get("value") or "").strip()[:64]

    if not ids:
        flash("请先勾选要操作的项目", "warning")
        return redirect(url_for("project.list_projects"))

    projects = [p for p in (db.session.get(Project, i) for i in ids) if p]
    if not projects:
        flash("选中的项目已不存在", "warning")
        return redirect(url_for("project.list_projects"))

    done, skipped = 0, 0

    if action == "delete":
        names = []
        for p in projects:
            if not has_project_permission(p, PERM_DELETE):
                skipped += 1
                continue
            send_project_to_bin(p)
            names.append(p.project_name)
            db.session.delete(p)
            done += 1
        db.session.commit()
        if done:
            preview = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
            log_action("删除", "项目", None,
                       f"批量删除 {done} 个项目：{preview}（连带附件，可在回收站恢复）")
        msg = f"已将 {done} 个项目及其附件移入回收站"
        flash(msg + (f"，{skipped} 个因无删除权限被跳过" if skipped else ""),
              "success" if not skipped else "warning")
        return redirect(url_for("project.list_projects"))

    if action not in ("status", "type") or not value:
        flash("请选择批量操作类型与目标值", "danger")
        return redirect(url_for("project.list_projects"))

    for p in projects:
        if not has_project_permission(p, PERM_EDIT):
            skipped += 1
            continue
        if action == "status":
            p.project_status = value
        else:
            p.project_type = value
        done += 1
    db.session.commit()

    label = "状态" if action == "status" else "类型"
    if done:
        log_action("修改", "项目", None, f"批量将 {done} 个项目的{label}改为「{value}」")
    flash(
        f"已将 {done} 个项目的{label}改为「{value}」"
        + (f"，{skipped} 个因无修改权限被跳过" if skipped else ""),
        "success" if not skipped else "warning",
    )
    return redirect(url_for("project.list_projects"))
