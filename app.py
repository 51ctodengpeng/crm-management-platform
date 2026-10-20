# -*- coding: utf-8 -*-
"""程序入口：应用工厂 + 仪表盘 + 全局搜索 + CLI 命令"""
import os
from datetime import date, datetime, timedelta

import click
from flask import Flask, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user
from sqlalchemy import func, or_

from config import Config
from dbcompat import format_period
from extensions import csrf, db, login_manager
from logregion import resolve_log_regions
from models import Attachment, Customer, OperationLog, Payment, Project, User
from permissions import (
    PERM_VIEW,
    apply_customer_scope,
    apply_project_scope,
    can_create_data,
    can_delete_customer,
    can_delete_project,
    can_edit_customer,
    can_edit_project,
    can_view_customer,
    can_view_project,
    has_full_scope,
)
from utils import (
    level_color,
    parse_date,
    log_action,
    month_series,
    option_items,
    owner_options,
    shorten_filename,
    status_names_by_kind,
    todo_summary,
)


def create_app(config_object=Config) -> Flask:
    # 模板与静态资源显式指向资源目录：打包成 EXE 后代码与资源都在临时解压目录里，
    # 不能依赖 Flask 默认的「按模块位置推断」逻辑。
    app = Flask(
        __name__,
        template_folder=config_object.TEMPLATE_FOLDER,
        static_folder=config_object.STATIC_FOLDER,
    )
    app.config.from_object(config_object)
    config_object.ensure_dirs()

    # Nginx 反向代理后需要信任代理头，否则 https 会被识别成 http、真实 IP 丢成 127.0.0.1
    if app.config.get("BEHIND_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # 注册蓝图
    from routes.attachment import bp as attachment_bp
    from routes.backup import bp as backup_bp
    from routes.auth import bp as auth_bp
    from routes.customer import bp as customer_bp
    from routes.export import bp as export_bp
    from routes.finance import bp as finance_bp
    from routes.followup import bp as followup_bp
    from routes.imports import bp as imports_bp
    from routes.option import bp as option_bp
    from routes.perm import bp as perm_bp
    from routes.profile import bp as profile_bp
    from routes.project import bp as project_bp
    from routes.recycle import bp as recycle_bp
    from routes.settings import bp as settings_bp
    from routes.todo import bp as todo_bp
    from routes.user import bp as user_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(perm_bp, url_prefix="/permission")
    app.register_blueprint(profile_bp, url_prefix="/profile")
    app.register_blueprint(customer_bp, url_prefix="/customer")
    app.register_blueprint(project_bp, url_prefix="/project")
    app.register_blueprint(attachment_bp, url_prefix="/attachment")
    app.register_blueprint(finance_bp, url_prefix="/finance")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(option_bp, url_prefix="/options")
    app.register_blueprint(export_bp, url_prefix="/export")
    app.register_blueprint(followup_bp, url_prefix="/followup")
    app.register_blueprint(todo_bp, url_prefix="/todos")
    app.register_blueprint(backup_bp, url_prefix="/backup")
    app.register_blueprint(imports_bp, url_prefix="/import")
    app.register_blueprint(recycle_bp, url_prefix="/recycle")

    # ---------- 健康检查：给 Nginx / 运维脚本探活用，不暴露业务数据 ----------
    @app.route("/healthz")
    def healthz():
        from flask import jsonify
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception:                            # 数据库不可用时返回 503，便于反向代理摘流量
            # 异常细节只写日志：连接串、库名、账号等信息不能回给未认证访问者
            app.logger.exception("健康检查失败：数据库不可用")
            return jsonify({"status": "db-error"}), 503
        return jsonify({
            "status": "ok",
            "db": app.config.get("DB_BACKEND"),
            "version": app.config.get("APP_VERSION", ""),
        })

    # ---------- 站点图标：避免浏览器默认请求 /favicon.ico 打出 404 ----------
    @app.route("/favicon.ico")
    def favicon():
        from flask import send_from_directory
        ico = os.path.join(app.root_path, "build", "app.ico")
        if os.path.exists(ico):
            return send_from_directory(os.path.join(app.root_path, "build"), "app.ico")
        return "", 204

    # ---------- 自定义模板过滤器 ----------
    @app.template_filter("shortname")
    def _shortname(name, width=34):
        """长文件名缩略显示，保留扩展名"""
        return shorten_filename(name or "", width)

    @app.template_filter("money")
    def _money(value):
        return f"{float(value or 0):,.2f}"

    @app.template_filter("dt")
    def _dt(value, fmt="%Y-%m-%d %H:%M"):
        return value.strftime(fmt) if value else "-"

    # ---------- 全局模板变量 ----------
    def _pending_users() -> int:
        """待审批账号数：仅管理员需要看到"""
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            return 0
        return db.session.scalar(
            db.select(func.count(User.id)).where(User.status == "pending")
        ) or 0

    _OPT_KEY = {
        "customer_level": "CUSTOMER_LEVELS",
        "customer_business": "BUSINESS_TYPES",
        "customer_channel": "SOURCE_CHANNELS",
        "project_type": "PROJECT_TYPES",
        "project_status": "PROJECT_STATUSES",
        "service_mode": "SERVICE_MODES",
        "payment_method": "PAYMENT_METHODS",
        "pay_channel": "PAY_CHANNELS",
    }

    def _transfer_methods() -> list[str]:
        """需要流水号并参与转账对账的付款方式（后台可配置，默认「支付软件转账」）"""
        try:
            from utils import transfer_methods
            return transfer_methods()
        except Exception:
            return [app.config.get("TRANSFER_METHOD", "支付软件转账")]

    def _opt(category: str) -> list[str]:
        """读取后台维护的选项字典；异常时回落到 config 预设，保证页面不白屏"""
        try:
            from utils import option_items
            return option_items(category)
        except Exception:
            return list(app.config.get(_OPT_KEY.get(category, ""), []))

    def _owner_list() -> list[str]:
        """负责人候选：同一请求内只查一次"""
        from flask import g
        cached = getattr(g, "_owner_list", None)
        if cached is None:
            cached = owner_options()
            g._owner_list = cached
        return cached

    def _todo_badge() -> dict:
        """顶栏铃铛与侧边栏角标：未登录/异常时返回零值，绝不阻塞页面渲染"""
        try:
            if not current_user.is_authenticated:
                return {"total": 0, "follow_count": 0, "project_count": 0,
                        "ar_count": 0, "pending_pay": 0}
            from utils import todo_summary
            return todo_summary()
        except Exception:
            return {"total": 0, "follow_count": 0, "project_count": 0,
                    "ar_count": 0, "pending_pay": 0}

    @app.context_processor
    def inject_globals():
        # 邮件相关开关：模板里据此显示「忘记密码」「邮箱注册」等入口
        try:
            import mailer
            cfg_mail = mailer.load_config()
            email_register = mailer.feature_enabled("email_register")
            email_reset = mailer.feature_enabled("email_reset")
            email_auto = bool(cfg_mail.get("email_auto_activate"))
        except Exception:
            email_register = email_reset = email_auto = False

        # 站点名称 / 注册开关允许管理员在「系统设置」中改，未设置时回落 config
        try:
            from models import Setting
            site_name = Setting.get("site_name", app.config["APP_NAME"]) or app.config["APP_NAME"]
            allow_register = Setting.get_bool("allow_register", app.config.get("ALLOW_REGISTER", True))
            site_subtitle = Setting.get("site_subtitle", "")
            dashboard_notice = Setting.get("dashboard_notice", "")
        except Exception:
            site_name = app.config["APP_NAME"]
            allow_register = app.config.get("ALLOW_REGISTER", True)
            site_subtitle = ""
            dashboard_notice = ""

        return {
            "APP_NAME": site_name,
            "SITE_SUBTITLE": site_subtitle,
            "DASHBOARD_NOTICE": dashboard_notice,
            "now": datetime.now(),
            # 下拉选项统一读后台「选项管理」维护的字典（未初始化时自动回落 config 预设）
            "CUSTOMER_LEVELS": _opt("customer_level"),
            "BUSINESS_TYPES": _opt("customer_business"),
            "PROJECT_TYPES": _opt("project_type"),
            "PROJECT_STATUSES": _opt("project_status"),
            "SOURCE_CHANNELS": _opt("customer_channel"),
            "level_color": level_color,
            "TODO": _todo_badge(),
            "OWNERS": _owner_list(),
            "SERVICE_MODES": _opt("service_mode"),
            "PAYMENT_METHODS": _opt("payment_method"),
            "FOLLOWUP_METHODS": _opt("followup_method"),
            "ATTACH_CATEGORIES": _opt("attachment_category"),
            "TRANSFER_METHODS": _transfer_methods(),
            "PAY_CHANNELS": _opt("pay_channel"),
            "PAYMENT_STATUSES": app.config["PAYMENT_STATUSES"],
            "CUSTOM_CHANNEL_FLAG": app.config["CUSTOM_CHANNEL_FLAG"],
            "USER_STATUSES": app.config["USER_STATUSES"],
            "USER_ROLES": app.config["USER_ROLES"],
            "ALLOW_REGISTER": allow_register,
            "EMAIL_REGISTER": email_register,
            "EMAIL_RESET": email_reset,
            "EMAIL_AUTO": email_auto,
            "pending_users": _pending_users(),
            # ---------- 权限（模板中直接调用，避免各页面重复计算） ----------
            "DATA_SCOPES": app.config["DATA_SCOPES"],
            "PERMISSIONS": app.config["PERMISSIONS"],
            "PERMISSION_DESC": app.config["PERMISSION_DESC"],
            "can_view_project": can_view_project,
            "can_edit_project": can_edit_project,
            "can_delete_project": can_delete_project,
            "can_view_customer": can_view_customer,
            "can_edit_customer": can_edit_customer,
            "can_delete_customer": can_delete_customer,
            "can_create_data": can_create_data,
            "has_full_scope": has_full_scope,
        }

    # ---------- 首页 / 仪表盘 ----------
    @app.route("/")
    def index():
        # 未登录时直接渲染登录页（200），而不是 302 到 /login?next=%2F：
        # 打开域名就是登录界面，地址栏干净，输入后照样能回到首页。
        if not current_user.is_authenticated:
            return render_template("auth/login.html")
        # 所有统计都受数据范围约束：普通成员只看到自己有权看到的项目/客户
        total_customers = db.session.scalar(
            apply_customer_scope(db.select(func.count(Customer.id)))
        ) or 0
        total_projects = db.session.scalar(
            apply_project_scope(db.select(func.count(Project.id)), PERM_VIEW)
        ) or 0
        total_amount = db.session.scalar(
            apply_project_scope(db.select(func.sum(Project.project_amount)), PERM_VIEW)
        ) or 0
        # 「进行中」按选项的语义标记（running）统计，状态改名后依然准确
        running_statuses = status_names_by_kind("running") or ["进行中"]
        running = db.session.scalar(
            apply_project_scope(
                db.select(func.count(Project.id)).where(
                    Project.project_status.in_(running_statuses)
                ),
                PERM_VIEW,
            )
        ) or 0
        year = datetime.now().year
        year_amount = db.session.scalar(
            apply_project_scope(
                db.select(func.sum(Project.project_amount)).where(
                    format_period(Project.created_at, "%Y") == str(year)
                ),
                PERM_VIEW,
            )
        ) or 0
        total_attachments = db.session.scalar(
            apply_project_scope(
                db.select(func.count(Attachment.id)).join(
                    Project, Project.id == Attachment.project_id
                ),
                PERM_VIEW,
            )
        ) or 0

        # 项目状态分布：以选项字典的顺序为准，历史遗留值追加在末尾
        status_stats = {s: 0 for s in option_items("project_status")}
        for name, cnt in db.session.execute(
            apply_project_scope(
                db.select(Project.project_status, func.count(Project.id)).group_by(
                    Project.project_status
                ),
                PERM_VIEW,
            )
        ).all():
            status_stats[name or "未设置"] = cnt

        # 客户来源渠道分布（含自定义渠道）
        channel_rows = db.session.execute(
            apply_customer_scope(
                db.select(Customer.source_channel, func.count(Customer.id))
                .group_by(Customer.source_channel)
                .order_by(func.count(Customer.id).desc())
            )
        ).all()
        channel_stats = [(name or "未填写", cnt) for name, cnt in channel_rows]

        # 客户行业分布 Top5
        type_rows = db.session.execute(
            apply_customer_scope(
                db.select(Customer.business_type, func.count(Customer.id))
                .group_by(Customer.business_type)
                .order_by(func.count(Customer.id).desc())
                .limit(5)
            )
        ).all()

        recent_projects = db.session.scalars(
            apply_project_scope(
                db.select(Project).order_by(Project.created_at.desc()).limit(8), PERM_VIEW
            )
        ).all()

        # 操作日志：普通成员只看到自己的记录，管理员可见全部
        log_stmt = db.select(OperationLog)
        if not getattr(current_user, "is_admin", False):
            log_stmt = log_stmt.where(OperationLog.username == current_user.username)
        recent_logs = db.session.scalars(
            log_stmt.order_by(OperationLog.create_time.desc()).limit(8)
        ).all()

        # 管理员待办：待审批账号列表
        pending_list = []
        if getattr(current_user, "is_admin", False):
            pending_list = db.session.scalars(
                db.select(User).where(User.status == "pending").order_by(User.create_time.asc())
            ).all()

        # ---------- 资金与经营指标 ----------
        receivable_total = db.session.scalar(
            apply_project_scope(db.select(func.sum(Project.tax_included_amount)), PERM_VIEW)
        ) or 0
        matched_total = db.session.scalar(
            apply_project_scope(
                db.select(func.sum(Payment.amount)).join(
                    Project, Project.id == Payment.project_id
                ).where(Payment.status == "matched"),
                PERM_VIEW,
            )
        ) or 0
        todo = todo_summary(current_user)
        money_stats = {
            "receivable": round(receivable_total, 2),
            "matched": round(matched_total, 2),
            "unsettled": round(receivable_total - matched_total, 2),
            "overdue": round(todo["ar_amount"], 2),
            "overdue_count": todo["ar_count"],
        }

        # 近 6 个月签约与回款（签约优先取签订日期，缺失时回落到录入日期）
        months = month_series(6)
        sign_expr = format_period(
            func.coalesce(Project.sign_date, func.date(Project.created_at)), "%Y-%m"
        )
        sign_map = dict(db.session.execute(
            apply_project_scope(
                db.select(sign_expr, func.sum(Project.tax_included_amount))
                .where(sign_expr.in_(months)).group_by(sign_expr),
                PERM_VIEW,
            )
        ).all())
        pay_expr = format_period(Payment.paid_at, "%Y-%m")
        pay_map = dict(db.session.execute(
            apply_project_scope(
                db.select(pay_expr, func.sum(Payment.amount)).join(
                    Project, Project.id == Payment.project_id
                ).where(pay_expr.in_(months), Payment.status == "matched").group_by(pay_expr),
                PERM_VIEW,
            )
        ).all())
        trend = [
            {"month": m,
             "signed": round(sign_map.get(m) or 0, 2),
             "received": round(pay_map.get(m) or 0, 2)}
            for m in months
        ]
        trend_max = max([max(t["signed"], t["received"]) for t in trend] + [1])

        # 应收账龄：按「结束日期 + 账期」计算逾期天数分桶
        paid_map = dict(db.session.execute(
            apply_project_scope(
                db.select(Payment.project_id, func.sum(Payment.amount)).join(
                    Project, Project.id == Payment.project_id
                ).where(Payment.status == "matched").group_by(Payment.project_id),
                PERM_VIEW,
            )
        ).all())
        scoped_projects = db.session.scalars(
            apply_project_scope(
                db.select(Project).where(Project.tax_included_amount > 0), PERM_VIEW
            )
        ).all()
        today = date.today()
        bucket_defs = [("未到期", 0, 0), ("逾期 1-30 天", 1, 30),
                       ("逾期 31-60 天", 31, 60), ("逾期 61-90 天", 61, 90),
                       ("逾期 90 天以上", 91, 999999)]
        buckets = {label: {"amount": 0.0, "count": 0} for label, _, _ in bucket_defs}
        for p in scoped_projects:
            remain = (p.tax_included_amount or 0) - (paid_map.get(p.id) or 0)
            if remain <= 0.005:
                continue
            due = (p.end_date + timedelta(days=p.credit_days or 30)) if p.end_date else today
            days = (today - due).days
            if days <= 0:
                label = "未到期"
            elif days <= 30:
                label = "逾期 1-30 天"
            elif days <= 60:
                label = "逾期 31-60 天"
            elif days <= 90:
                label = "逾期 61-90 天"
            else:
                label = "逾期 90 天以上"
            buckets[label]["amount"] += remain
            buckets[label]["count"] += 1
        aging = [
            {"label": label, "amount": round(buckets[label]["amount"], 2),
             "count": buckets[label]["count"]}
            for label, _, _ in bucket_defs
        ]
        aging_max = max([a["amount"] for a in aging] + [1])

        # 负责人业绩（按签约额，含税口径）
        owner_rank = [
            {"owner": name or "未分配", "count": cnt, "amount": round(amount or 0, 2)}
            for name, cnt, amount in db.session.execute(
                apply_project_scope(
                    db.select(Project.owner, func.count(Project.id),
                              func.sum(Project.tax_included_amount))
                    .group_by(Project.owner)
                    .order_by(func.sum(Project.tax_included_amount).desc())
                    .limit(5),
                    PERM_VIEW,
                )
            ).all()
        ]

        return render_template(
            "index.html",
            money_stats=money_stats,
            trend=trend,
            trend_max=trend_max,
            aging=aging,
            aging_max=aging_max,
            owner_rank=owner_rank,
            stats={
                "customers": total_customers,
                "projects": total_projects,
                "amount": round(total_amount, 2),
                "running": running,
                "attachments": total_attachments,
                "accounts": db.session.scalar(db.select(func.count(User.id))) or 0,
                "year": year,
                "year_amount": round(year_amount, 2),
            },
            status_stats=status_stats,
            max_status=max(list(status_stats.values()) + [1]),
            channel_stats=channel_stats,
            max_channel=max([c for _, c in channel_stats] + [1]),
            type_rows=type_rows,
            max_type=max([c for _, c in type_rows] + [1]),
            recent_projects=recent_projects,
            recent_logs=recent_logs,
            pending_list=pending_list,
        )

    # ---------- 全局搜索 ----------
    @app.route("/search")
    @login_required
    def search():
        keyword = (request.args.get("q") or "").strip()
        customers, projects = [], []
        if keyword:
            like = f"%{keyword}%"
            customers = db.session.scalars(
                apply_customer_scope(
                    db.select(Customer).where(
                        or_(
                            Customer.company_name.like(like),
                            Customer.contact_name.like(like),
                            Customer.contact_phone.like(like),
                            Customer.wechat.like(like),
                            Customer.region.like(like),
                            Customer.address.like(like),
                            Customer.remark.like(like),
                        )
                    )
                ).order_by(Customer.updated_at.desc()).limit(30)
            ).all()
            projects = db.session.scalars(
                apply_project_scope(
                    db.select(Project).join(Customer, Project.customer_id == Customer.id).where(
                        or_(
                            Project.project_name.like(like),
                            Customer.company_name.like(like),
                            Project.project_summary.like(like),
                        )
                    ),
                    PERM_VIEW,
                ).order_by(Project.id.desc()).limit(30)
            ).all()

        return render_template(
            "search.html",
            keyword=keyword,
            customers=customers,
            projects=projects,
        )

    @app.route("/logs")
    @login_required
    def logs():
        page = request.args.get("page", 1, type=int)
        action = (request.args.get("action") or "").strip()
        keyword = (request.args.get("q") or "").strip()
        target_type = (request.args.get("target_type") or "").strip()
        target_id = request.args.get("target_id", type=int)
        date_from = parse_date(request.args.get("date_from"))
        date_to = parse_date(request.args.get("date_to"))
        operator = (request.args.get("operator") or "").strip()
        client = (request.args.get("client") or "").strip()

        stmt = db.select(OperationLog)
        # 普通成员只能查看自己的操作记录，管理员可审计全部
        if not getattr(current_user, "is_admin", False):
            stmt = stmt.where(OperationLog.username == current_user.username)
        if action:
            stmt = stmt.where(OperationLog.action == action)
        # 客户端大类是 client_type 的首段，用前缀匹配即可覆盖「大类 · 浏览器 · 系统」
        if client:
            stmt = stmt.where(OperationLog.client_type.like(f"{client}%"))
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    OperationLog.username.like(like),
                    OperationLog.detail.like(like),
                    OperationLog.target_type.like(like),
                )
            )
        # 按对象定位：详情页「查看全部」会带上 target_type + target_id
        if target_type:
            stmt = stmt.where(OperationLog.target_type == target_type)
        if target_id:
            stmt = stmt.where(OperationLog.target_id == target_id)
        # 时间区间：结束日期含当天
        if date_from:
            stmt = stmt.where(OperationLog.create_time >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            stmt = stmt.where(OperationLog.create_time <= datetime.combine(date_to, datetime.max.time()))
        if operator:
            stmt = stmt.where(OperationLog.username == operator)
        stmt = stmt.order_by(OperationLog.create_time.desc())
        pagination = db.paginate(stmt, page=page, per_page=20, error_out=False)

        actions_stmt = db.select(OperationLog.action).distinct()
        if not getattr(current_user, "is_admin", False):
            actions_stmt = actions_stmt.where(OperationLog.username == current_user.username)
        actions = [a for (a,) in db.session.execute(actions_stmt).all() if a]

        types_stmt = db.select(OperationLog.target_type).distinct()
        if not getattr(current_user, "is_admin", False):
            types_stmt = types_stmt.where(OperationLog.username == current_user.username)
        target_types = [t for (t,) in db.session.execute(types_stmt).all() if t]

        operators = []
        if getattr(current_user, "is_admin", False):
            operators = [u for (u,) in db.session.execute(
                db.select(OperationLog.username).distinct()
            ).all() if u]

        # 客户端类型下拉：从已有记录里取出出现过的大类（固定顺序，历史空值不计入）
        from clientinfo import KINDS, client_kind_of
        clients_stmt = db.select(OperationLog.client_type).distinct()
        if not getattr(current_user, "is_admin", False):
            clients_stmt = clients_stmt.where(OperationLog.username == current_user.username)
        seen_kinds = {client_kind_of(ct) for (ct,) in db.session.execute(clients_stmt).all()}
        client_kinds = [k for k in KINDS if k in seen_kinds]

        # 翻页链接要带上当前全部筛选条件，否则第二页就串到默认视图
        filter_args = {k: v for k, v in {
            "q": keyword, "action": action, "target_type": target_type,
            "client": client, "operator": operator, "target_id": target_id,
            "date_from": request.args.get("date_from") or "",
            "date_to": request.args.get("date_to") or "",
        }.items() if v}

        # 客户归属地：日志只存对象 ID，省 / 市实时从客户主档批量关联（不回填日志表）
        try:
            log_regions = resolve_log_regions(pagination.items)
        except Exception:                 # 归属地只是展示增强，取不到不能连带日志页 500
            db.session.rollback()
            current_app.logger.warning("关联日志客户地区失败", exc_info=True)
            log_regions = {}

        return render_template(
            "logs.html", pagination=pagination, action=action, keyword=keyword, actions=actions,
            target_type=target_type, target_id=target_id, target_types=target_types,
            date_from=request.args.get("date_from") or "",
            date_to=request.args.get("date_to") or "",
            operator=operator, operators=operators,
            client=client, client_kinds=client_kinds, filter_args=filter_args,
            log_regions=log_regions,
        )

    # ---------- 会话安全：空闲超时 + 安全响应头 ----------
    @app.before_request
    def _enforce_idle_timeout():
        """超过空闲时长未操作则强制退出，避免共用电脑时被继续使用

        只对已登录的业务请求生效；登录页、静态资源、健康检查不受影响。
        使用 session 里的 last_seen 时间戳（滑动窗口），每次请求刷新。
        """
        if request.endpoint in ("auth.login", "static", "favicon"):
            return None
        if not current_user.is_authenticated:
            return None
        idle_minutes = app.config.get("SESSION_IDLE_MINUTES", 120) or 0
        if idle_minutes <= 0:
            return None
        now_ts = datetime.now().timestamp()
        last_seen = session.get("last_seen")
        if last_seen and now_ts - float(last_seen) > idle_minutes * 60:
            username = current_user.username
            logout_user()
            session.clear()
            flash(f"已超过 {idle_minutes} 分钟未操作，为保证安全需要重新登录", "warning")
            log_action("退出", "系统", None, f"用户 {username} 因空闲超时自动退出")
            return redirect(url_for("auth.login"))
        session["last_seen"] = now_ts
        session.permanent = True
        return None

    @app.after_request
    def _security_headers(resp):
        """基础安全响应头：防 MIME 嗅探、防点击劫持、限制来源信息"""
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        # 页面内含大量内联脚本，这里只声明框架来源与表单目标，避免误伤功能
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        return resp

    # ---------- 错误页 ----------
    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, msg="页面不存在或已被删除"), 404

    @app.errorhandler(413)
    def too_large(e):
        limit = app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
        return render_template("error.html", code=413, msg=f"上传文件超过 {limit}MB 限制"), 413

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template("error.html", code=500, msg="服务器内部错误"), 500

    # ---------- CLI：初始化数据库 / 建管理员 ----------
    @app.cli.command("init-db")
    def init_db():
        """初始化数据库表结构"""
        db.create_all()
        click.echo(f"[OK] 数据库已初始化 -> {app.config['DB_PATH']}")

    @app.cli.command("create-admin")
    @click.argument("username", default="admin")
    @click.argument("password", default="")
    def create_admin(username, password):
        """创建管理员账号：flask create-admin [用户名] [密码]

        不传密码时随机生成 12 位口令并打印一次，避免出现全网通用的固定初始口令。
        """
        db.create_all()
        exists = db.session.scalar(db.select(User).where(User.username == username))
        if exists:
            exists.role, exists.status = "admin", "active"
            db.session.commit()
            click.echo(f"[OK] 用户 {username} 已存在，已提升为管理员并启用")
            return
        generated = False
        if not password:
            import secrets
            import string

            alphabet = string.ascii_letters + string.digits
            password = "".join(secrets.choice(alphabet) for _ in range(12))
            generated = True
        u = User(username=username, role="admin", status="active", real_name=username)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"[OK] 管理员已创建：{username} / {password}")
        if generated:
            click.echo("[!] 以上口令为随机生成，仅显示这一次，请立即登录并修改。")

    return app


app = create_app()

# 首次运行自举：建表 / 补列 / 建默认管理员。所有入口（含打包后的 EXE）都靠它做到开箱即用。
from bootstrap import bootstrap as _bootstrap  # noqa: E402  (必须在 app 创建后导入)

BOOTSTRAP_RESULT = _bootstrap(app)

if __name__ == "__main__":
    port = int(os.environ.get("CRM_PORT", 8081))
    if BOOTSTRAP_RESULT.get("created_admin"):
        print("=" * 58)
        print("  首次运行，已自动创建管理员账号：")
        print(f"      账号 {BOOTSTRAP_RESULT['username']}   密码 {BOOTSTRAP_RESULT['password']}")
        print("  该密码仅显示这一次，请登录后立即修改。")
        print("=" * 58)
    # 调试器只在显式声明非生产环境时才允许打开：Werkzeug 调试控制台可在服务器上执行任意代码，
    # 生产环境（CRM_ENV=prod，部署脚本已写入）即使误设 CRM_DEBUG=1 也不会暴露。
    debug = os.environ.get("CRM_DEBUG", "0") == "1" and os.environ.get("CRM_ENV") != "prod"
    print(f"  {app.config['APP_NAME']} 已启动 -> http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
