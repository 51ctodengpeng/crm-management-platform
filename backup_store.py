# -*- coding: utf-8 -*-
"""备份与恢复的数据层

职责边界：
- 本模块只处理「数据怎么落地」：库快照（SQLite online backup / mysqldump）、
  附件与头像打包、备份包列举、从备份包恢复；
- 权限、表单校验、flash 提示、跳转等 HTTP 相关的事情留在 routes/backup.py。

为什么单独拆出来：备份涉及四种完全不同的机制（sqlite3 API、subprocess 调
mysqldump/mysql、zipfile、目录拷贝），和路由的 request/flash 逻辑混在一个文件里时，
任何一处排错都要在 400 行里来回翻。拆开后路由层只剩"取参数 → 调用 → 提示"。

一致性要点（改代码时不要破坏）：
- 备份必须是"一致性快照"：SQLite 用 online backup API，MySQL 用 --single-transaction；
- 一个备份 = 一个 zip：库快照 + uploads/ + avatars/ + manifest.json；
- 恢复前先自动做一份 pre-restore 备份，误操作可回退；
- SQLite 替换文件前必须 dispose 连接池，否则 Windows 上文件被占用无法覆盖；
- 恢复后重跑 bootstrap，使老备份也能自动补齐新增列与字典项。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from flask import current_app

import dbcompat
from config import Config
from extensions import db
from models import Attachment, Customer, Payment, Project, User

NAME_RE = "crm-backup-"
# 备份包内的数据库快照文件名：SQLite 是库文件本身，MySQL 是 SQL 文本
SQLITE_MEMBER = "crm.db"
MYSQL_MEMBER = "crm.sql"

# 恢复包的安全上限：压缩包本身受 MAX_CONTENT_LENGTH 限制（20MB），
# 但解压后体积不受控，必须单独设限，否则一个高压缩比 zip 可以写满磁盘。
MAX_MEMBERS = 20000                      # 条目数上限
MAX_TOTAL_BYTES = 4 * 1024 ** 3          # 解压后总字节上限（4GB）
RESTORE_LOCK_NAME = ".restore.lock"


# ============================== 路径 ==============================
def backup_dir() -> Path:
    path = Path(Config.DATA_DIR) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return Path(Config.DB_PATH)


def db_member() -> str:
    """当前后端对应的快照文件名"""
    return MYSQL_MEMBER if dbcompat.is_mysql() else SQLITE_MEMBER


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


# ============================== 库快照 ==============================
def snapshot_db(dest: Path) -> None:
    """生成一致性快照（WAL / 未提交事务都不会拷坏；MySQL 不锁表）"""
    if dbcompat.is_mysql():
        dump_mysql(dest)
        return
    src = sqlite3.connect(str(db_path()))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def mysql_env() -> dict:
    """把密码放进 MYSQL_PWD，避免出现在进程命令行（ps 可见）里"""
    env = dict(os.environ)
    env["MYSQL_PWD"] = Config.DB_PASSWORD or ""
    return env


def dump_mysql(dest: Path) -> None:
    """mysqldump 导出一致性快照：单事务、不锁表、utf8mb4"""
    cmd = [
        "mysqldump",
        "-h", Config.DB_HOST or "127.0.0.1",
        "-P", str(Config.DB_PORT or 3306),
        "-u", Config.DB_USER,
        "--single-transaction",
        "--quick",
        "--no-tablespaces",
        "--set-gtid-purged=OFF",
        "--default-character-set=utf8mb4",
        Config.DB_NAME,
    ]
    try:
        with open(dest, "wb") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, env=mysql_env())
    except FileNotFoundError:
        raise OSError("服务器缺少 mysqldump 命令，请安装 mysql 客户端（yum install -y mysql）")
    if proc.returncode != 0:
        raise OSError(f"mysqldump 导出失败：{proc.stderr.decode('utf-8', 'ignore')[:300]}")


def restore_mysql(dump: Path) -> None:
    """把 SQL 快照灌回 MySQL（mysqldump 自带 DROP TABLE IF EXISTS，可重复执行）"""
    cmd = [
        "mysql",
        "-h", Config.DB_HOST or "127.0.0.1",
        "-P", str(Config.DB_PORT or 3306),
        "-u", Config.DB_USER,
        "--default-character-set=utf8mb4",
        Config.DB_NAME,
    ]
    with open(dump, "rb") as fh:
        proc = subprocess.run(cmd, stdin=fh, stderr=subprocess.PIPE, env=mysql_env())
    if proc.returncode != 0:
        raise OSError(f"mysql 导入失败：{proc.stderr.decode('utf-8', 'ignore')[:300]}")


# ============================== 打包 ==============================
def add_dir(zf: zipfile.ZipFile, folder: Path, arc_root: str) -> int:
    """把目录打进 zip，返回文件数"""
    if not folder.exists():
        return 0
    count = 0
    for path in folder.rglob("*"):
        if path.is_file():
            zf.write(path, f"{arc_root}/{path.relative_to(folder).as_posix()}")
            count += 1
    return count


def build_manifest() -> dict:
    return {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app": current_app.config.get("APP_NAME", "客户管理系统"),
        "backend": dbcompat.dialect_name(),
        "counts": {
            "customers": db.session.scalar(db.select(db.func.count(Customer.id))) or 0,
            "projects": db.session.scalar(db.select(db.func.count(Project.id))) or 0,
            "payments": db.session.scalar(db.select(db.func.count(Payment.id))) or 0,
            "attachments": db.session.scalar(db.select(db.func.count(Attachment.id))) or 0,
            "users": db.session.scalar(db.select(db.func.count(User.id))) or 0,
        },
    }


def create_backup(prefix: str = NAME_RE, note: str = "") -> Path:
    """生成一份备份 zip，返回文件路径（供路由与自动化调用）"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"{prefix}{stamp}.zip"
    manifest = build_manifest()
    if note:
        manifest["note"] = note

    member = db_member()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / member
        snapshot_db(tmp_db)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, member)
            manifest["files"] = {
                "uploads": add_dir(zf, Path(Config.UPLOAD_FOLDER), "uploads"),
                "avatars": add_dir(zf, Path(Config.AVATAR_FOLDER), "avatars"),
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return target


def list_backups() -> list[dict]:
    rows = []
    for path in sorted(backup_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        info = {"name": path.name, "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime), "manifest": {}}
        try:
            with zipfile.ZipFile(path) as zf:
                if "manifest.json" in zf.namelist():
                    info["manifest"] = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (zipfile.BadZipFile, ValueError, OSError):
            info["broken"] = True
        rows.append(info)
    return rows


def db_info() -> dict:
    """数据库概况：SQLite 显示文件，MySQL 显示连接目标与数据量"""
    if dbcompat.is_mysql():
        size = 0
        try:
            size = db.session.execute(db.text(
                "SELECT COALESCE(SUM(data_length + index_length), 0) "
                "FROM information_schema.tables WHERE table_schema = DATABASE()"
            )).scalar() or 0
        except Exception:
            pass
        return {
            "path": f"MySQL {Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}",
            "size": human_size(int(size)),
            "mtime": None,
        }
    path = db_path()
    size = path.stat().st_size if path.exists() else 0
    return {"path": str(path), "size": human_size(size),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else None}


# ============================== 恢复 ==============================
class _NullLock:
    """Windows / 单进程场景的空实现（桌面版只有一个进程，不需要跨进程互斥）"""

    def close(self):
        pass


def acquire_restore_lock():
    """恢复期间的跨进程互斥，拿不到锁返回 None。

    POSIX 用 flock 非阻塞独占；Windows 桌面版为单进程，直接返回空锁。
    这样做是因为恢复会替换库文件 / 灌 SQL，两个 worker 同时恢复会互相覆盖。
    """
    if os.name == "nt":
        return _NullLock()
    try:
        import fcntl
    except ImportError:                     # pragma: no cover - 极端裁剪的运行环境
        return _NullLock()
    path = Path(Config.DATA_DIR) / RESTORE_LOCK_NAME
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def check_archive_limits(zf: zipfile.ZipFile) -> str | None:
    """校验备份包规模，返回错误文案；None 表示通过"""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        return f"备份包条目过多（{len(infos)} 个），已拒绝恢复"
    total = sum(i.file_size for i in infos)
    if total > MAX_TOTAL_BYTES:
        return f"备份包解压后体积异常（{total // 1024 // 1024} MB），已拒绝恢复"
    return None


def restore_from_zip(path: Path) -> tuple[bool, str]:
    """用备份包恢复数据：校验 -> 预备份 -> 替换库与附件 -> 重跑 bootstrap"""
    lock = acquire_restore_lock()
    if lock is None:
        return False, "另一个恢复任务正在进行，请稍后再试"
    try:
        return _restore_locked(path)
    finally:
        lock.close()


def _restore_locked(path: Path) -> tuple[bool, str]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="crm-restore-"))
    member = db_member()
    other = SQLITE_MEMBER if member == MYSQL_MEMBER else MYSQL_MEMBER
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if member not in names:
                if other in names:
                    return False, ("备份包来自另一种数据库版本（"
                                   f"{other}），当前是 {member}，不能直接恢复")
                return False, f"备份包内缺少 {member}，可能不是本系统生成的备份"
            too_big = check_archive_limits(zf)
            if too_big:
                return False, too_big
            zf.extract(member, tmp_dir)
            # 附件与头像：覆盖同名文件，保留备份包里没有的现有文件
            for name in names:
                if name.startswith(("uploads/", "avatars/")) and not name.endswith("/"):
                    zf.extract(name, tmp_dir)

        new_db = tmp_dir / member
        if dbcompat.is_mysql():
            head = new_db.read_text(encoding="utf-8", errors="ignore")[:4000]
            if "CREATE TABLE" not in head.upper():
                return False, "备份包内的 SQL 快照不完整，找不到建表语句"
        else:
            conn = sqlite3.connect(str(new_db))
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            finally:
                conn.close()
            if not ok or ok[0] != "ok":
                return False, "备份包内的数据库文件已损坏，无法恢复"
            if not {"user", "customer", "project"}.issubset(tables):
                return False, "备份包内的数据库结构不完整，缺少核心表"

        # 恢复前先给当前数据留一份，误操作可回退
        create_backup(prefix="crm-before-restore-", note="恢复前的自动备份")

        db.session.remove()
        db.engine.dispose()
        if dbcompat.is_mysql():
            restore_mysql(new_db)
        else:
            target = db_path()
            try:
                os.replace(str(new_db), str(target))
            except OSError:
                shutil.copy2(str(new_db), str(target))

        for folder, arc in ((Path(Config.UPLOAD_FOLDER), "uploads"),
                            (Path(Config.AVATAR_FOLDER), "avatars")):
            src_dir = tmp_dir / arc
            if src_dir.exists():
                folder.mkdir(parents=True, exist_ok=True)
                for item in src_dir.rglob("*"):
                    if item.is_file():
                        dest = folder / item.relative_to(src_dir)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)

        # 老备份可能缺少新增列 / 字典项，恢复后统一补齐
        from bootstrap import bootstrap
        bootstrap(current_app._get_current_object())
        return True, "数据已恢复"
    except (zipfile.BadZipFile, OSError, sqlite3.DatabaseError) as exc:
        current_app.logger.exception("恢复失败")
        return False, f"恢复失败：{exc}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- 兼容别名：历史调用方使用下划线前缀的私有名 ----------
_backup_dir = backup_dir
_db_path = db_path
_db_member = db_member
_snapshot_db = snapshot_db
_manifest = build_manifest
_add_dir = add_dir
_list_backups = list_backups
_human = human_size
_db_info = db_info
_restore_from_zip = restore_from_zip
