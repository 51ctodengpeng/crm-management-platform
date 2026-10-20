# -*- coding: utf-8 -*-
"""数据库平滑迁移：为已有库补齐新增字段与新表，不丢数据。

SQLite 的 ALTER TABLE 只能 ADD COLUMN，本脚本先读 PRAGMA table_info 判断缺哪些列，
只补缺失部分，可重复执行（幂等）。字段清单集中在 bootstrap.py，避免两处维护。

用法：
    python migrate.py
"""
from app import app
from bootstrap import ensure_columns
from config import Config
from extensions import db


def main():
    with app.app_context():
        db.create_all()
        print("[1/3] 缺失的数据表已创建")

        stats = ensure_columns(verbose=True)
        print(f"[2/3] 新增字段 {stats['columns']} 个、索引 {stats['indexes']} 个（已存在的自动跳过）")
        if stats["legacy"]:
            print("      检测到旧版账号表：存量账号已统一归位为「已启用的管理员」")

        # 用 ORM 计数而不是手写 SQL：SQLite 认双引号标识符，MySQL 只认反引号
        from models import User
        fixed = db.session.scalar(
            db.select(db.func.count()).select_from(User).where(User.status == "active")
        ) or 0
        total = db.session.scalar(db.select(db.func.count()).select_from(User)) or 0
        print(f"[3/3] 账号状态归位完成：启用 {fixed} / 共 {total} 个")

        print(f"\n迁移完成。数据库：{Config.DB_PATH}")
        print("如仍有账号无法登录，请用： python init_db.py  或  flask create-admin 账号 密码")


if __name__ == "__main__":
    main()
