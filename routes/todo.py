# -*- coding: utf-8 -*-
"""路由层：待办提醒中心

数据不额外落表，全部由业务数据实时推导（跟进计划、项目结束日、应收账期、待对账），
避免"提醒表和业务数据两套账"。顶栏铃铛、经营概览与待办页共用同一份 todo_summary。
"""
from html import escape

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

import mailer
from utils import log_action, option_default, option_items, todo_summary

bp = Blueprint("todo", __name__)


@bp.route("/")
@login_required
def index():
    data = todo_summary()
    return render_template(
        "todo/index.html",
        todo=data,
        methods=option_items("followup_method"),
        default_method=option_default("followup_method") or "电话",
    )


@bp.route("/notify", methods=["POST"])
@login_required
def notify():
    """把当前待办汇总发到本人邮箱（需先在系统设置里启用 SMTP）"""
    data = todo_summary()
    if data["total"] == 0 and not data["pending_pay"]:
        flash("当前没有待办事项，无需发送提醒", "info")
        return redirect(url_for("todo.index"))
    if not (current_user.email or "").strip():
        flash("你的账号还没有填写邮箱，请先在「个人资料」中补充", "warning")
        return redirect(url_for("profile.edit"))

    items = []
    for c in data["follow_rows"][:10]:
        items.append(f"跟进到期：{c.company_name}（计划 {c.next_follow_at}）")
    for p in data["project_rows"][:10]:
        items.append(f"项目到期：{p.project_name}（{p.end_date}，{p.project_status}）")
    for p, due, days in data["ar_rows"][:10]:
        items.append(f"应收逾期：{p.project_name}（应回款日 {due}，逾期 {days} 天）")
    if data["pending_pay"]:
        items.append(f"待对账收款：{data['pending_pay']} 笔")

    # 客户名 / 项目名由用户自由填写，拼进 HTML 邮件前必须转义，
    # 否则一个带尖括号的项目名就能把通知邮件排版搞乱、甚至塞进钓鱼链接。
    body = "<ul>" + "".join(f"<li>{escape(line)}</li>" for line in items) + "</ul>"
    body += (f"<p>合计：跟进 {data['follow_count']} 项、项目到期 {data['project_count']} 项、"
             f"应收逾期 {data['ar_count']} 项（未收 ¥{data['ar_amount']:,.2f}）、"
             f"待对账 {data['pending_pay']} 笔。</p>")
    ok, msg = mailer.send_mail(
        current_user.email,
        "CRM 待办提醒",
        mailer._wrap("今日待办提醒", body, "本邮件由系统按当前数据实时生成，处理后再次发送即更新。"),
    )
    log_action("通知", "系统", None, f"发送待办提醒邮件到 {current_user.email}")
    flash(("提醒邮件已发送：" if ok else "发送失败：") + msg, "success" if ok else "danger")
    return redirect(url_for("todo.index"))
