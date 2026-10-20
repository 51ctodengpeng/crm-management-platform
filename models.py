# -*- coding: utf-8 -*-
"""数据库模型：User / Customer / Project / Attachment / OperationLog"""
from datetime import date, datetime, timedelta

from flask_login import UserMixin
from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db

# ============================== 金额与税点 ==============================
DEFAULT_TAX_RATE = 6.0          # 默认税点 6%
MAX_TAX_RATE = 100.0            # 税点上限（百分比），防止误填


def money_round(value) -> float:
    """金额四舍五入到分，使用 Decimal 避免二进制浮点误差（ROUND_HALF_UP）"""
    from decimal import Decimal, ROUND_HALF_UP
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def calc_gross(net, rate) -> float:
    """不含税金额 + 税点 -> 含税价格：含税 = 不含税 × (1 + 税点/100)"""
    return money_round((net or 0) * (1 + (rate or 0) / 100))


def calc_net(gross, rate) -> float:
    """含税价格 + 税点 -> 不含税金额：不含税 = 含税 ÷ (1 + 税点/100)"""
    divisor = 1 + (rate or 0) / 100
    if not divisor:
        return 0.0
    return money_round((gross or 0) / divisor)


def normalize_tax_rate(value) -> float:
    """税点归一化：空值取默认 6%，限制在 0~100，保留 2 位小数"""
    try:
        rate = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return DEFAULT_TAX_RATE
    if rate != rate:                       # NaN
        return DEFAULT_TAX_RATE
    return round(max(0.0, min(MAX_TAX_RATE, rate)), 2)


