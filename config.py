# -*- coding: utf-8 -*-
"""配置文件：路径、数据库、上传目录、密钥、文件大小限制

打包为 EXE 后有两类路径，务必区分（区分错了会丢数据）：
- RESOURCE_DIR「资源目录」：模板 / 静态文件，随程序分发，只读。
  打包后位于 PyInstaller 解压出的临时目录（sys._MEIPASS），进程退出即被清理。
- DATA_DIR「数据目录」：crm.db / uploads / .secret_key，必须可写且持久。
  打包后固定放在 EXE 同级目录（绿色便携），该目录不可写时回落到 %LOCALAPPDATA%\\CRMSystem。
"""
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 EXE 中"""
    return bool(getattr(sys, "frozen", False))


def _resource_dir() -> Path:
    """只读资源目录：模板与静态文件所在位置"""
    if is_frozen():
        # 单文件模式下 PyInstaller 把随包数据解压到 _MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _writable(path: Path) -> bool:
    """探测目录是否真的可写（Program Files 等目录普通用户没有写权限）"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _data_dir() -> Path:
    """可写数据目录：crm.db / uploads / .secret_key 都落在这里"""
    # 1) 环境变量优先，方便多实例、测试或把数据放到指定盘
    env = os.environ.get("CRM_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    # 2) 源码方式运行：就用项目目录，保持原有习惯不变
    if not is_frozen():
        return Path(__file__).resolve().parent
    # 3) EXE 运行：优先 EXE 同级目录（整个文件夹拷走即可迁移）
    exe_dir = Path(sys.executable).resolve().parent
    if _writable(exe_dir):
        return exe_dir
    # 4) 兜底：EXE 放在只读目录（如 Program Files）时改用用户数据目录
    fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CRMSystem"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


RESOURCE_DIR = _resource_dir()
DATA_DIR = _data_dir()
BASE_DIR = DATA_DIR          # 向后兼容：历史代码里的 BASE_DIR 指数据目录


def _db_settings() -> dict:
    """数据库连接参数：设了 CRM_DB_HOST（或完整 CRM_DATABASE_URL）就走 MySQL，否则 SQLite。

    服务器部署只需在 systemd 的 EnvironmentFile 里写：
        CRM_DB_HOST=127.0.0.1
        CRM_DB_NAME=crm
        CRM_DB_USER=crm
        CRM_DB_PASSWORD=***
    其余参数按 MySQL 生产环境的常规值兜底，业务代码完全不用改。
    """
    sqlite_path = Path(os.environ.get("CRM_DB_PATH") or (DATA_DIR / "crm.db"))
    sqlite_settings = {
        "backend": "sqlite",
        "host": "", "port": 0, "name": "", "user": "", "password": "",
        "uri": "sqlite:///" + sqlite_path.as_posix(),
        "engine_options": {
            "pool_pre_ping": True,
            "connect_args": {"timeout": 15},
        },
    }

    url = (os.environ.get("CRM_DATABASE_URL") or "").strip()
    host = (os.environ.get("CRM_DB_HOST") or "").strip()
    if not url and not host:
        return sqlite_settings

    from urllib.parse import quote_plus

    host = host or "127.0.0.1"
    port = int(os.environ.get("CRM_DB_PORT") or 3306)
    name = (os.environ.get("CRM_DB_NAME") or "crm").strip()
    user = (os.environ.get("CRM_DB_USER") or "crm").strip()
    password = os.environ.get("CRM_DB_PASSWORD") or ""

    if not url:
        url = (
            f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{name}?charset=utf8mb4"
        )
    return {
        "backend": "mysql",
        "host": host, "port": port, "name": name,
        "user": user, "password": password,
        "uri": url,
        # 连接池：MySQL 会主动断开空闲连接（默认 8 小时），回收要比它短
        "engine_options": {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "pool_size": 5,
            "max_overflow": 10,
            "connect_args": {"connect_timeout": 10, "charset": "utf8mb4"},
        },
    }


DB_SETTINGS = _db_settings()


def _load_secret_key() -> str:
    """SECRET_KEY 优先取环境变量，其次读本地 .secret_key 文件，都没有则随机生成并落盘。
    绝不硬编码在代码里。"""
    key = os.environ.get("CRM_SECRET_KEY")
    if key:
        return key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        content = key_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    key = secrets.token_hex(32)
    try:
        key_file.write_text(key, encoding="utf-8")
        # 同机其他账号不该读得到签名密钥：拿到它就等于能伪造任意会话与 CSRF 令牌
        os.chmod(key_file, 0o600)
    except OSError:
        pass          # 实在写不了也不影响本次运行，只是重启后会话失效
    return key


class Config:
    # ---------- 路径 ----------
    RESOURCE_DIR = RESOURCE_DIR
    DATA_DIR = DATA_DIR
    TEMPLATE_FOLDER = str(RESOURCE_DIR / "templates")
    STATIC_FOLDER = str(RESOURCE_DIR / "static")

    # ---------- 基础 ----------
    SECRET_KEY = _load_secret_key()
    JSON_AS_ASCII = False
    APP_NAME = "客户管理系统"

    # ---------- 数据库 ----------
    # 两种后端，按部署形态自动选择，业务代码无感知：
    #   sqlite —— 桌面版 / EXE / 单元自测，单文件，零依赖
    #   mysql  —— 服务器部署（Nginx + Gunicorn），设 CRM_DB_HOST 等变量即启用
    DB_BACKEND = DB_SETTINGS["backend"]
    DB_HOST = DB_SETTINGS["host"]
    DB_PORT = DB_SETTINGS["port"]
    DB_NAME = DB_SETTINGS["name"]
    DB_USER = DB_SETTINGS["user"]
    DB_PASSWORD = DB_SETTINGS["password"]
    # SQLite 文件路径（MySQL 模式下仅用于本地兼容与备份提示，不参与连接）
    DB_PATH = Path(os.environ.get("CRM_DB_PATH") or (DATA_DIR / "crm.db"))
    SQLALCHEMY_DATABASE_URI = DB_SETTINGS["uri"]
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = DB_SETTINGS["engine_options"]

    # ---------- 文件上传 ----------
    UPLOAD_FOLDER = os.environ.get("CRM_UPLOAD_DIR") or str(DATA_DIR / "uploads")
    # 头像单独存放，避免与业务附件混在一起
    AVATAR_FOLDER = os.environ.get("CRM_AVATAR_DIR") or str(DATA_DIR / "avatars")
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 单文件最大 20MB
    MAX_AVATAR_SIZE = 2 * 1024 * 1024      # 头像最大 2MB
    ALLOWED_AVATAR_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
    ALLOWED_EXTENSIONS = {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "txt", "csv", "zip", "rar", "7z",
        "jpg", "jpeg", "png", "gif", "bmp", "webp",
    }
    # 明确黑名单，双保险（防止后缀被绕过）
    DANGEROUS_EXTENSIONS = {
        "exe", "bat", "cmd", "com", "msi", "scr", "pif", "vbs", "vbe",
        "js", "jse", "wsf", "wsh", "ps1", "psm1", "sh", "py", "pyc",
        "php", "jsp", "asp", "aspx", "cgi", "pl", "jar", "dll", "so",
    }

    # ---------- 分页 ----------
    PER_PAGE = 10

    # ---------- 账号安全 ----------
    PASSWORD_MIN_LENGTH = 8          # 新密码最短长度（需同时包含字母与数字）
    LOGIN_MAX_FAILURES = 5           # 连续密码错误多少次后临时锁定
    LOGIN_LOCK_MINUTES = 15          # 临时锁定时长（分钟）
    SESSION_IDLE_MINUTES = int(os.environ.get("CRM_SESSION_IDLE_MINUTES") or 120)
    # 浏览器会话 Cookie 有效期（绝对上限），空闲超时会提前失效
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ---------- 反向代理（Nginx + HTTPS 部署） ----------
    # 放在 Nginx 后面时打开：让 Flask 认 X-Forwarded-Proto / X-Forwarded-For，
    # 否则生成的绝对链接会退回 http://，日志里记到的也是 127.0.0.1
    BEHIND_PROXY = os.environ.get("CRM_BEHIND_PROXY") == "1"
    # 全站 HTTPS 后把会话 Cookie 标记为 Secure，明文通道拿不到登录态
    SESSION_COOKIE_SECURE = os.environ.get("CRM_COOKIE_SECURE") == "1"

    # ---------- 业务字典 ----------
    CUSTOMER_LEVELS = ["重要", "普通", "潜在"]
    BUSINESS_TYPES = ["政企", "制造业", "金融", "医疗", "教育", "互联网", "其他"]
    PROJECT_TYPES = ["虚拟化", "AD域", "存储故障", "技术咨询", "网络", "其他"]
    # 「其他」为常规选项；表单另提供「自定义…」可自由填写，保存后自动进入下拉候选
    PROJECT_STATUSES = ["待实施", "进行中", "已验收", "已终止", "其他"]

    # 服务方式：交付形式，决定是否需要出差 / 驻场，影响成本与排期
    SERVICE_MODES = ["远程服务", "现场服务", "远程+现场"]
    # 付款方式：约定/实际的收款形式。支付软件转账需要流水号并参与对账；现金不产生流水号
    PAYMENT_METHODS = ["支付软件转账", "现金支付"]
    TRANSFER_METHOD = "支付软件转账"
    CASH_METHOD = "现金支付"
    # 转账渠道（仅「支付软件转账」时可填），用于和账单流水核对
    PAY_CHANNELS = ["微信", "支付宝", "银行转账", "云闪付", "其他"]
    # 对账状态：待对账 -> 已对账（对账后金额计入「已收」，不可随意回退）
    PAYMENT_STATUSES = {"pending": "待对账", "matched": "已对账"}

    # 客户来源渠道（表单下拉预设，选择"自定义"可自由填写并入库）
    SOURCE_CHANNELS = [
        "互联网", "客户介绍", "老客户续约", "展会活动", "电话拜访",
        "渠道代理", "招投标", "行业社群", "其他",
    ]
    CUSTOM_CHANNEL_FLAG = "__custom__"   # 前端"自定义"选项的哨兵值

    # ---------- 可配置选项字典（后台「选项管理」可维护） ----------
    # 设计原则：
    #   1) 预设值只用于首次建库时初始化（seed），之后以数据库为准，管理员的增删改排不会被打回；
    #   2) protected 里的值（通常是「其他」）不允许删除 / 停用 / 改名，保证表单兜底可用；
    #   3) default_value 仅作新建表单的初始选中项，必须能在候选里找到，否则自动回落第一项；
    #   4) field 指向实际业务字段，用于统计使用量、重命名时同步历史数据；
    #   5) 表单里的「自定义…」只作用于当前这条记录，不会写进本字典。
    OPTION_CATEGORIES = {
        "customer_level": {
            "label": "客户等级", "group": "客户",
            "desc": "客户分级，用于列表筛选与批量调整",
            "defaults": ["重要", "普通", "潜在"],
            "protected": [], "default_value": "普通",
            # extra 存徽标颜色：改名后仍保持原来的视觉层级（重要=红、普通=灰、潜在=橙）
            "extras": {"重要": "red", "普通": "gray", "潜在": "orange"},
            "extra_kind": "level_color",
            "model": "customer", "field": "customer_level",
        },
        "customer_business": {
            "label": "客户行业类型", "group": "客户",
            "desc": "客户所属行业，用于行业分布统计",
            "defaults": ["政企", "制造业", "金融", "医疗", "教育", "互联网", "其他"],
            "protected": ["其他"], "default_value": "",
            "model": "customer", "field": "business_type",
        },
        "customer_channel": {
            "label": "客户来源渠道", "group": "客户",
            "desc": "客户获取渠道，用于来源渠道分布统计",
            "defaults": ["互联网", "客户介绍", "老客户续约", "展会活动", "电话拜访",
                         "渠道代理", "招投标", "行业社群", "其他"],
            "protected": ["其他"], "default_value": "",
            "model": "customer", "field": "source_channel",
        },
        "project_type": {
            "label": "项目类型", "group": "项目",
            "desc": "项目所属技术类型，用于列表筛选与批量调整",
            "defaults": ["虚拟化", "AD域", "存储故障", "技术咨询", "网络", "其他"],
            "protected": ["其他"], "default_value": "",
            "model": "project", "field": "project_type",
        },
        "project_status": {
            "label": "项目状态", "group": "项目",
            "desc": "项目推进阶段，影响概览页状态分布",
            "defaults": ["待实施", "进行中", "已验收", "已终止", "其他"],
            "protected": ["其他"], "default_value": "待实施",
            # extra 存状态语义：概览「进行中项目」按 running 统计，不再依赖中文名
            "extras": {"待实施": "pending", "进行中": "running",
                       "已验收": "success", "已终止": "failed"},
            "extra_kind": "status_kind",
            "model": "project", "field": "project_status",
        },
        "service_mode": {
            "label": "服务方式", "group": "项目",
            "desc": "交付形式，决定是否需要出差与驻场",
            "defaults": ["远程服务", "现场服务", "远程+现场"],
            "protected": [], "default_value": "远程服务",
            "model": "project", "field": "service_mode",
        },
        "payment_method": {
            "label": "付款方式", "group": "收款",
            "desc": "约定或实际的收款形式；「支付软件转账」需登记流水号并参与对账",
            "defaults": ["支付软件转账", "现金支付"],
            "protected": [], "default_value": "支付软件转账",
            # 带 transfer 标记的方式需要流水号、参与「已收」对账；现金不需要
            "extras": {"支付软件转账": "transfer"},
            "extra_kind": "transfer",
            "model": "project", "field": "payment_method",
        },
        "followup_method": {
            "label": "跟进方式", "group": "客户",
            "desc": "记录与客户沟通的方式，用于客户跟进时间轴",
            "defaults": ["电话", "微信", "上门", "邮件", "其他"],
            "protected": ["其他"], "default_value": "电话",
            "model": "follow_up", "field": "method",
        },
        "pay_channel": {
            "label": "转账渠道", "group": "收款",
            "desc": "转账时使用的支付渠道，用于与银行/平台账单核对",
            "defaults": ["微信", "支付宝", "银行转账", "云闪付", "其他"],
            "protected": ["其他"], "default_value": "",
            "model": "payment", "field": "pay_channel",
        },
        "attachment_category": {
            "label": "附件分类", "group": "项目",
            "desc": "上传附件时归入的类别，便于在项目中按类别筛选合同、发票等材料",
            "defaults": ["合同", "发票", "验收单", "报告", "其他"],
            "protected": ["其他"], "default_value": "其他",
            "extras": {"合同": "blue", "发票": "orange", "验收单": "green",
                       "报告": "purple", "其他": "gray"},
            "extra_kind": "level_color",
            "model": "attachment", "field": "category",
        },
    }

    # ---------- 账号体系 ----------
    USER_ROLES = {"admin": "管理员", "user": "普通用户"}
    USER_STATUSES = {
        "pending": "待审批",
        "active": "已启用",
        "disabled": "已禁用",
        "rejected": "已拒绝",
    }

    # ---------- 权限模型：数据范围 + 项目级操作类型 ----------
    # 数据范围：管理员恒为 all；普通成员可为 all（全员可见）或 assigned（仅授权项目）
    DATA_SCOPES = {
        "all": "全部数据",
        "assigned": "仅授权项目",
    }
    # 项目级操作类型：右侧为基础权限，左侧为其包含关系（delete ⊃ edit ⊃ view）
    PERMISSIONS = {
        "view": "查看",
        "edit": "修改",
        "delete": "删除",
    }
    PERMISSION_DESC = {
        "view": "列表与详情可见，可下载 / 预览附件",
        "edit": "可编辑项目与客户信息、上传附件、修改附件备注",
        "delete": "可删除项目及其附件（风险操作，建议仅授予负责人）",
    }
    # 注册开关：关掉后注册页提示"暂未开放注册"
    # 内网系统默认开放；公网部署建议在 systemd EnvironmentFile 里写
    # CRM_ALLOW_REGISTER=0，环境变量优先级高于后台「系统设置」里的同名开关。
    ALLOW_REGISTER = os.environ.get("CRM_ALLOW_REGISTER", "1") == "1"

    # ---------- 站点 / 通用设置默认值（管理员可在「系统设置」中改，存 setting 表） ----------
    SITE_DEFAULTS = {
        "site_name": "客户管理系统",
        "site_subtitle": "客户信息 · 项目记录 · 合同附件 一站式管理",
        "default_per_page": "20",
        "allow_register": "1",
        "dashboard_notice": "",
    }
    SITE_NAME_MIN, SITE_NAME_MAX = 2, 24
    PER_PAGE_OPTIONS = [10, 20, 50, 100]

    # ---------- 邮件（SMTP）默认配置 ----------
    # 真实值存在 setting 表（管理员在「邮件设置」页维护），这里只放默认值与选项字典
    MAIL_DEFAULTS = {
        "smtp_host": "",
        "smtp_port": "465",
        "smtp_user": "",
        "smtp_password_enc": "",          # 密文，见 mailer.encrypt_secret
        "smtp_security": "ssl",           # ssl / starttls / none
        "smtp_sender_name": "客户管理系统",
        "smtp_enabled": "0",              # 总开关：关掉后所有发信被跳过
        # 功能开关
        "email_register": "0",            # 邮箱注册（注册需验证邮箱）
        "email_reset": "0",               # 邮箱找回密码
        "email_notify": "0",              # 重要操作邮件通知
        "email_auto_activate": "1",       # 邮箱验证通过后自动激活（否则仍需管理员审批）
        "email_notify_events": "新增,删除,审批,对账",
    }
    MAIL_SECURITY = {
        "ssl": "SSL/TLS（465 端口常用）",
        "starttls": "STARTTLS（587 端口常用）",
        "none": "不加密（内网中继）",
    }
    NOTIFY_EVENTS = ["新增", "修改", "删除", "审批", "对账", "导出", "重置密码"]
    MAIL_TIMEOUT = 12                    # SMTP 超时（秒），避免请求被网络卡死
    EMAIL_TOKEN_TTL = {"verify": 24 * 60, "reset": 30}   # 有效期（分钟）

    # 首次运行自动创建的管理员（仅在账号表为空时生效）
    DEFAULT_ADMIN_USERNAME = os.environ.get("CRM_ADMIN_USER", "admin")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("CRM_ADMIN_PASSWORD", "admin123")

    @staticmethod
    def ensure_dirs():
        Path(Config.UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
        Path(Config.AVATAR_FOLDER).mkdir(parents=True, exist_ok=True)
