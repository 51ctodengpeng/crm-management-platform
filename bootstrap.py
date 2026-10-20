# -*- coding: utf-8 -*-
"""首次运行自举：建表 + 补列 + 建默认管理员

打包成 EXE 后双击即用，用户不可能先去命令行跑 init_db.py，
因此把「数据库准备」收敛到这里，任何入口（app.py / desktop.py / init_db.py）启动时都会执行，且完全幂等。

- 空库：建全部表，并创建默认管理员
- 老库（PLUS 版本之前的结构）：自动补齐新增字段与索引，不丢数据
- 已是新库：什么都不做
"""
from sqlalchemy import inspect, text

import dbcompat
from extensions import db

# 表 -> [(列名, 建列 SQL), ...]：老库缺哪些补哪些
NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "user": [
        ("real_name", "VARCHAR(64)"),
        ("email", "VARCHAR(128)"),
        ("phone", "VARCHAR(32)"),
        ("department", "VARCHAR(64)"),
        ("wechat", "VARCHAR(64)"),
        ("avatar", "VARCHAR(255)"),
        ("role", "VARCHAR(16) DEFAULT 'user'"),
        ("status", "VARCHAR(16) DEFAULT 'pending'"),
        ("apply_reason", "TEXT"),
        ("review_remark", "TEXT"),
        ("approved_by", "VARCHAR(64)"),
        ("approve_time", "DATETIME"),
        # 权限模型升级新增：数据范围 / 新增客户项目的能力
        ("data_scope", "VARCHAR(16) DEFAULT 'assigned'"),
        ("can_create", "BOOLEAN DEFAULT 0"),
        ("email_verified", "BOOLEAN DEFAULT 0"),
        # 登录防护：连续错误次数与临时锁定截止时间
        ("failed_count", "INTEGER DEFAULT 0"),
        ("locked_until", "DATETIME"),
    ],
    "customer": [
        ("position", "VARCHAR(32)"),
        ("wechat", "VARCHAR(64)"),
        ("region", "VARCHAR(64)"),
        # 省 / 市 / 区三级结构化（region 仍是展示与搜索用的拼接文本，此处为可统计的拆分值）
        ("province", "VARCHAR(32)"),
        ("city", "VARCHAR(32)"),
        ("district", "VARCHAR(32)"),
        ("source_channel", "VARCHAR(32)"),
        ("owner", "VARCHAR(64)"),
        ("last_follow_at", "DATE"),
        ("next_follow_at", "DATE"),
    ],
    "project": [
        ("tax_rate", "FLOAT DEFAULT 6"),
        ("tax_included_amount", "FLOAT DEFAULT 0"),
        ("service_mode", "VARCHAR(16) DEFAULT '远程服务'"),
        ("payment_method", "VARCHAR(16) DEFAULT '支付软件转账'"),
        ("owner", "VARCHAR(64)"),
        ("contract_no", "VARCHAR(64)"),
        ("sign_date", "DATE"),
        ("payment_terms", "TEXT"),
        ("credit_days", "INTEGER DEFAULT 30"),
    ],
    "attachment": [
        ("uploader", "VARCHAR(64)"),
        ("category", "VARCHAR(16) DEFAULT '其他'"),
        ("version", "INTEGER DEFAULT 1"),
        ("version_key", "VARCHAR(300)"),
        ("is_latest", "BOOLEAN DEFAULT 1"),
    ],
    "payment": [
        # 付款方式已改为后台可配置字典，转账判定不能再依赖名称，落库一个标记位
        ("transfer_flag", "BOOLEAN DEFAULT 0"),
        ("plan_id", "INTEGER"),
    ],
    # 溯源信息：老日志补列后为 NULL，页面按「—」展示，不追改历史数据
    "operation_log": [
        ("client_type", "VARCHAR(64)"),
    ],
}

NEW_INDEXES = {
    "user": [("status", "ix_user_status"), ("data_scope", "ix_user_data_scope")],
    "customer": [("source_channel", "ix_customer_source_channel"),
                 ("owner", "ix_customer_owner"),
                 ("province", "ix_customer_province"),
                 ("next_follow_at", "ix_customer_next_follow_at")],
    "project": [("service_mode", "ix_project_service_mode"),
                ("payment_method", "ix_project_payment_method"),
                ("owner", "ix_project_owner"),
                ("contract_no", "ix_project_contract_no"),
                ("sign_date", "ix_project_sign_date")],
    "payment": [("project_id", "ix_payment_project_id"), ("status", "ix_payment_status"),
                ("plan_id", "ix_payment_plan_id")],
    "attachment": [("category", "ix_attachment_category"),
                   ("version_key", "ix_attachment_version_key"),
                   ("is_latest", "ix_attachment_is_latest")],
}