class User(UserMixin, db.Model):
    """账号表：支持管理员 / 普通用户，注册后需管理员审批才能启用"""
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    real_name = db.Column(db.String(64))
    email = db.Column(db.String(128))
    phone = db.Column(db.String(32))
    department = db.Column(db.String(64))
    wechat = db.Column(db.String(64))                      # 微信号
    avatar = db.Column(db.String(255))                      # 头像文件名（文件在 DATA_DIR/avatars）
    role = db.Column(db.String(16), default="user")        # admin / user
    status = db.Column(db.String(16), default="pending", index=True)  # pending/active/disabled/rejected
    # ---------- 权限：数据范围与全局操作 ----------
    # data_scope：all=全部数据（等同全局可见）；assigned=仅被授权的项目
    data_scope = db.Column(db.String(16), default="assigned", index=True)
    # can_create：是否允许新增客户 / 项目（项目级授权只作用于已有数据）
    can_create = db.Column(db.Boolean, default=False)
    apply_reason = db.Column(db.Text)                      # 注册时填写的申请说明
    review_remark = db.Column(db.Text)                     # 管理员审批备注
    approved_by = db.Column(db.String(64))
    approve_time = db.Column(db.DateTime)
    email_verified = db.Column(db.Boolean, default=False)   # 邮箱是否已验证（邮箱注册 / 找回密码依赖）
    create_time = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)
    # ---------- 登录防护 ----------
    failed_count = db.Column(db.Integer, default=0)        # 连续密码错误次数
    locked_until = db.Column(db.DateTime)                  # 锁定截止时间，为空表示未锁定

    # ---------- 密码 ----------
    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    # ---------- 状态 ----------
    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active_account(self) -> bool:
        """账号是否已通过审批并处于启用状态"""
        return self.status == "active"

    @property
    def is_active(self) -> bool:
        """Flask-Login 会检查该属性：未审批 / 被禁用的账号不允许建立会话"""
        return self.status == "active"

    @property
    def status_label(self) -> str:
        from config import Config
        return Config.USER_STATUSES.get(self.status, self.status or "未知")

    @property
    def role_label(self) -> str:
        from config import Config
        return Config.USER_ROLES.get(self.role, self.role or "普通用户")

    @property
    def status_color(self) -> str:
        return {
            "pending": "orange",
            "active": "green",
            "disabled": "gray",
            "rejected": "red",
        }.get(self.status, "gray")

    # ---------- 登录防护 ----------
    @property
    def is_locked(self) -> bool:
        """是否处于登录锁定期（连续密码错误触发的临时锁定）"""
        return bool(self.locked_until and self.locked_until > datetime.now())

    @property
    def lock_remain_minutes(self) -> int:
        """锁定剩余分钟数（向上取整，至少 1 分钟）"""
        if not self.is_locked:
            return 0
        seconds = (self.locked_until - datetime.now()).total_seconds()
        return max(1, int(seconds // 60) + (1 if seconds % 60 else 0))

    def register_failure(self, limit: int, minutes: int) -> bool:
        """记录一次密码错误；达到阈值则锁定并返回 True"""
        self.failed_count = (self.failed_count or 0) + 1
        if limit and self.failed_count >= limit:
            self.locked_until = datetime.now() + timedelta(minutes=minutes or 15)
            self.failed_count = 0
            return True
        return False

    def reset_failures(self):
        """登录成功或管理员解锁时清空失败计数与锁定期"""
        self.failed_count = 0
        self.locked_until = None

    # ---------- 权限：数据范围 ----------
    @property
    def scope_label(self) -> str:
        from config import Config
        if self.is_admin:
            return "全部数据"
        return Config.DATA_SCOPES.get(self.data_scope or "assigned", "仅授权项目")

    @property
    def has_full_scope(self) -> bool:
        """是否可访问全部数据：管理员恒为真；普通成员取决于 data_scope"""
        return self.role == "admin" or (self.data_scope or "assigned") == "all"

    @property
    def can_create_data(self) -> bool:
        """是否允许新增客户 / 项目：管理员恒为真"""
        return self.role == "admin" or bool(self.can_create)

    @property
    def display_name(self) -> str:
        return self.real_name or self.username

    @property
    def initial(self) -> str:
        return (self.display_name or "?")[0].upper()

    @property
    def avatar_url(self) -> str | None:
        """头像访问地址；未设置头像时返回 None，由模板回退到首字母"""
        if not self.avatar:
            return None
        try:
            from flask import url_for
            return url_for("profile.avatar_file", filename=self.avatar)
        except Exception:  # pragma: no cover - 非请求上下文
            return None

    def to_dict(self):
        return {
            "账号": self.username,
            "姓名": self.real_name or "",
            "角色": self.role_label,
            "状态": self.status_label,
            "邮箱": self.email or "",
            "手机": self.phone or "",
            "微信号": self.wechat or "",
            "部门": self.department or "",
            "申请说明": self.apply_reason or "",
            "审批人": self.approved_by or "",
            "审批备注": self.review_remark or "",
            "注册时间": self.create_time.strftime("%Y-%m-%d %H:%M") if self.create_time else "",
            "最后登录": self.last_login.strftime("%Y-%m-%d %H:%M") if self.last_login else "从未登录",
        }

    def __repr__(self):
        return f"<User {self.username} {self.role}/{self.status}>"


class Customer(db.Model):
    """客户主表"""
    __tablename__ = "customer"

    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(128), nullable=False, index=True)
    contact_name = db.Column(db.String(64))
    position = db.Column(db.String(32))               # 对接人职位
    contact_phone = db.Column(db.String(32))
    contact_email = db.Column(db.String(128))
    wechat = db.Column(db.String(64))                 # 微信号
    region = db.Column(db.String(64))                 # 所在地区（省 / 市 / 区拼接文本，展示与搜索沿用）
    # 省 / 市 / 区三级结构化：由表单的三级联动下拉落库，便于按层级统计与筛选。
    # region 仍是唯一的展示口径（列表、搜索、导出、查重都用它），这三列是它的拆分视图；
    # 历史记录与「手动填写」记录均为 NULL，读取时统一回落到 region。
    province = db.Column(db.String(32), index=True)   # 省级，如「江苏省」
    city = db.Column(db.String(32))                   # 市级，如「苏州市」
    district = db.Column(db.String(32))               # 区县级，如「吴中区」
    address = db.Column(db.Text)                      # 详细地址
    business_type = db.Column(db.String(32))
    customer_level = db.Column(db.String(16), default="普通")
    source_channel = db.Column(db.String(32), index=True)  # 来源渠道（预设或自定义）
    remark = db.Column(db.Text)
    # 跟进汇总（由 FollowUp 变更时维护，便于列表展示与到期提醒，避免逐行子查询）
    last_follow_at = db.Column(db.Date, index=True)     # 最近一次跟进日期
    next_follow_at = db.Column(db.Date, index=True)     # 下一次计划跟进日期
    owner = db.Column(db.String(64), index=True)         # 客户负责人（销售归属）
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    projects = db.relationship(
        "Project",
        backref="customer",
        cascade="all, delete-orphan",
        passive_deletes=False,
        order_by="Project.id.desc()",
    )

    @property
    def project_count(self) -> int:
        return len(self.projects)

    @property
    def region_levels(self) -> list[str]:
        """省 / 市 / 区三段（自动去空、去重）。

        历史客户与「手动填写地区」的客户没有结构化值，返回空列表——
        调用方应回落到 region 文本，不要把空列表当成「未填写地区」。
        """
        parts: list[str] = []
        for value in (self.province, self.city, self.district):
            name = (value or "").strip()
            if name and name not in parts:
                parts.append(name)
        return parts

    @property
    def region_structured(self) -> str:
        """结构化地区文本；没有三级数据时退回 region，保证展示口径不变"""
        return " ".join(self.region_levels) or (self.region or "")

    @property
    def total_amount(self) -> float:
        return round(sum(p.project_amount or 0 for p in self.projects), 2)

    contacts = db.relationship(
        "Contact",
        back_populates="customer",
        cascade="all, delete-orphan",
        order_by="Contact.is_primary.desc(), Contact.sort_order, Contact.id",
    )

    follow_ups = db.relationship(
        "FollowUp",
        back_populates="customer",
        cascade="all, delete-orphan",
        order_by="FollowUp.followed_at.desc(), FollowUp.id.desc()",
    )

    @property
    def full_address(self) -> str:
        return " ".join(x for x in [self.region, self.address] if x)

    @property
    def days_since_follow(self) -> int | None:
        """距上次跟进的天数；从未跟进返回 None"""
        if not self.last_follow_at:
            return None
        return (date.today() - self.last_follow_at).days

    @property
    def follow_state(self) -> str:
        """跟进状态文案：逾期 / 今日 / 正常 / 未跟进"""
        if not self.next_follow_at:
            return "已跟进" if self.last_follow_at else "未跟进"
        delta = (self.next_follow_at - date.today()).days
        if delta < 0:
            return f"逾期 {abs(delta)} 天"
        if delta == 0:
            return "今日跟进"
        return f"{delta} 天后"

    def to_dict(self):
        return {
            "客户公司": self.company_name,
            "对接人": self.contact_name or "",
            "职位": self.position or "",
            "联系电话": self.contact_phone or "",
            "微信号": self.wechat or "",
            "邮箱": self.contact_email or "",
            "所在地区": self.region or "",
            "详细地址": self.address or "",
            "行业类型": self.business_type or "",
            "客户等级": self.customer_level or "",
            "来源渠道": self.source_channel or "",
            "负责人": self.owner or "",
            "最近跟进": self.last_follow_at.strftime("%Y-%m-%d") if self.last_follow_at else "",
            "下次跟进": self.next_follow_at.strftime("%Y-%m-%d") if self.next_follow_at else "",
            "项目数量": self.project_count,
            "累计合同金额": self.total_amount,
            "客户备注": self.remark or "",
            "录入时间": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "最后修改": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else "",
        }

    def __repr__(self):
        return f"<Customer {self.company_name}>"


class Project(db.Model):
    """项目记录表：一个客户对应多个项目"""
    __tablename__ = "project"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_name = db.Column(db.String(128), nullable=False, index=True)
    project_type = db.Column(db.String(32))
    project_amount = db.Column(db.Float, default=0.0)          # 不含税合同金额
    tax_rate = db.Column(db.Float, default=DEFAULT_TAX_RATE)    # 税点（%）
    tax_included_amount = db.Column(db.Float, default=0.0)      # 含税价格
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    project_status = db.Column(db.String(16), default="待实施")
    service_mode = db.Column(db.String(16), default="远程服务", index=True)   # 服务方式
    payment_method = db.Column(db.String(16), default="支付软件转账", index=True)  # 约定付款方式
    owner = db.Column(db.String(64), index=True)               # 项目负责人（销售归属）
    # ---------- 合同要素 ----------
    contract_no = db.Column(db.String(64), index=True)          # 合同编号
    sign_date = db.Column(db.Date)                              # 签订日期
    payment_terms = db.Column(db.Text)                          # 付款条款（预付/进度/尾款约定）
    credit_days = db.Column(db.Integer, default=30)             # 账期（天），用于应收逾期判定
    project_summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    attachments = db.relationship(
        "Attachment",
        backref="project",
        cascade="all, delete-orphan",
        passive_deletes=False,
        order_by="Attachment.id.desc()",
    )
    payments = db.relationship(
        "Payment",
        backref="project",
        cascade="all, delete-orphan",
        passive_deletes=False,
        order_by="Payment.id.desc()",
    )
    follow_ups = db.relationship(
        "FollowUp",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="FollowUp.followed_at.desc(), FollowUp.id.desc()",
    )
    payment_plans = db.relationship(
        "PaymentPlan",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PaymentPlan.due_date, PaymentPlan.seq",
    )

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    @property
    def attachment_total_size(self) -> int:
        return sum(a.file_size or 0 for a in self.attachments)

    @property
    def attachment_total_human(self) -> str:
        return human_size(self.attachment_total_size)

    @property
    def status_color(self) -> str:
        """状态徽标颜色按选项的语义标记取，改名后不会退化成灰色"""
        from utils import status_color
        return status_color(self.project_status)

    # ---------- 金额与税点 ----------
    @property
    def plan_amount(self) -> float:
        """回款计划合计（含税口径）"""
        return money_round(sum(p.amount or 0 for p in self.payment_plans))

    @property
    def plan_gap(self) -> float:
        """计划合计与应收的差额：正数表示计划超额，负数表示尚未排满"""
        return money_round(self.plan_amount - self.receivable)

    @property
    def tax_amount(self) -> float:
        """税额 = 含税价格 - 不含税金额"""
        return money_round((self.tax_included_amount or 0) - (self.project_amount or 0))

    @property
    def tax_rate_label(self) -> str:
        rate = self.tax_rate if self.tax_rate is not None else DEFAULT_TAX_RATE
        return f"{rate:g}%"

    def apply_net(self, net, rate=None):
        """以不含税金额为准，正算含税价格"""
        self.tax_rate = normalize_tax_rate(rate if rate is not None else self.tax_rate)
        self.project_amount = money_round(net or 0)
        self.tax_included_amount = calc_gross(self.project_amount, self.tax_rate)

    def apply_gross(self, gross, rate=None):
        """以含税价格为准，反算不含税金额"""
        self.tax_rate = normalize_tax_rate(rate if rate is not None else self.tax_rate)
        self.tax_included_amount = money_round(gross or 0)
        self.project_amount = calc_net(self.tax_included_amount, self.tax_rate)

    def recalc_tax(self):
        """税点变化后，按当前含税价重新推导不含税金额（保持对外报价不变）"""
        self.tax_included_amount = money_round(self.tax_included_amount or 0)
        self.project_amount = calc_net(self.tax_included_amount, self.tax_rate)

    def to_dict(self):
        return {
            "项目名称": self.project_name,
            "客户公司": self.customer.company_name if self.customer else "",
            "项目类型": self.project_type or "",
            "合同金额": self.project_amount or 0,
            "税点(%)": self.tax_rate if self.tax_rate is not None else DEFAULT_TAX_RATE,
            "含税金额": self.tax_included_amount or 0,
            "税额": self.tax_amount,
            "启动日期": self.start_date.strftime("%Y-%m-%d") if self.start_date else "",
            "结束日期": self.end_date.strftime("%Y-%m-%d") if self.end_date else "",
            "项目状态": self.project_status or "",
            "服务方式": self.service_mode or "",
            "付款方式": self.payment_method or "",
            "已对账金额": self.matched_amount,
            "未对账金额": self.pending_amount,
            "待收差额": self.unsettled_amount,
            "附件数量": self.attachment_count,
            "项目描述": self.project_summary or "",
            "创建时间": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
        }

    # ---------- 收款与对账 ----------
    @property
    def receivable(self) -> float:
        """应收金额：按含税价格口径（对外报价就是含税价）"""
        return money_round(self.tax_included_amount or 0)

    @property
    def matched_amount(self) -> float:
        """已对账金额：只有对账通过的收款才计入，避免「填了就算收到」"""
        return money_round(sum(p.amount or 0 for p in self.payments if p.status == "matched"))

    @property
    def pending_amount(self) -> float:
        """待对账金额：已登记但还没核对流水的收款"""
        return money_round(sum(p.amount or 0 for p in self.payments if p.status == "pending"))

    @property
    def unsettled_amount(self) -> float:
        """差额 = 应收 - 已对账（正数为待收，负数为超收）"""
        return money_round(self.receivable - self.matched_amount)

    @property
    def settle_label(self) -> str:
        if not self.receivable:
            return "未报价"
        if self.matched_amount <= 0:
            return "未收款"
        if self.unsettled_amount > 0.005:
            return "部分收款"
        if self.unsettled_amount < -0.005:
            return "超收"
        return "已结清"

    @property
    def settle_color(self) -> str:
        return {"未收款": "gray", "部分收款": "orange", "已结清": "green",
                "超收": "red", "未报价": "gray"}.get(self.settle_label, "gray")

    def __repr__(self):
        return f"<Project {self.project_name}>"


class Payment(db.Model):
    """收款记录表：一条记录 = 一笔实际到账，转账需流水号并走对账流程

    对账口径（与 Project.receivable 配合）：
      应收 = 项目含税价格；已收 = 本项目 status='matched' 的金额之和；
      差额 = 应收 - 已收。只有对账通过的记录才计入已收，登记但未对账的停留在「待对账」。
    """
    __tablename__ = "payment"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id = db.Column(
        db.Integer, db.ForeignKey("payment_plan.id", ondelete="SET NULL"), index=True
    )                                                     # 关联的回款计划期次（可空）
    amount = db.Column(db.Float, default=0.0)                 # 本笔到账金额（含税口径）
    pay_method = db.Column(db.String(16), default="支付软件转账", index=True)  # 付款方式
    pay_channel = db.Column(db.String(32))                    # 微信 / 支付宝 / 银行转账…
    trade_no = db.Column(db.String(64), index=True)           # 交易流水号（转账必填、唯一）
    payer = db.Column(db.String(64))                          # 付款方名称
    paid_at = db.Column(db.Date)                              # 到账日期
    status = db.Column(db.String(16), default="pending", index=True)  # pending / matched
    transfer_flag = db.Column(db.Boolean, default=False, nullable=False)  # 是否属于需流水号的转账方式
    reconciled_at = db.Column(db.DateTime)                    # 对账时间
    reconciled_by = db.Column(db.String(64))                  # 对账人
    remark = db.Column(db.String(255))
    created_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    plan = db.relationship("PaymentPlan", back_populates="payments")

    @property
    def is_transfer(self) -> bool:
        """是否需要流水号 / 参与转账对账。

        用登记时落库的标记判断，而不是比对付款方式名称——付款方式已改成后台可
        增删改的字典项，改名后按名称判断会失效。
        """
        return bool(self.transfer_flag)

    @property
    def status_label(self) -> str:
        return {"pending": "待对账", "matched": "已对账"}.get(self.status, "待对账")

    @property
    def status_color(self) -> str:
        return "green" if self.status == "matched" else "orange"

    @property
    def method_label(self) -> str:
        if self.is_transfer:
            return f"转账·{self.pay_channel}" if self.pay_channel else (self.pay_method or "转账")
        return self.pay_method or "现金"

    @property
    def method_color(self) -> str:
        return "blue" if self.is_transfer else "green"

    def to_dict(self):
        return {
            "项目名称": self.project.project_name if self.project else "",
            "客户公司": self.project.customer.company_name if self.project and self.project.customer else "",
            "收款金额": self.amount or 0,
            "付款方式": self.pay_method or "",
            "转账渠道": self.pay_channel or "",
            "交易流水号": self.trade_no or "",
            "付款方": self.payer or "",
            "到账日期": self.paid_at.strftime("%Y-%m-%d") if self.paid_at else "",
            "对账状态": self.status_label,
            "对账人": self.reconciled_by or "",
            "对账时间": self.reconciled_at.strftime("%Y-%m-%d %H:%M") if self.reconciled_at else "",
            "备注": self.remark or "",
            "登记人": self.created_by or "",
            "登记时间": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
        }

    def __repr__(self):
        return f"<Payment p{self.project_id} {self.amount} {self.status}>"


class PaymentPlan(db.Model):
    """回款计划：一个项目可分多期，登记收款时可关联到具体期次

    计划金额为含税口径，与「应收 = 项目含税价」保持同一坐标系。
    只有已对账（matched）的收款才计入期次已收，避免"登记了就算回款"。
    """
    __tablename__ = "payment_plan"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq = db.Column(db.Integer, default=1)                 # 期次序号
    due_date = db.Column(db.Date, index=True)              # 约定回款日期
    ratio = db.Column(db.Float)                            # 占比（%），可空
    amount = db.Column(db.Float, default=0.0)              # 计划金额（含税）
    note = db.Column(db.String(120))                       # 说明：预付款 / 验收款 / 尾款
    created_at = db.Column(db.DateTime, default=datetime.now)

    project = db.relationship("Project", back_populates="payment_plans")
    payments = db.relationship("Payment", back_populates="plan")

    @property
    def paid_amount(self) -> float:
        return money_round(sum(p.amount or 0 for p in self.payments if p.status == "matched"))

    @property
    def remain_amount(self) -> float:
        return money_round((self.amount or 0) - self.paid_amount)

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_date and self.due_date < date.today()
                    and self.remain_amount > 0.005)

    @property
    def status_label(self) -> str:
        if self.remain_amount <= 0.005:
            return "已收齐"
        if self.paid_amount > 0:
            return "部分收款"
        if self.is_overdue:
            return "已逾期"
        return "待收款"

    @property
    def status_color(self) -> str:
        return {"已收齐": "green", "部分收款": "orange",
                "已逾期": "red", "待收款": "gray"}[self.status_label]

    def to_dict(self):
        return {
            "项目名称": self.project.project_name if self.project else "",
            "期次": f"第 {self.seq} 期",
            "约定日期": self.due_date.strftime("%Y-%m-%d") if self.due_date else "",
            "计划金额": self.amount or 0,
            "已收金额": self.paid_amount,
            "未收金额": self.remain_amount,
            "状态": self.status_label,
            "说明": self.note or "",
        }



