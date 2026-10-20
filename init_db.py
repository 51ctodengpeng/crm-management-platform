# -*- coding: utf-8 -*-
"""初始化脚本：建表 + 创建管理员账号 + 生成目录

用法：
    python init_db.py                  # 默认 admin / admin123
    python init_db.py 账号 密码         # 自定义账号密码
"""
import sys

from app import app
from config import Config
from extensions import db
from models import User


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    password = sys.argv[2] if len(sys.argv) > 2 else "admin123"

    Config.ensure_dirs()

    with app.app_context():
        db.create_all()
        print(f"[1/3] 数据库表已创建 -> {Config.DB_PATH}")

        exists = db.session.scalar(db.select(User).where(User.username == username))
        if exists:
            # 兜底：确保该账号是可直接登录的管理员
            changed = []
            if exists.role != "admin":
                exists.role = "admin"
                changed.append("role=admin")
            if exists.status != "active":
                exists.status = "active"
                changed.append("status=active")
            if not exists.real_name:
                exists.real_name = username
                changed.append("补全姓名")
            if changed:
                db.session.commit()
                print(f"[2/3] 用户 {username} 已存在，已修正：{'、'.join(changed)}")
            else:
                print(f"[2/3] 用户 {username} 已存在，跳过创建")
        else:
            user = User(username=username, real_name=username, role="admin", status="active")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            print(f"[2/3] 管理员已创建：{username} / {password}（角色=管理员，状态=已启用）")

        print(f"[3/3] 附件目录已就绪 -> {Config.UPLOAD_FOLDER}")
        print("\n初始化完成，启动命令：python app.py  然后访问 http://127.0.0.1:8081")


if __name__ == "__main__":
    main()
