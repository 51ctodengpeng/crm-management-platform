"""客户端溯源信息：把请求的 User-Agent 归一化为可读类型

操作日志里的来源信息直接取自请求本身，不需要前端上报、也不额外发请求：

- **客户端类型** ← `User-Agent` 请求头。浏览器/客户端自己声明，这里只做归一化，
  统一成「大类 · 浏览器 版本 · 操作系统」三段，例如 `桌面浏览器 · Chrome 131 · Windows`。

⚠️ UA 可以被任意伪造，因此它只用于「区分访问来源类型」，不能当作身份凭据；
真实身份始终以登录会话为准。无法识别时统一落 `未知`，不猜测。
"""
from __future__ import annotations

import re

# 大类取值固定，前端徽标配色按它取色；新增取值时同步 style 里的映射即可
KIND_DESKTOP = "桌面浏览器"
KIND_MOBILE = "移动浏览器"
KIND_WECHAT = "微信内置"
KIND_API = "接口调用"
KIND_UNKNOWN = "未知"

# 筛选项固定顺序；列表页按此顺序生成下拉，避免不同请求顺序抖动
KINDS = (KIND_DESKTOP, KIND_MOBILE, KIND_WECHAT, KIND_API, KIND_UNKNOWN)

# 顺序敏感：越具体的特征越靠前（Edge / Opera / 微信的 UA 里都含 Chrome）
_BROWSERS: list[tuple[str, str]] = [
    ("企业微信", r"wxwork/([\d.]+)"),
    ("微信", r"MicroMessenger/([\d.]+)"),
    ("钉钉", r"DingTalk/([\d.]+)"),
    ("QQ浏览器", r"(?:QQBrowser|MQQBrowser)/([\d.]+)"),
    ("Edge", r"(?:Edg|EdgA|EdgiOS)/([\d.]+)"),
    ("Opera", r"(?:OPR|Opera)[/ ]([\d.]+)"),
    ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
    ("Chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
    ("Safari", r"Version/([\d.]+)[^;]*Safari"),
]

# 非浏览器客户端：命令行、脚本、压测/巡检工具
_API_CLIENTS: list[tuple[str, str]] = [
    ("curl", r"\bcurl/([\d.]+)"),
    ("Wget", r"\bWget/([\d.]+)"),
    ("Postman", r"PostmanRuntime/([\d.]+)"),
    ("python-requests", r"python-requests/([\d.]+)"),
    ("Go-http-client", r"Go-http-client/([\d.]+)"),
    ("okhttp", r"okhttp/([\d.]+)"),
    ("Java", r"Java/([\d.]+)"),
]

_OS_PATTERNS: list[tuple[str, str]] = [
    ("HarmonyOS", r"HarmonyOS"),
    ("Windows", r"Windows NT ([\d.]+)"),
    ("Android", r"Android ([\d.]+)"),
    ("iOS", r"(?:iPhone|iPad|iPod).*?OS ([\d_]+)"),
    ("macOS", r"Mac OS X ([\d_.]+)"),
    ("ChromeOS", r"CrOS"),
    ("Linux", r"Linux"),
]

_WINDOWS_NAMES = {
    "10.0": "10/11",
    "6.3": "8.1",
    "6.2": "8",
    "6.1": "7",
}

_MOBILE_HINTS = re.compile(r"Mobile|Android|iPhone|iPad|iPod|HarmonyOS", re.I)
_VERSION_TRIM = re.compile(r"[._]")


def _match(table: list[tuple[str, str]], ua: str, keep_full: bool = False) -> tuple[str, str]:
    """按顺序匹配特征表，返回 (名称, 版本)；都没有命中返回 ("", "")"""
    for name, pattern in table:
        hit = re.search(pattern, ua, re.I)
        if hit:
            version = ""
            if hit.groups():
                raw = hit.group(1).replace("_", ".")
                # 浏览器只保留主版本，避免 UA 里超长的小版本把字段撑满；
                # 操作系统要完整版本才能区分 Windows 10 与 8.1
                version = raw if keep_full else _VERSION_TRIM.split(raw)[0][:4]
            return name, version
    return "", ""


def _os_of(ua: str) -> tuple[str, str]:
    name, version = _match(_OS_PATTERNS, ua, keep_full=True)
    if name == "Windows":
        version = _WINDOWS_NAMES.get(version, version)
    elif name == "iOS":
        version = version.split(".")[0]
    elif name == "Android":
        version = version.split(".")[0]
    elif name == "macOS":
        version = ".".join(version.split(".")[:2])
    elif name in ("Linux", "ChromeOS", "HarmonyOS"):
        version = ""
    return name, version


def client_kind_of(client_type: str | None) -> str:
    """从落库的客户端类型里取出大类（首段），兼容手工写入与历史空值"""
    text = (client_type or "").strip()
    if not text:
        return ""
    head = text.split(" · ", 1)[0].strip()
    if head in (KIND_DESKTOP, KIND_MOBILE, KIND_WECHAT, KIND_API, KIND_UNKNOWN):
        return head
    return ""


def client_detail_of(client_type: str | None) -> str:
    """取大类之后的部分（浏览器/系统），用于列表里的次要说明行"""
    text = (client_type or "").strip()
    if " · " not in text:
        return ""
    return text.split(" · ", 1)[1]


def client_from_agent(user_agent: str | None) -> str:
    """把 User-Agent 归一化为「大类 · 客户端 版本 · 操作系统」，识别不出时返回「未知」"""
    ua = (user_agent or "").strip()
    if not ua:
        return KIND_UNKNOWN

    api_client, api_version = _match(_API_CLIENTS, ua)
    if api_client:
        kind = KIND_API
        client, version = api_client, api_version
    else:
        browser, version = _match(_BROWSERS, ua)
        if browser in ("微信", "企业微信"):
            kind = KIND_WECHAT
        elif _MOBILE_HINTS.search(ua):
            kind = KIND_MOBILE
        elif browser:
            kind = KIND_DESKTOP
        else:
            kind = KIND_UNKNOWN
        client = browser

    os_name, os_version = _os_of(ua)
    parts = [kind]
    label = " ".join(x for x in (client, version) if x)
    if label:
        parts.append(label)
    if os_name:
        parts.append(f"{os_name} {os_version}".strip())
    return " · ".join(parts)[:64]


def client_type_of(req) -> str:
    """取当前请求的客户端类型；req 为空或取不到 UA 时返回「未知」"""
    if req is None:
        return ""
    try:
        return client_from_agent(req.headers.get("User-Agent", ""))
    except Exception:      # pragma: no cover - 取不到就留空，绝不影响主流程
        return ""
