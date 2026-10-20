# -*- coding: utf-8 -*-
"""路由层：收款登记 + 转账对账

对账口径（全系统统一）：
  应收 = 项目含税价格（Project.tax_included_amount）
  已收 = 本项目 status='matched' 的收款金额之和
  差额 = 应收 - 已收（正数待收、负数超收）
一笔收款登记后进入「待对账」，核对流水无误后才计入已收，避免"填了就算收到"。
"""
from datetime import datetime
from contextlib import contextmanager
import threading

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload

import dbcompat
from config import Config
from extensions import db
from models import Payment, PaymentPlan, Project, money_round
from permissions import (
    PERM_DELETE,
    PERM_EDIT,
    PERM_VIEW,
    apply_project_scope,
    has_project_permission,
)
from utils import (
    current_per_page,
    is_transfer_method,
    log_action,
    merge_local_choice,
    option_items,
    parse_amount,
    parse_date,
)

bp = Blueprint("finance", __name__)

EPS = 0.005          # 金额比较容差（半分钱），避免浮点误差误判


class ReconcileBusy(Exception):
    """对账时项目行被另一个请求占用（数据库死锁 / 锁等待超时）"""


# 同一进程内按项目串行化"读已收 → 判断超收 → 写已对账"这段临界区。
# 生产是 2 worker × 4 threads，进程内锁挡线程；跨进程靠 MySQL 的行锁（见 _reconcile_guard）。
_project_locks: dict[int, threading.Lock] = {}
_project_locks_guard = threading.Lock()


def _project_lock(project_id: int) -> threading.Lock:
    """取（必要时创建）某个项目的对账锁"""
    with _project_locks_guard:
        lock = _project_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _project_locks[project_id] = lock
        return lock


@contextmanager
def _reconcile_guard(project_id: int):
    """对账临界区：进程锁 + MySQL 行锁

    对账是典型的 check-then-act：先读「已收合计」判断会不会超收，再写「已对账」。
    没有锁时两个并发请求会各自读到旧值、各自通过校验，导致已收超过应收。
    串行使用（单用户、单请求）时行为与以前完全一致，只是多了一次极廉价的加锁。
    """
    lock = _project_lock(project_id)
    with lock:
        try:
            if dbcompat.is_mysql():
                db.session.execute(
                    db.text("SELECT id FROM project WHERE id = :pid FOR UPDATE"),
                    {"pid": project_id},
                )
            yield
        except OperationalError as exc:
            message = str(exc).lower()
            if "deadlock" in message or "lock wait timeout" in message:
                db.session.rollback()
                raise ReconcileBusy() from exc
            raise


def _guard(project_id: int, action: str) -> bool:
    """收款记录继承所属项目的权限位"""
    if has_project_permission(project_id, action):
        return True
    label = {"view": "查看", "edit": "修改", "delete": "删除"}.get(action, "操作")
    flash(f"没有该项目收款的{label}权限，如需开通请联系管理员在「权限管理」中授权", "danger")
    return False