def ensure_columns(verbose: bool = False) -> dict:
    """为已有库补齐新增列与索引（幂等），返回统计信息"""
    stats = {"columns": 0, "indexes": 0, "legacy": False, "scope_added": False,
             "tax_added": False}
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in NEW_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in present:
                continue
            db.session.execute(
                text(f"ALTER TABLE {dbcompat.q(table)} ADD COLUMN {dbcompat.q(name)} {ddl}")
            )
            stats["columns"] += 1
            if table == "user" and name == "status":
                stats["legacy"] = True
            if table == "user" and name == "data_scope":
                stats["scope_added"] = True
            if table == "project" and name == "tax_included_amount":
                stats["tax_added"] = True
            if verbose:
                print(f"      + {table}.{name}")
    db.session.commit()

    for table, indexes in NEW_INDEXES.items():
        if table not in existing_tables:
            continue
        for column, index_name in indexes:
            if dbcompat.create_index(table, index_name, column):
                stats["indexes"] += 1
    db.session.commit()

    # 存量账号兜底：SQLite 的 ADD COLUMN 会把 DEFAULT 应用到已有行，
    # 老库账号会被写成 pending / user，导致管理员被锁在门外，这里统一归位。
    U = dbcompat.q("user")
    if stats["legacy"] and "user" in existing_tables:
        db.session.execute(text(f"UPDATE {U} SET status = 'active'"))
        db.session.execute(text(f"UPDATE {U} SET role = 'admin'"))
    db.session.execute(text(
        f"UPDATE {U} SET status = 'active' WHERE status IS NULL OR status = ''"
    ))
    db.session.execute(text(
        f"UPDATE {U} SET role = 'admin' WHERE role IS NULL OR role = ''"
    ))
    db.session.execute(text(
        f"UPDATE {U} SET real_name = username WHERE real_name IS NULL OR real_name = ''"
    ))

    # 权限模型升级：新增 data_scope 列时，存量账号统一设为「全部数据」，
    # 保持升级前后可见范围一致（否则老成员登录后会突然看不到任何数据）。
    # 之后新建 / 新注册的账号默认「仅授权项目」，由管理员按项目授权。
    if stats["scope_added"]:
        db.session.execute(text(f"UPDATE {U} SET data_scope = 'all'"))
    db.session.execute(text(
        f"UPDATE {U} SET data_scope = 'assigned' WHERE data_scope IS NULL OR data_scope = ''"
    ))
    db.session.execute(text(
        f"UPDATE {U} SET can_create = 0 WHERE can_create IS NULL"
    ))
    db.session.commit()

    # 交易流水号唯一（部分索引）：空流水号（现金 / 未填）不参与唯一约束，
    # 从数据库层兜底防止同一笔转账被重复登记、重复对账。
    if "payment" in existing_tables:
        dbcompat.ensure_payment_trade_no_unique()

    # 转账标记回填：升级前只有「支付软件转账」走流水号，按名称归位一次
    if "payment" in existing_tables:
        db.session.execute(text(
            f"UPDATE {dbcompat.q('payment')} SET transfer_flag = 1 "
            "WHERE pay_method = '支付软件转账' AND (transfer_flag IS NULL OR transfer_flag = 0)"
        ))
        db.session.commit()

    # 含税价格升级：老项目的 tax_included_amount 被 ADD COLUMN 写成 0，
    # 这里按各自税点（默认 6%）正算补齐，保证列表/详情/导出不再显示 0。
    _backfill_project_tax(force=stats["tax_added"])

    return stats


def _backfill_project_tax(force: bool = False) -> int:
    """回填存量项目的含税金额，返回修正条数（幂等）"""
    from models import DEFAULT_TAX_RATE, Project, calc_gross, money_round, normalize_tax_rate

    db.session.execute(text(
        f"UPDATE {dbcompat.q('project')} SET tax_rate = 6 WHERE tax_rate IS NULL"
    ))
    db.session.commit()

    rows = db.session.scalars(
        db.select(Project).where(
            db.or_(
                Project.tax_included_amount.is_(None),
                db.and_(Project.tax_included_amount == 0,
                        Project.project_amount.isnot(None),
                        Project.project_amount != 0),
            )
        )
    ).all()

    fixed = 0
    for p in rows:
        rate = normalize_tax_rate(p.tax_rate if p.tax_rate is not None else DEFAULT_TAX_RATE)
        gross = calc_gross(money_round(p.project_amount or 0), rate)
        if not gross and not force:
            continue
        p.tax_rate = rate
        p.tax_included_amount = gross
        fixed += 1
    if fixed:
        db.session.commit()
    return fixed


def ensure_option_items(verbose: bool = False) -> int:
    """初始化下拉字典（幂等）

    只在该类别一条记录都没有时写入 config 里的预设值：
    管理员后续的增删改排、停用都不会被下次启动覆盖，也不会把删掉的预设值加回来。
    """
    from config import Config
    from models import OptionItem

    created = 0
    for category, meta in Config.OPTION_CATEGORIES.items():
        exists = db.session.scalar(
            db.select(db.func.count(OptionItem.id)).where(OptionItem.category == category)
        ) or 0
        if exists:
            continue
        protected = set(meta.get("protected") or [])
        default_value = meta.get("default_value") or ""
        for index, name in enumerate(meta.get("defaults") or []):
            db.session.add(OptionItem(
                category=category,
                name=name,
                sort_order=(index + 1) * 10,
                enabled=True,
                is_default=(name == default_value),
                builtin=name in protected,
                extra=(meta.get("extras") or {}).get(name, ""),
            ))
            created += 1
            if verbose:
                print(f"      + option {category}.{name}")
    if created:
        db.session.commit()
    return created


