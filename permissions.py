# -*- coding: utf-8 -*-
"""权限判定层：角色（Role）+ 数据范围（Scope）+ 项目级操作（Action）

参考同类企业管理系统的通用做法（RBAC 负责"谁能进哪个功能"，ACL 负责"能看哪些数据"）：

1. **角色 role**
   - ``admin`` 系统管理员：全部数据、全部操作，不受项目授权约束；
   - ``user``  普通成员：功能入口受限（账号管理、权限配置仅管理员），数据受项目授权约束。
2. **数据范围 data_scope**
   - ``all``      全部数据：视为该成员拥有全局可见范围（仍不能进管理员专属功能）；
   - ``assigned`` 仅授权项目：只能看到被显式授权的项目及其所属客户。
3. **操作类型 action**（项目粒度，向下包含）
   - ``view``   可见：列表 / 详情可见，可下载与预览附件；
   - ``edit``   可修改：编辑项目与客户信息、上传附件、改附件备注（隐含 view）；
   - ``delete`` 可删除：删除项目与附件（隐含 edit 与 view，属风险操作）。

所有判定集中在本模块，路由与模板只调用这里的函数，避免"页面隐藏了按钮但接口没拦截"的越权漏洞。
"""
from functools import wraps

from flask import abort, flash, g, redirect, request, url_for
from flask_login import current_user
from sqlalchemy import select

from extensions import db
from models import Customer, Project, ProjectPermission

PERM_VIEW = "view"
PERM_EDIT = "edit"
PERM_DELETE = "delete"

# 每个操作位对应 ProjectPermission 的列，便于按需拼接 SQL
_PERM_COLUMNS = {
    PERM_VIEW: ProjectPermission.can_view,
    PERM_EDIT: ProjectPermission.can_edit,
    PERM_DELETE: ProjectPermission.can_delete,
}

_ACTION_LABELS = {
    PERM_VIEW: "查看",
    PERM_EDIT: "修改",
    PERM_DELETE: "删除",
}


# ============================== 基础判定 ==============================
def _resolve(user=None):
    return user if user is not None else current_user


def is_authenticated(user=None) -> bool:
    return bool(getattr(_resolve(user), "is_authenticated", False))


def is_admin(user=None) -> bool:
    u = _resolve(user)
    return is_authenticated(u) and bool(getattr(u, "is_admin", False))


def has_full_scope(user=None) -> bool:
    """是否拥有全部数据范围（管理员或 data_scope=all）"""
    u = _resolve(user)
    if not is_authenticated(u):
        return False
    return bool(getattr(u, "is_admin", False)) or (getattr(u, "data_scope", "assigned") or "assigned") == "all"


def can_create_data(user=None) -> bool:
    """是否允许新增客户 / 项目（项目级授权只作用于已存在的数据）"""
    u = _resolve(user)
    if not is_authenticated(u):
        return False
    return bool(getattr(u, "is_admin", False)) or bool(getattr(u, "can_create", False))


def scope_summary(user=None) -> str:
    u = _resolve(user)
    if not is_authenticated(u):
        return "未登录"
    if has_full_scope(u):
        return "全部数据"
    return f"仅授权项目（{len(visible_project_ids(u))} 个）"


# ============================== 项目级授权 ==============================
def permission_map(user=None) -> dict[int, ProjectPermission]:
    """当前用户的项目授权字典 {project_id: ProjectPermission}，请求内缓存避免重复查询"""
    u = _resolve(user)
    if not is_authenticated(u) or has_full_scope(u):
        return {}
    cache_key = f"_proj_perm_map_{u.id}"
    cached = g.get(cache_key)
    if cached is None:
        rows = db.session.scalars(
            select(ProjectPermission).where(ProjectPermission.user_id == u.id)
        ).all()
        cached = {r.project_id: r for r in rows}
        setattr(g, cache_key, cached)
    return cached


def clear_permission_cache(user=None):
    """授权变更后清理缓存（同一请求内立即生效）"""
    u = _resolve(user)
    if is_authenticated(u):
        setattr(g, f"_proj_perm_map_{u.id}", None)


def _pid(project) -> int | None:
    if project is None:
        return None
    return project if isinstance(project, int) else getattr(project, "id", None)