def _clean(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def _check_reconcile(payment: Payment, allow_over: bool = False) -> str | None:
    """对账前置校验，返回错误文案；None 表示通过

    规则（转账与现金都适用，转账额外要求流水号）：
      1. 已是「已对账」→ 不重复对账
      2. 金额必须 > 0
      3. 转账必须有交易流水号，且不能与其他已对账记录重复（防一笔记两次）
      4. 已对账金额 + 本笔 ≤ 应收 + 容差，否则视为超收，需显式勾选允许
    """
    if payment.status == "matched":
        return "该笔记录已对账，无需重复操作"
    if money_round(payment.amount) <= 0:
        return "收款金额必须大于 0"
    if payment.is_transfer:
        if not (payment.trade_no or "").strip():
            return f"「{payment.pay_method or '转账'}」方式必须填写交易流水号才能对账"
        dup = db.session.scalar(
            db.select(Payment).where(
                Payment.trade_no == payment.trade_no.strip(),
                Payment.id != payment.id,
                Payment.status == "matched",
            )
        )
        if dup:
            return f"流水号 {payment.trade_no} 已被另一笔记录对账，疑似重复入账"

    project = payment.project
    if not project:
        return "找不到所属项目"
    after = money_round(project.matched_amount + (payment.amount or 0))
    if after > project.receivable + EPS and not allow_over:
        over = money_round(after - project.receivable)
        return (f"本笔对账后累计已收 ¥{after:,.2f} 将超出应收 ¥{project.receivable:,.2f} "
                f"¥{over:,.2f}；如确为追加款项，请勾选「允许超收」后再提交")
    return None


def _do_reconcile(payment: Payment, allow_over: bool = False) -> str | None:
    """执行对账，成功返回 None，失败返回原因"""
    reason = _check_reconcile(payment, allow_over=allow_over)
    if reason:
        return reason
    payment.status = "matched"
    payment.reconciled_at = datetime.now()
    payment.reconciled_by = current_user.display_name or current_user.username
    return None


@bp.route("/")
@login_required
def reconcile():
    """对账中心：待对账优先，支持按状态 / 方式 / 关键字筛选与分页"""
    page = request.args.get("page", 1, type=int)
    per_page = current_per_page()
    status = (request.args.get("status") or "").strip()
    method = (request.args.get("method") or "").strip()
    keyword = (request.args.get("q") or "").strip()

    # 付款方式候选 = 启用中的字典项 + 当前筛选值（历史/停用的方式也要能回显）
    methods = merge_local_choice(option_items("payment_method"), method)

    conditions = []
    if status in Config.PAYMENT_STATUSES:
        conditions.append(Payment.status == status)
    if method and method in methods:
        conditions.append(Payment.pay_method == method)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(or_(
            Project.project_name.like(like),
            Payment.trade_no.like(like),
            Payment.payer.like(like),
            Payment.remark.like(like),
        ))

    def _base(select_stmt):
        if conditions:
            select_stmt = select_stmt.where(*conditions)
        return apply_project_scope(select_stmt, PERM_VIEW)

    # 列表：分页加载，并一次性带出项目与客户，避免逐行查询（N+1）
    stmt = _base(
        db.select(Payment).join(Project, Payment.project_id == Project.id)
    ).options(
        joinedload(Payment.project).joinedload(Project.customer)
    ).order_by(Payment.status.asc(), Payment.paid_at.desc(), Payment.id.desc())
    pagination = db.paginate(stmt, page=page, per_page=per_page, error_out=False)

    # 汇总：按当前筛选条件在数据库里聚合，不受分页影响
    summary = {
        "pending_count": 0, "pending_amount": 0.0,
        "matched_count": 0, "matched_amount": 0.0,
        "cash_amount": 0.0, "transfer_amount": 0.0,
    }
    sum_stmt = _base(
        db.select(Payment.status, func.count(Payment.id), func.sum(Payment.amount))
        .select_from(Payment).join(Project, Payment.project_id == Project.id)
        .group_by(Payment.status)
    )
    for st, cnt, amount in db.session.execute(sum_stmt).all():
        amount = money_round(amount or 0)
        if st == "matched":
            summary["matched_count"] = cnt
            summary["matched_amount"] = amount
        elif st == "pending":
            summary["pending_count"] = cnt
            summary["pending_amount"] = amount

    split_stmt = _base(
        db.select(Payment.transfer_flag, func.sum(Payment.amount))
        .select_from(Payment).join(Project, Payment.project_id == Project.id)
        .where(Payment.status == "matched")
        .group_by(Payment.transfer_flag)
    )
    for flag, amount in db.session.execute(split_stmt).all():
        if flag:
            summary["transfer_amount"] = money_round(amount or 0)
        else:
            summary["cash_amount"] = money_round(amount or 0)

    # 批量对账：覆盖当前筛选下的全部待对账记录（上限 1000，避免超大表单）
    pending_stmt = _base(
        db.select(Payment.id).join(Project, Payment.project_id == Project.id)
        .where(Payment.status == "pending")
        .order_by(Payment.paid_at.asc(), Payment.id.asc())
    ).limit(1000)
    pending_ids = list(db.session.scalars(pending_stmt).all())

    return render_template(
        "finance/reconcile.html",
        payments=pagination.items,
        pagination=pagination,
        summary=summary,
        pending_ids=pending_ids,
        status=status,
        method=method,
        keyword=keyword,
        statuses=Config.PAYMENT_STATUSES,
        methods=methods,
        per_page=per_page,
    )


@bp.route("/plan/<int:pid>/add", methods=["POST"])
@login_required
def add_plan(pid: int):
    """新增一期回款计划"""
    project = db.get_or_404(Project, pid)
    if not has_project_permission(project, PERM_EDIT):
        flash("没有该项目的修改权限，无法维护回款计划", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")

    due_date = parse_date(request.form.get("due_date"))
    amount = parse_amount(request.form.get("amount"))
    note = (request.form.get("note") or "").strip()[:120]
    if not due_date:
        flash("请填写约定回款日期", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")
    if amount <= 0:
        flash("计划金额必须大于 0", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")

    seq = (max([p.seq or 0 for p in project.payment_plans]) + 1) if project.payment_plans else 1
    ratio = round(amount / project.receivable * 100, 1) if project.receivable else None
    row = PaymentPlan(project_id=pid, seq=seq, due_date=due_date,
                      amount=amount, ratio=ratio, note=note or None)
    db.session.add(row)
    db.session.commit()
    log_action("新增", "项目", pid,
               f"项目「{project.project_name}」新增第 {seq} 期回款计划：{due_date} ¥{amount:,.2f}")
    total = money_round(sum(p.amount or 0 for p in project.payment_plans))
    if total > project.receivable + 0.005:
        flash(f"第 {seq} 期计划已加入；当前计划合计 ¥{total:,.2f} 已超过应收 ¥{project.receivable:,.2f}，请核对",
              "warning")
    else:
        flash(f"第 {seq} 期回款计划已加入（{due_date}，¥{amount:,.2f}）", "success")
    return redirect(url_for("project.detail", pid=pid) + "#payments")


@bp.route("/plan/<int:plan_id>/delete", methods=["POST"])
@login_required
def delete_plan(plan_id: int):
    """删除一期计划；已有关联收款时拒绝，避免账目失去归属"""
    plan = db.get_or_404(PaymentPlan, plan_id)
    project = plan.project
    if not has_project_permission(project, PERM_EDIT):
        flash("没有该项目的修改权限，无法删除回款计划", "danger")
        return redirect(url_for("project.detail", pid=plan.project_id) + "#payments")
    if plan.payments:
        flash("该期次已有收款记录关联，不能删除；如需调整请先处理关联的收款", "warning")
        return redirect(url_for("project.detail", pid=plan.project_id) + "#payments")

    seq = plan.seq
    db.session.delete(plan)
    db.session.commit()
    log_action("删除", "项目", project.id,
               f"项目「{project.project_name}」删除第 {seq} 期回款计划")
    flash(f"第 {seq} 期回款计划已删除", "success")
    return redirect(url_for("project.detail", pid=plan.project_id) + "#payments")


@bp.route("/add/<int:pid>", methods=["POST"])
@login_required
def add_payment(pid: int):
    """登记一笔收款（默认进入待对账）"""
    project = db.get_or_404(Project, pid)
    if not _guard(pid, PERM_EDIT):
        return redirect(url_for("project.detail", pid=pid) + "#payments")

    amount = parse_amount(request.form.get("amount"))
    pay_options = option_items("payment_method")
    pay_method = (request.form.get("pay_method") or "").strip()
    if pay_method not in pay_options:
        pay_method = pay_options[0] if pay_options else "支付软件转账"
    is_transfer = is_transfer_method(pay_method)
    pay_channel = _clean(request.form.get("pay_channel"), 32) if is_transfer else ""
    trade_no = _clean(request.form.get("trade_no"), 64) if is_transfer else ""
    payer = _clean(request.form.get("payer"), 64)
    paid_at = parse_date(request.form.get("paid_at")) or datetime.now().date()
    remark = _clean(request.form.get("remark"), 255)
    # 关联回款期次（可选）：只有本项目自己的计划才允许关联，防止串项目
    plan_id = request.form.get("plan_id", type=int)
    plan = db.session.get(PaymentPlan, plan_id) if plan_id else None
    if plan and plan.project_id != pid:
        plan = None

    if amount <= 0:
        flash("收款金额需大于 0", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")
    if is_transfer and not trade_no:
        flash("支付软件转账请填写交易流水号，便于后续对账", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")
    if trade_no and db.session.scalar(db.select(Payment).where(Payment.trade_no == trade_no)):
        flash(f"流水号 {trade_no} 已存在，请勿重复登记", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#payments")

    pay = Payment(
        project_id=pid,
        amount=money_round(amount),
        pay_method=pay_method,
        pay_channel=pay_channel,
        transfer_flag=is_transfer,
        # 空流水号存 NULL 而不是空串：MySQL 的唯一索引允许多个 NULL，
        # 这样「现金 / 未填流水号」的收款不会互相顶掉，语义与 SQLite 的部分索引一致
        trade_no=trade_no or None,
        payer=payer or (project.customer.company_name if project.customer else ""),
        paid_at=paid_at,
        status="pending",
        remark=remark,
        plan_id=plan.id if plan else None,
        created_by=current_user.display_name or current_user.username,
    )
    db.session.add(pay)
    db.session.commit()
    log_action("新增", "收款", pay.id,
               f"登记收款：{project.project_name} ¥{pay.amount:,.2f}"
               f"（{pay.method_label}，待对账"
               + (f"，第 {plan.seq} 期" if plan else "") + "）")
    flash(f"已登记收款 ¥{pay.amount:,.2f}"
          + (f"（计入第 {plan.seq} 期计划）" if plan else "")
          + "，核对流水后点「对账」计入已收", "success")
    return redirect(url_for("project.detail", pid=pid) + "#payments")


@bp.route("/<int:pay_id>/reconcile", methods=["POST"])
@login_required
def reconcile_one(pay_id: int):
    pay = db.get_or_404(Payment, pay_id)
    if not _guard(pay.project_id, PERM_EDIT):
        return redirect(url_for("project.detail", pid=pay.project_id) + "#payments")

    allow_over = request.form.get("allow_over") == "1"
    try:
        # 加锁范围覆盖到 commit：保证"判断超收 → 落库"之间没有别的对账插进来
        with _reconcile_guard(pay.project_id):
            reason = _do_reconcile(pay, allow_over=allow_over)
            if reason:
                db.session.rollback()
                flash(reason, "danger")
                return redirect(request.form.get("next") or url_for("finance.reconcile"))
            db.session.commit()
    except ReconcileBusy:
        flash("该项目正在被另一笔对账操作占用，请稍后重试", "danger")
        return redirect(request.form.get("next") or url_for("finance.reconcile"))

    log_action("对账", "收款", pay.id,
               f"收款对账通过：{pay.project.project_name} ¥{pay.amount:,.2f}"
               f"（流水号 {pay.trade_no or '现金无流水'}）")
    flash(f"对账成功，¥{pay.amount:,.2f} 已计入已收", "success")
    return redirect(request.form.get("next") or url_for("finance.reconcile"))


@bp.route("/<int:pay_id>/unreconcile", methods=["POST"])
@login_required
def unreconcile(pay_id: int):
    """撤销对账：属于风险操作，要求项目删除权限"""
    pay = db.get_or_404(Payment, pay_id)
    if not _guard(pay.project_id, PERM_DELETE):
        return redirect(url_for("project.detail", pid=pay.project_id) + "#payments")
    if pay.status != "matched":
        flash("该笔记录尚未对账，无需撤销", "warning")
        return redirect(request.form.get("next") or url_for("finance.reconcile"))

    pay.status = "pending"
    pay.reconciled_at = None
    pay.reconciled_by = None
    db.session.commit()
    log_action("撤销对账", "收款", pay.id,
               f"撤销对账：{pay.project.project_name} ¥{pay.amount:,.2f}，退回待对账")
    flash("已撤销对账，该笔金额不再计入已收", "warning")
    return redirect(request.form.get("next") or url_for("finance.reconcile"))


@bp.route("/<int:pay_id>/delete", methods=["POST"])
@login_required
def delete_payment(pay_id: int):
    pay = db.get_or_404(Payment, pay_id)
    project_id = pay.project_id
    if not _guard(project_id, PERM_DELETE):
        return redirect(url_for("project.detail", pid=project_id) + "#payments")
    label = f"{pay.project.project_name} ¥{pay.amount:,.2f}"
    db.session.delete(pay)
    db.session.commit()
    log_action("删除", "收款", pay_id, f"删除收款记录：{label}")
    flash("收款记录已删除", "success")
    return redirect(request.form.get("next") or url_for("finance.reconcile"))


@bp.route("/batch-reconcile", methods=["POST"])
@login_required
def batch_reconcile():
    """批量对账：逐条走同一套校验，失败记录收集原因后统一提示"""
    ids = request.form.getlist("ids", type=int)
    if not ids:
        flash("请先勾选要核对的对账记录", "warning")
        return redirect(url_for("finance.reconcile"))

    allow_over = request.form.get("allow_over") == "1"
    done, skipped, reasons = 0, 0, []
    busy = False
    for pid in ids:
        pay = db.session.get(Payment, pid)
        if not pay:
            skipped += 1
            continue
        if not has_project_permission(pay.project_id, PERM_EDIT):
            skipped += 1
            reasons.append(f"#{pid} 无修改权限")
            continue
        try:
            with _reconcile_guard(pay.project_id):
                reason = _do_reconcile(pay, allow_over=allow_over)
        except ReconcileBusy:
            # 整批回滚，保持"要么全成、要么全不成"的既有语义
            db.session.rollback()
            done, busy = 0, True
            break
        if reason:
            skipped += 1
            reasons.append(f"#{pid} {reason}")
            continue
        done += 1

    if busy:
        flash("批量对账未完成：有项目正在被另一笔对账操作占用，请稍后重试", "warning")
        return redirect(url_for("finance.reconcile"))

    if done:
        try:
            db.session.commit()
        except OperationalError as exc:
            # 行锁是在 commit 时释放的，死锁也可能在这一刻才抛出来
            db.session.rollback()
            if "deadlock" in str(exc).lower() or "lock wait timeout" in str(exc).lower():
                flash("批量对账未完成：有项目正在被另一笔对账操作占用，请稍后重试", "warning")
                return redirect(url_for("finance.reconcile"))
            raise
        log_action("对账", "收款", None, f"批量对账 {done} 笔收款")
    else:
        db.session.rollback()

    msg = f"已对账 {done} 笔"
    if skipped:
        msg += f"，{skipped} 笔未通过：" + "；".join(reasons[:3]) + ("…" if len(reasons) > 3 else "")
    flash(msg, "success" if not skipped else "warning")
    return redirect(url_for("finance.reconcile"))


@bp.route("/project/<int:pid>")
@login_required
def project_payments(pid: int):
    """单项目对账视图：直接跳项目详情的收款区，保持入口唯一"""
    db.get_or_404(Project, pid)
    return redirect(url_for("project.detail", pid=pid) + "#payments")