def ensure_option_extras() -> int:
    """为存量选项补齐语义标记（幂等，按类别各做一次）

    老库的 option_item 没有 extra 列，升级后「进行中」等状态拿不到语义，
    会导致概览统计和徽标配色退化。这里按 config 的预设补一次；
    之后管理员在选项管理里的调整（包括清空标记）不会被启动流程覆盖。

    按类别记录种子标记：新增类别（如附件分类）只补自己那一份，
    老类别的历史标记不会被重新写入。
    """
    from config import Config
    from models import OptionItem, Setting

    legacy_seeded = Setting.get("option_extras_seeded") == "1"
    # 本次版本新增的选项类别：老库的全局标记已置位时，这些类别仍需单独补种子
    new_categories = {"attachment_category"}
    fixed = 0
    for category, meta in Config.OPTION_CATEGORIES.items():
        key = f"option_extras_seeded:{category}"
        if Setting.get(key) == "1":
            continue
        if legacy_seeded and category not in new_categories:
            # 升级前的全局标记已置位：历史类别视为已处理，仅补本次新增的类别
            Setting.set(key, "1")
            continue
        extras = meta.get("extras") or {}
        for name, extra in extras.items():
            rows = db.session.scalars(
                db.select(OptionItem).where(
                    OptionItem.category == category, OptionItem.name == name
                )
            ).all()
            for row in rows:
                if not (row.extra or "").strip():
                    row.extra = extra
                    fixed += 1
        Setting.set(key, "1")
    Setting.set("option_extras_seeded", "1")
    db.session.commit()
    return fixed


def ensure_attachment_meta() -> int:
    """为存量附件补齐分类与版本归并键（幂等）

    老库的附件没有 category / version_key：分类回落「其他」，
    归并键按「项目ID:文件名主干」补一份，这样新上传的同名文件能接着排版本号。
    """
    from models import Attachment

    rows = db.session.scalars(
        db.select(Attachment).where(
            db.or_(Attachment.version_key.is_(None), Attachment.version_key == "")
        )
    ).all()
    fixed = 0
    for row in rows:
        stem = row.stem.strip().lower()[:120]
        row.version_key = f"{row.project_id}:{stem}" if stem else ""
        if row.version is None:
            row.version = 1
        if row.is_latest is None:
            row.is_latest = True
        if not (row.category or "").strip():
            row.category = "其他"
        fixed += 1
    if fixed:
        db.session.commit()
    return fixed


def ensure_contacts() -> int:
    """把客户主档上的联系人字段升格成「联系人子表」首条记录（幂等，只做一次）

    老版本客户只有一个对接人，升级后统一变成「主联系人」，
    之后可以在详情页继续添加其他对接人。
    """
    from models import Contact, Customer, Setting

    if Setting.get("contacts_seeded") == "1":
        return 0

    created = 0
    for customer in db.session.scalars(db.select(Customer)).all():
        if customer.contacts:
            continue
        if not any([customer.contact_name, customer.contact_phone,
                    customer.contact_email, customer.wechat, customer.position]):
            continue
        db.session.add(Contact(
            customer_id=customer.id,
            name=customer.contact_name or "主要联系人",
            position=customer.position,
            phone=customer.contact_phone,
            email=customer.contact_email,
            wechat=customer.wechat,
            is_primary=True,
        ))
        created += 1
    Setting.set("contacts_seeded", "1")
    db.session.commit()
    return created


def ensure_default_admin() -> tuple[bool, str, str]:
    """账号表为空时创建默认管理员。返回 (是否新建, 用户名, 密码)

    未通过环境变量指定初始口令时，随机生成 12 位密码，避免出现全网通用的
    固定初始口令；该密码仅在首次创建时返回一次，由调用方打印给管理员。
    """
    from config import Config
    from models import User

    username = Config.DEFAULT_ADMIN_USERNAME
    password = Config.DEFAULT_ADMIN_PASSWORD

    if db.session.scalar(db.select(db.func.count(User.id))) or 0:
        return False, username, ""

    if not password:
        import secrets
        import string
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(12))

    user = User(username=username, real_name=username, role="admin", status="active")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return True, username, password


def bootstrap(app, verbose: bool = False) -> dict:
    """应用启动前的数据准备，幂等可重复调用"""
    from config import Config

    Config.ensure_dirs()
    result = {"created_columns": 0, "created_admin": False, "username": None, "password": None,
              "created_options": 0}

    with app.app_context():
        db.create_all()                      # 空库建表；已存在则跳过
        stats = ensure_columns(verbose=verbose)
        result["created_columns"] = stats["columns"]
        result["created_options"] = ensure_option_items(verbose=verbose)
        ensure_option_extras()
        ensure_attachment_meta()
        ensure_contacts()
        created, username, password = ensure_default_admin()
        result["created_admin"] = created
        result["username"] = username
        result["password"] = password
    return result