def has_project_permission(project, action: str = PERM_VIEW, user=None) -> bool:
    """单个项目是否具备某操作权限"""
    if not is_authenticated(user):
        return False
    if has_full_scope(user):
        return True
    pid = _pid(project)
    if not pid:
        return False
    perm = permission_map(user).get(pid)
    if perm is None:
        return False
    return bool(getattr(perm, f"can_{action}", False))


def can_view_project(project, user=None) -> bool:
    return has_project_permission(project, PERM_VIEW, user)


def can_edit_project(project, user=None) -> bool:
    return has_project_permission(project, PERM_EDIT, user)


def can_delete_project(project, user=None) -> bool:
    return has_project_permission(project, PERM_DELETE, user)


def visible_project_ids(user=None) -> set[int]:
    """可见项目 ID 集合；拥有全量范围时返回全部项目 ID"""
    u = _resolve(user)
    if has_full_scope(u):
        return set(db.session.scalars(select(Project.id)).all())
    return {
        pid for pid, perm in permission_map(u).items() if perm.can_view
    }


def apply_project_scope(stmt, action: str = PERM_VIEW, user=None):
    """给项目查询追加数据范围条件（纯 SQL 子查询，不加载 ID 列表）"""
    u = _resolve(user)
    if has_full_scope(u):
        return stmt
    column = _PERM_COLUMNS.get(action, ProjectPermission.can_view)
    sub = select(ProjectPermission.project_id).where(
        ProjectPermission.user_id == u.id, column.is_(True)
    )
    return stmt.where(Project.id.in_(sub))


# ============================== 客户级判定 ==============================
def _customer_scoped(customer, action: str = PERM_VIEW, user=None) -> bool:
    """客户是否在范围内：其名下存在具备该操作权限的项目"""
    u = _resolve(user)
    if has_full_scope(u):
        return True
    cid = _pid(customer)
    if not cid:
        return False
    column = _PERM_COLUMNS.get(action, ProjectPermission.can_view)
    stmt = (
        select(ProjectPermission.id)
        .join(Project, Project.id == ProjectPermission.project_id)
        .where(
            ProjectPermission.user_id == u.id,
            column.is_(True),
            Project.customer_id == cid,
        )
        .limit(1)
    )
    return db.session.scalar(stmt) is not None


def can_view_customer(customer, user=None) -> bool:
    return _customer_scoped(customer, PERM_VIEW, user)


def can_edit_customer(customer, user=None) -> bool:
    return _customer_scoped(customer, PERM_EDIT, user)


def can_delete_customer(customer, user=None) -> bool:
    """删除客户会连带删除其名下全部项目（可能包含未授权项目），因此只允许全量范围账号操作"""
    return has_full_scope(user)


def apply_customer_scope(stmt, user=None):
    """给客户查询追加数据范围条件"""
    u = _resolve(user)
    if has_full_scope(u):
        return stmt
    sub_projects = (
        select(ProjectPermission.project_id)
        .where(ProjectPermission.user_id == u.id, ProjectPermission.can_view.is_(True))
    )
    sub_customers = select(Project.customer_id).where(Project.id.in_(sub_projects))
    return stmt.where(Customer.id.in_(sub_customers))


# ============================== 视图装饰器 ==============================
def project_access_required(action: str = PERM_VIEW, arg: str = "pid"):
    """校验 URL 中的项目 ID（默认 pid）是否具备指定权限，未通过则提示并回到列表页"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            pid = kwargs.get(arg)
            project = db.session.get(Project, pid) if pid else None
            if project is None:
                abort(404)
            if not has_project_permission(project, action):
                flash(
                    f"没有该项目的{_ACTION_LABELS.get(action, '访问')}权限，"
                    "如需开通请联系管理员在「权限管理」中授权",
                    "danger",
                )
                return redirect(url_for("project.list_projects"))
            return func(*args, **kwargs)
        return wrapper
    return decorator


def customer_access_required(action: str = PERM_VIEW, arg: str = "cid"):
    """校验 URL 中的客户 ID（默认 cid）是否在数据范围内"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cid = kwargs.get(arg)
            customer = db.session.get(Customer, cid) if cid else None
            if customer is None:
                abort(404)
            allowed = (
                can_edit_customer(customer) if action == PERM_EDIT
                else can_delete_customer(customer) if action == PERM_DELETE
                else can_view_customer(customer)
            )
            if not allowed:
                flash("没有该客户的访问权限，如需开通请联系管理员", "danger")
                return redirect(url_for("customer.list_customers"))
            return func(*args, **kwargs)
        return wrapper
    return decorator
