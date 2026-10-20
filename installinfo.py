# -*- coding: utf-8 -*-
"""安装版标记读取（不依赖 config，可在导入 config 之前调用）

Inno Setup 安装完成后会在安装目录写一个 `installed.ini`：

    [CRM]
    DataDir=C:\\ProgramData\\CRM客户管理系统
    Host=0.0.0.0
    Port=8081

它回答三个问题：
- 数据放哪（Program Files 不可写，装到 Program Files 时必须换到 ProgramData）
- 监听哪个地址、哪个端口（装成服务后没有命令行参数，只能从这里读）
- 当前是「安装版」还是「便携版」（双击即用的绿色版没有这个文件）

设计要点：本模块只依赖标准库，绝不 import config。因为 config 在导入时就会
锁定 DATA_DIR，任何“先导入再改环境变量”的做法都晚了。
"""
import os
import sys
from pathlib import Path

INSTALLED_INI = "installed.ini"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 EXE 中"""
    return bool(getattr(sys, "frozen", False))


def exe_dir():
    """EXE 所在目录；源码方式运行时返回 None"""
    if not is_frozen():
        return None
    return Path(sys.executable).resolve().parent


def _read_text(path: Path) -> str:
    """按 UTF-8 → GBK 顺序尝试解码

    Inno Setup 写 [INI] 时可能落成 ANSI(GBK)，而路径里带中文（CRM客户管理系统），
    解错编码会让数据目录变成一个带问号的乱码路径，必须两种都试。
    """
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _parse_ini(path: Path) -> dict:
    """极简 INI 解析：只认 [CRM] 段下的 key=value，够用且不引入额外依赖"""
    out = {}
    section = ""
    try:
        text = _read_text(path)
    except Exception:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "crm" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


def read_installed() -> dict:
    """读取安装版信息；便携版或读取失败时返回空字典"""
    d = exe_dir()
    if d is None:
        return {}
    p = d / INSTALLED_INI
    if not p.exists():
        return {}
    cfg = _parse_ini(p)
    cfg["_ini"] = str(p)
    return cfg


def prepare_env_from_install() -> dict:
    """把安装版配置写进环境变量（仅设置尚未存在的键，不覆盖显式指定值）

    必须在 `import config` 之前调用：config 在导入时就会解析 DATA_DIR。
    """
    info = read_installed()
    if not info:
        return {}
    if info.get("datadir"):
        os.environ.setdefault("CRM_DATA_DIR", info["datadir"])
    if info.get("host"):
        os.environ.setdefault("CRM_HOST", info["host"])
    if info.get("port"):
        os.environ.setdefault("CRM_PORT", info["port"])
    return info
