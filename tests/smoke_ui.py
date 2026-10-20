# -*- coding: utf-8 -*-
"""界面冒烟检查：登录后逐页取内容，验证页脚、权限页、批量条、三级地区等是否就位

    python tests/smoke_ui.py [base_url]
"""
import os
import re
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081"
ADMIN_USER = os.environ.get("CRM_TEST_ADMIN", "admin")
ADMIN_PASS = os.environ.get("CRM_TEST_PASS", "")
if not ADMIN_PASS:
    raise SystemExit("请先设置 CRM_TEST_PASS 环境变量（被测实例的管理员口令）")


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def main() -> int:
    session = requests.Session()
    page = session.get(f"{BASE}/login")
    session.post(
        f"{BASE}/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASS, "csrf_token": csrf(page.text)},
        allow_redirects=True,
    )

    home = session.get(f"{BASE}/")
    checks = [
        ("登录后进入首页", home.status_code == 200 and "经营概览" in home.text),
        ("页脚含技术支持团队", "PowerShell 中文社区团队" in home.text),
        ("官网为超链接", 'href="https://www.powershell.com.cn"' in home.text),
        ("页脚含邮箱链接", 'href="mailto:dengpeng@powershell.com.cn"' in home.text),
        ("侧边栏含权限管理", "/permission/" in home.text),
        ("侧边栏含个人资料", "/profile/" in home.text),
    ]

    for path, keyword in (
        ("/customer/", "客户管理"),
        ("/project/", "项目管理"),
        ("/permission/", "权限管理"),
        ("/profile/", "个人资料"),
        ("/user/", "账号管理"),
        ("/user/new", "新建账号"),
        ("/logs", "操作日志"),
        ("/change-password", "修改密码"),
    ):
        resp = session.get(f"{BASE}{path}")
        checks.append((f"{path} 可访问且渲染正确", resp.status_code == 200 and keyword in resp.text))

    customer_list = session.get(f"{BASE}/customer/")
    checks += [
        ("客户列表含批量表单", 'id="bulkForm"' in customer_list.text),
        ("客户列表含多选框", "row-check" in customer_list.text),
        ("客户列表含批量改等级", 'id="bulkLevel"' in customer_list.text),
        ("客户列表含批量改行业", 'id="bulkBusiness"' in customer_list.text),
        ("自定义弹窗脚本已加载", "js/dialog.js" in customer_list.text),
        ("删除确认走自定义弹窗", 'data-confirm="确认删除客户' in customer_list.text),
        ("模板中无原生 confirm", "return confirm(" not in customer_list.text),
        ("筛选表单自动生效", "data-auto-filter" in customer_list.text),
        ("筛选按钮已移除", "bi-funnel\"></i> 筛选" not in customer_list.text),
    ]

    project_list = session.get(f"{BASE}/project/")
    checks += [
        ("项目列表含批量表单", 'id="bulkForm"' in project_list.text),
        ("项目列表含批量改状态", 'id="bulkStatus"' in project_list.text),
        ("项目列表含批量改类型", 'id="bulkType"' in project_list.text),
        ("项目筛选自动生效", "data-auto-filter" in project_list.text),
    ]

    for path, label in (("/user/", "账号"), ("/logs", "日志"), ("/permission/", "权限")):
        page = session.get(f"{BASE}{path}")
        checks.append((f"{label}筛选自动生效", "data-auto-filter" in page.text))

    customer_form = session.get(f"{BASE}/customer/new")
    checks += [
        ("客户表单含三级地区", "region-picker" in customer_form.text and "regions.json" in customer_form.text),
        ("客户表单含行业自定义", "businessTypeCustom" in customer_form.text),
        ("客户表单含微信号字段", 'name="wechat"' in customer_form.text),
    ]

    project_form = session.get(f"{BASE}/project/new")
    checks += [
        ("项目表单含状态自定义", "projectStatusCustom" in project_form.text),
        ("项目表单状态含其他", ">其他<" in project_form.text),
    ]

    user_new = session.get(f"{BASE}/user/new")
    checks += [
        ("新建账号含微信号", 'name="wechat"' in user_new.text),
        ("新建账号含角色选择", 'name="role"' in user_new.text),
    ]

    failed = 0
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        failed += 0 if passed else 1
    print(f"\n通过 {len(checks) - failed} / {len(checks)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