class Contact(db.Model):
    """客户联系人（一个客户可有多个对接人，其中一个为主联系人）

    主联系人的姓名/电话/邮箱/微信会同步回 Customer 上的同名字段，
    这样列表展示、搜索、导出、查重都不必改造成联表查询，兼容历史数据。
    """
    __tablename__ = "customer_contact"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(64), nullable=False)
    position = db.Column(db.String(32))
    phone = db.Column(db.String(32))
    email = db.Column(db.String(128))
    wechat = db.Column(db.String(64))
    remark = db.Column(db.String(255))
    is_primary = db.Column(db.Boolean, default=False, index=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship("Customer", back_populates="contacts")

    @property
    def initial(self) -> str:
        return (self.name or "?")[0].upper()

    def __repr__(self):
        return f"<Contact {self.name} of c{self.customer_id}>"


def sync_primary_contact(customer: Customer) -> None:
    """把主联系人（没有主联系人时取第一条）回写到客户主档字段

    这样客户列表的「对接人」列、关键字搜索、Excel 导出都能直接沿用旧字段，
    不会因为引入联系人子表而出现两套口径。
    """
    primary = None
    for row in customer.contacts:
        if row.is_primary:
            primary = row
            break
    if primary is None and customer.contacts:
        primary = customer.contacts[0]
        primary.is_primary = True
    customer.contact_name = primary.name if primary else ""
    customer.position = primary.position if primary else ""
    customer.contact_phone = primary.phone if primary else ""
    customer.contact_email = primary.email if primary else ""
    customer.wechat = primary.wechat if primary else ""


class FollowUp(db.Model):
    """客户跟进记录：一次沟通 = 一条记录，可挂在客户下，也可指定具体项目"""
    __tablename__ = "follow_up"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = db.Column(
        db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    method = db.Column(db.String(16), default="电话")        # 电话 / 微信 / 上门 / 邮件
    content = db.Column(db.Text)                              # 沟通内容与结论
    followed_at = db.Column(db.Date, default=date.today, index=True)
    next_at = db.Column(db.Date, index=True)                  # 下次跟进日期
    owner = db.Column(db.String(64))                          # 跟进人（展示名）
    created_by = db.Column(db.String(64))                     # 登录账号
    created_at = db.Column(db.DateTime, default=datetime.now)

    customer = db.relationship("Customer", back_populates="follow_ups")
    project = db.relationship("Project", back_populates="follow_ups")

    @property
    def method_color(self) -> str:
        return {"电话": "blue", "微信": "green", "上门": "orange",
                "邮件": "cyan"}.get(self.method, "gray")

    @property
    def next_label(self) -> str:
        if not self.next_at:
            return "未安排下次跟进"
        delta = (self.next_at - date.today()).days
        if delta < 0:
            return f"下次跟进已逾期 {abs(delta)} 天"
        if delta == 0:
            return "今天需要跟进"
        return f"{delta} 天后跟进"

    def __repr__(self):
        return f"<FollowUp c{self.customer_id} {self.followed_at}>"


def refresh_customer_follow(customer: Customer) -> None:
    """根据跟进记录重算客户的「最近跟进 / 下次跟进」汇总字段

    新增或删除跟进后调用；下次跟进取所有未过期的计划日期里最近的一个。
    """
    if customer is None:
        return
    rows = customer.follow_ups or []
    if not rows:
        customer.last_follow_at = None
        customer.next_follow_at = None
        return
    customer.last_follow_at = max((f.followed_at for f in rows if f.followed_at), default=None)
    upcoming = [f.next_at for f in rows if f.next_at and f.next_at >= date.today()]
    customer.next_follow_at = min(upcoming) if upcoming else None


def human_size(size) -> str:
    """字节数转人类可读大小"""
    size = float(size or 0)
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


class Attachment(db.Model):
    """附件表：合同/发票/Word/PPT 等，只存元信息，真实文件落在 uploads 目录"""
    __tablename__ = "attachment"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_original_name = db.Column(db.String(255), nullable=False)
    file_store_name = db.Column(db.String(255), nullable=False, unique=True)
    file_path = db.Column(db.String(512), nullable=False)
    file_ext = db.Column(db.String(16))
    file_size = db.Column(db.Integer, default=0)
    uploader = db.Column(db.String(64))
    upload_time = db.Column(db.DateTime, default=datetime.now)
    remark = db.Column(db.Text)
    # ---------- 分类与版本 ----------
    category = db.Column(db.String(16), default="其他", index=True)   # 合同/发票/验收单/报告/其他
    version = db.Column(db.Integer, default=1)                        # 同名文件第几版
    version_key = db.Column(db.String(300), index=True)               # 版本归并键：项目ID + 文件名主干
    is_latest = db.Column(db.Boolean, default=True, index=True)        # 是否为该组最新版

    @property
    def size_human(self) -> str:
        return human_size(self.file_size)

    @property
    def stem(self) -> str:
        """不含扩展名的部分"""
        name = self.file_original_name or ""
        return name.rsplit(".", 1)[0] if "." in name[1:] else name

    @property
    def icon(self) -> str:
        ext = (self.file_ext or "").lower()
        return {
            "pdf": "bi-file-earmark-pdf text-danger",
            "doc": "bi-file-earmark-word text-primary",
            "docx": "bi-file-earmark-word text-primary",
            "xls": "bi-file-earmark-excel text-success",
            "xlsx": "bi-file-earmark-excel text-success",
            "csv": "bi-file-earmark-excel text-success",
            "ppt": "bi-file-earmark-ppt text-warning",
            "pptx": "bi-file-earmark-ppt text-warning",
            "zip": "bi-file-earmark-zip text-secondary",
            "rar": "bi-file-earmark-zip text-secondary",
            "7z": "bi-file-earmark-zip text-secondary",
            "txt": "bi-file-earmark-text text-info",
        }.get(
            ext,
            "bi-file-earmark-image text-info"
            if ext in {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
            else "bi-file-earmark",
        )

    def __repr__(self):
        return f"<Attachment {self.file_original_name}>"

    # ---------- 分类 / 版本展示 ----------
    @property
    def category_color(self) -> str:
        return {
            "合同": "blue", "发票": "orange", "验收单": "green",
            "报告": "purple", "其他": "gray",
        }.get(self.category or "其他", "gray")

    @property
    def version_label(self) -> str:
        return f"v{self.version or 1}"

    @property
    def older_versions(self) -> list:
        """同一文件的历史版本（按版本号倒序），用于详情页折叠展示"""
        from extensions import db
        if not self.version_key or (self.version or 1) <= 1:
            return []
        return list(db.session.scalars(
            db.select(Attachment)
            .where(
                Attachment.version_key == self.version_key,
                Attachment.version < (self.version or 1),
            )
            .order_by(Attachment.version.desc())
        ).all())


class OperationLog(db.Model):
    """操作日志：记录新增、修改、删除、上传、审批等动作"""
    __tablename__ = "operation_log"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64))
    action = db.Column(db.String(32))       # 新增 / 修改 / 删除 / 上传 / 下载 / 审批 / 登录
    target_type = db.Column(db.String(32))  # 客户 / 项目 / 附件 / 账号 / 系统
    target_id = db.Column(db.Integer)
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    # 溯源信息（来源：本次请求的 User-Agent，由 utils.log_action 落库）
    # 历史记录无此列，MySQL/SQLite 补列后为 NULL，展示时统一按「—」处理
    client_type = db.Column(db.String(64))    # 桌面浏览器 · Chrome 131 · Windows
    create_time = db.Column(db.DateTime, default=datetime.now, index=True)

    @property
    def client_kind(self) -> str:
        """客户端大类：桌面浏览器 / 移动浏览器 / 微信内置 / 接口调用 / 未知"""
        from clientinfo import client_kind_of
        return client_kind_of(self.client_type)

    @property
    def client_detail(self) -> str:
        """大类之后的明细（浏览器与系统），列表里作为次要行显示"""
        from clientinfo import client_detail_of
        return client_detail_of(self.client_type)

    @property
    def client_color(self) -> str:
        return {
            "桌面浏览器": "blue",
            "移动浏览器": "cyan",
            "微信内置": "green",
            "接口调用": "orange",
        }.get(self.client_kind, "gray")

    @property
    def action_color(self) -> str:
        return {
            "删除": "red",
            "拒绝": "red",
            "禁用": "red",
            "新增": "green",
            "上传": "green",
            "通过": "green",
            "审批": "cyan",
            "修改": "orange",
            "导出": "cyan",
            "下载": "gray",
        }.get(self.action, "gray")

    def __repr__(self):
        return f"<OperationLog {self.action} {self.target_type}>"


class ProjectPermission(db.Model):
    """项目级授权（ACL）：管理员可按项目为普通成员授予 可见 / 可修改 / 可删除

    设计要点：
    - 一个成员对同一项目只有一条授权记录（user_id + project_id 唯一）；
    - 权限位向下包含：可修改 ⇒ 一定可见，可删除 ⇒ 一定可修改，保存时统一归一化；
    - 管理员不受本表约束（管理员对全部数据拥有全部操作）。
    """
    __tablename__ = "project_permission"
    __table_args__ = (
        db.UniqueConstraint("user_id", "project_id", name="uq_perm_user_project"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = db.Column(
        db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    can_view = db.Column(db.Boolean, default=True, nullable=False)
    can_edit = db.Column(db.Boolean, default=False, nullable=False)
    can_delete = db.Column(db.Boolean, default=False, nullable=False)
    granted_by = db.Column(db.String(64))              # 最近一次授权的管理员
    create_time = db.Column(db.DateTime, default=datetime.now)
    update_time = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship(
        "User",
        backref=db.backref(
            "project_permissions",
            cascade="all, delete-orphan",
            passive_deletes=True,
            lazy="select",
        ),
    )
    project = db.relationship(
        "Project",
        backref=db.backref(
            "permissions",
            cascade="all, delete-orphan",
            passive_deletes=True,
            lazy="select",
        ),
    )

    # ---------- 归一化：高权限自动包含低权限 ----------
    @staticmethod
    def normalize(view: bool, edit: bool, delete: bool) -> tuple[bool, bool, bool]:
        if delete:
            edit = True
        if edit:
            view = True
        return bool(view), bool(edit), bool(delete)

    def apply(self, view: bool, edit: bool, delete: bool):
        self.can_view, self.can_edit, self.can_delete = self.normalize(view, edit, delete)

    @property
    def level_label(self) -> str:
        if self.can_delete:
            return "可删除"
        if self.can_edit:
            return "可修改"
        if self.can_view:
            return "可见"
        return "无权限"

    @property
    def level_color(self) -> str:
        return {"可删除": "red", "可修改": "orange", "可见": "cyan"}.get(self.level_label, "gray")

    def __repr__(self):
        return f"<ProjectPermission u{self.user_id} p{self.project_id} {self.level_label}>"


@event.listens_for(Project, "before_insert")
def _project_fill_tax(mapper, connection, target):
    """兜底：任何入口（脚本、演示数据、接口）创建项目时都补齐含税金额"""
    target.tax_rate = normalize_tax_rate(target.tax_rate)
    if not target.tax_included_amount:
        target.tax_included_amount = calc_gross(target.project_amount or 0, target.tax_rate)


# ============================== 系统设置 / 邮件令牌 ==============================
class Setting(db.Model):
    """键值配置表：SMTP 参数与各类开关都存在这里，改配置不需要改库结构"""
    __tablename__ = "setting"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        row = db.session.get(cls, key)
        return row.value if row is not None and row.value is not None else default

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        val = cls.get(key, "1" if default else "0")
        return str(val).strip().lower() in ("1", "true", "on", "yes")

    @classmethod
    def set(cls, key: str, value) -> None:
        row = db.session.get(cls, key)
        text = "" if value is None else str(value)
        if row is None:
            db.session.add(cls(key=key, value=text))
        else:
            row.value = text

    @classmethod
    def as_dict(cls, keys) -> dict:
        return {k: cls.get(k) for k in keys}

    def __repr__(self):
        return f"<Setting {self.key}>"


class EmailToken(db.Model):
    """一次性邮件令牌：邮箱验证 / 找回密码

    设计要点：
      - token 为 32 字节随机数的 hex（64 字符），不可猜测；
      - 有 purpose 区分用途，跨用途不可混用；
      - 有 expires_at，过期即失效；用掉后写 used_at，不能复用。
    """
    __tablename__ = "email_token"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    purpose = db.Column(db.String(16), default="verify", index=True)   # verify / reset
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship("User", backref="email_tokens")

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.now()

    def __repr__(self):
        return f"<EmailToken {self.purpose} u{self.user_id}>"


# ============================== 可配置选项字典 ==============================
class OptionItem(db.Model):
    """后台可维护的下拉选项（客户行业 / 项目类型 / 项目状态 …）

    - category 对应 config.OPTION_CATEGORIES 的键；
    - sort_order 越小越靠前；「其他」由程序强制排在最后，不依赖 sort_order；
    - enabled=False 时不再出现在新建/编辑表单与筛选器，但历史记录仍保留原值；
    - builtin=True 的项（如「其他」）受保护，不能删除 / 停用 / 改名。
    """
    __tablename__ = "option_item"
    __table_args__ = (
        db.UniqueConstraint("category", "name", name="ux_option_category_name"),
        db.Index("ix_option_category_sort", "category", "sort_order"),
    )

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(32), nullable=False)
    name = db.Column(db.String(32), nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    builtin = db.Column(db.Boolean, default=False, nullable=False)
    remark = db.Column(db.String(120))
    # 语义标记：目前只用于付款方式——"transfer" 表示该方式需登记流水号并参与转账对账
    extra = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def protected(self) -> bool:
        return bool(self.builtin)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "category": self.category, "name": self.name,
            "sort_order": self.sort_order, "enabled": bool(self.enabled),
            "is_default": bool(self.is_default), "builtin": bool(self.builtin),
            "remark": self.remark or "",
        }

    def __repr__(self):
        return f"<OptionItem {self.category}:{self.name}>"


OPTION_FIELDS = {
    "customer_level": [Customer.customer_level],
    "customer_business": [Customer.business_type],
    "customer_channel": [Customer.source_channel],
    "project_type": [Project.project_type],
    "project_status": [Project.project_status],
    "service_mode": [Project.service_mode],
    # 付款方式同时落在「项目约定的付款方式」和「每笔收款的付款方式」两处，
    # 统计引用与改名同步都必须覆盖，否则会删掉仍在流水里使用的选项。
    "payment_method": [Project.payment_method, Payment.pay_method],
    "pay_channel": [Payment.pay_channel],
    "followup_method": [FollowUp.method],
    "attachment_category": [Attachment.category],
}


def option_columns(category: str) -> list:
    """该选项类别对应的全部业务字段（可能跨表，如付款方式）"""
    return list(OPTION_FIELDS.get(category, []))


def option_column(category: str):
    """兼容旧调用：返回第一个业务字段；未知类别返回 None"""
    columns = option_columns(category)
    return columns[0] if columns else None


# ============================== 回收站 ==============================
class RecycleBin(db.Model):
    """回收站：删除客户 / 项目时先在此留存完整快照，可原样恢复

    为什么用快照而不是给业务表加 deleted_at：
    系统里对客户 / 项目的查询分散在列表、看板、导出、下拉、统计等几十处，
    软删除要求每处都记得加 `deleted_at IS NULL`，漏一处就会把已删数据算进
    金额与统计。快照方案让删除后业务表保持「干净」，恢复时按快照重建，
    附件物理文件（UUID 命名）保留在 uploads 里，恢复后自动重新关联。
    """
    __tablename__ = "recycle_bin"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False, index=True)   # customer / project
    title = db.Column(db.String(255))                             # 删除时的名称，便于辨认
    origin_id = db.Column(db.Integer)                             # 原记录 id（恢复时仅作提示）
    payload = db.Column(db.Text, nullable=False)                  # 完整快照 JSON
    file_paths = db.Column(db.Text)                               # 涉及的磁盘文件（彻底删除时清理）
    file_count = db.Column(db.Integer, default=0)
    file_size = db.Column(db.Integer, default=0)
    deleted_by = db.Column(db.String(64))
    deleted_at = db.Column(db.DateTime, default=datetime.now, index=True)

    @property
    def kind_label(self) -> str:
        return {"customer": "客户", "project": "项目"}.get(self.kind, self.kind)

    @property
    def size_human(self) -> str:
        return human_size(self.file_size)

    @property
    def expire_hint(self) -> str:
        """保留期提示（页面展示用，不自动清理，避免误删用户数据）"""
        return "手动清理"
