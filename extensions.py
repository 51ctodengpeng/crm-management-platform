# -*- coding: utf-8 -*-
"""扩展实例集中定义，避免循环导入"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "请先登录后再访问该页面"
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    from models import User
    return db.session.get(User, int(user_id))
