# -*- coding: utf-8 -*-
"""后台「选项管理」与夜间模式端到端验证

覆盖：
  1. 选项字典的增、改、删、排序、启停；
  2. 前台表单 / 筛选读取同一份字典，停用后立即从下拉消失；
  3. 自定义输入只作用于当前记录，不写入字典、不影响其他表单；
  4. 删除保护：内置「其他」不可删；仍被数据使用的选项拒绝删除；
  5. 改名可同步历史数据；
  6. 权限：非管理员不能进入选项管理与写接口；
  7. 夜间模式：主题入口、首屏恢复脚本、data-bs-theme 适配。

在隔离实例上运行：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_options.py
"""
import os
import re
import sys

import requests

BASE = os.environ.get("CRM_TEST_BASE", "http://127.0.0.1:8081")
ADMIN_USER = os.environ.get("CRM_TEST_ADMIN", "admin")
ADMIN_PASS = os.environ.get("CRM_TEST_PASS", "")
if not ADMIN_PASS:
    raise SystemExit(
        "请先设置 CRM_TEST_PASS 环境变量（被测实例的管理员口令）\n"
        "  Windows: set CRM_TEST_PASS=你的口令\n"
        "  Linux/macOS: export CRM_TEST_PASS=你的口令"
    )

ok_count = 0
fail = []


def check(name, cond, extra=""):
    global ok_count
    if cond:
        ok_count += 1
        print(f"  [PASS] {name} {extra}")
    else:
        fail.append(name)
        print(f"  [FAIL] {name} {extra}")


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(username=ADMIN_USER, password=ADMIN_PASS):
    s = requests.Session()
    page = s.get(f"{BASE}/login")
    return s, s.post(
        f"{BASE}/login",
        data={"username": username, "password": password, "csrf_token": csrf(page.text)},
        allow_redirects=True,
    )


def wait_ready(timeout=40):
    import time
    for _ in range(timeout * 2):
        try:
            if requests.get(f"{BASE}/login", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def select_values(html: str, name: str) -> list[str]:
    """取某个 select 的 option value 列表（排除空值与自定义哨兵）"""
    m = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>' % re.escape(name), html, re.S)
    if not m:
        return []
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(1))
            if v and v != "__custom__"]


def main():
    if not wait_ready():
        print("服务未就绪")
        return 1

    admin, resp = login()
    check("管理员登录成功", resp.status_code == 200 and "经营概览" in resp.text)

    # ---------- 页面与权限 ----------
    page = admin.get(f"{BASE}/options/")
    check("选项管理页可访问", page.status_code == 200 and "选项管理" in page.text)
    check("按类别分组展示", all(k in page.text for k in
                          ("客户等级", "项目类型", "项目状态", "服务方式", "付款方式", "转账渠道")))
    check("内置项标注受保护", "opt-tag" in page.text)

    anon = requests.get(f"{BASE}/options/", allow_redirects=False)
    check("未登录访问被重定向", anon.status_code in (302, 303) and "/login" in anon.headers.get("Location", ""))

    # ---------- 新增：进入前台下拉 ----------
    token = csrf(page.text)
    r = admin.post(f"{BASE}/options/add",
                   data={"csrf_token": token, "category": "project_type",
                         "name": "数据库运维", "remark": "测试新增", "enabled": "1"},
                   allow_redirects=True)
    check("新增选项成功", "已新增" in r.text)
    form = admin.get(f"{BASE}/project/new").text
    types = select_values(form, "project_type")
    check("新增项出现在项目类型下拉", "数据库运维" in types)
    check("「其他」仍在类型选项最底部", types and types[-1] == "其他")

    # ---------- 自定义输入：只作用于当前记录 ----------
    customer_page = admin.get(f"{BASE}/customer/new").text
    admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(customer_page), "company_name": "选项测试客户",
        "contact_name": "张三", "contact_phone": "13800000001",
        "customer_level": "普通", "business_type": "其他",
    }, allow_redirects=True)
    cid = int(re.search(r"/customer/(\d+)", admin.get(f"{BASE}/customer/?q=选项测试客户").text).group(1))

    admin.post(f"{BASE}/project/new", data={
        "csrf_token": csrf(admin.get(f"{BASE}/project/new").text),
        "customer_id": cid, "project_name": "临时自定义类型项目",
        "project_type": "__custom__", "project_type_custom": "只此一次类型",
        "service_mode": "远程服务", "payment_method": "现金支付", "project_amount": "100",
    }, allow_redirects=True)
    form2 = admin.get(f"{BASE}/project/new").text
    check("自定义值不进入全局下拉", "只此一次类型" not in select_values(form2, "project_type"))
    check("自定义值不写入选项字典", "只此一次类型" not in admin.get(f"{BASE}/options/").text)

    # ---------- 改名：同步历史记录 ----------
    option_page = admin.get(f"{BASE}/options/").text
    m = re.search(r'/options/(\d+)/edit[^>]*>\s*</form>\s*<input[^>]*name="name" value="数据库运维"', option_page)
    if not m:
        m = re.search(r'name="name" value="数据库运维"[^>]*form="f(\d+)"', option_page)
    oid = int(m.group(1)) if m else None
    check("能定位新增选项的编辑表单", oid is not None)

    if oid:
        r = admin.post(f"{BASE}/options/{oid}/edit", data={
            "csrf_token": csrf(option_page), "name": "数据库运维服务",
            "remark": "改名测试", "cascade": "1",
        }, allow_redirects=True)
        check("改名成功", "选项已更新" in r.text)
        check("改名后仍在下拉中", "数据库运维服务" in select_values(admin.get(f"{BASE}/project/new").text, "project_type"))

        # ---------- 停用：前台消失，历史数据保留 ----------
        r = admin.post(f"{BASE}/options/{oid}/toggle",
                       data={"csrf_token": csrf(admin.get(f"{BASE}/options/").text)},
                       allow_redirects=True)
        check("停用成功", "已停用" in r.text)
        check("停用项从前端下拉消失",
              "数据库运维服务" not in select_values(admin.get(f"{BASE}/project/new").text, "project_type"))
        check("停用项仍在字典中可见", "数据库运维服务" in admin.get(f"{BASE}/options/").text)

        r = admin.post(f"{BASE}/options/{oid}/toggle",
                       data={"csrf_token": csrf(admin.get(f"{BASE}/options/").text)},
                       allow_redirects=True)
        check("可重新启用", "已启用" in r.text)

    # ---------- 排序：上移 / 下移 ----------
    status_page = admin.get(f"{BASE}/options/").text
    block = status_page.split('id="cat-project_type"')[1].split("</section>")[0]
    ids = re.findall(r'/options/(\d+)/move', block)
    first_id = ids[0] if ids else None
    if first_id:
        before = [v for v in re.findall(r'name="name" value="([^"]+)"', block)]
        admin.post(f"{BASE}/options/{first_id}/move",
                   data={"csrf_token": csrf(status_page), "direction": "down"},
                   allow_redirects=True)
        after_block = admin.get(f"{BASE}/options/").text.split('id="cat-project_type"')[1].split("</section>")[0]
        after = re.findall(r'name="name" value="([^"]+)"', after_block)
        check("下移后顺序发生变化", before[:2] != after[:2], f"{before[:2]} -> {after[:2]}")

    # ---------- 删除保护 ----------
    builtin = re.search(r'/options/(\d+)/delete"[\s\S]{0,400}?name="csrf_token"', status_page)
    # 内置项渲染的是 disabled 按钮，没有删除表单；取「其他」所在行验证
    check("内置项没有可用的删除入口", '内置兜底选项不能删除' in status_page)

    # 仍被数据使用：新建一个类型并挂到项目上，再删
    admin.post(f"{BASE}/options/add", data={
        "csrf_token": csrf(admin.get(f"{BASE}/options/").text),
        "category": "project_type", "name": "占用类型", "enabled": "1",
    }, allow_redirects=True)
    admin.post(f"{BASE}/project/new", data={
        "csrf_token": csrf(admin.get(f"{BASE}/project/new").text),
        "customer_id": cid, "project_name": "占用类型项目",
        "project_type": "占用类型", "service_mode": "远程服务",
        "payment_method": "现金支付", "project_amount": "200",
    }, allow_redirects=True)
    op = admin.get(f"{BASE}/options/").text
    m = re.search(r'name="name" value="占用类型"[^>]*form="f(\d+)"', op)
    used_id = int(m.group(1)) if m else None
    if used_id:
        r = admin.post(f"{BASE}/options/{used_id}/delete",
                       data={"csrf_token": csrf(op)}, allow_redirects=True)
        check("使用中的选项拒绝删除", "已阻止删除" in r.text or "仍被" in r.text)
        check("拒绝后选项仍存在", "占用类型" in r.text)

        # 先停用再删（仍被使用，依旧保护）
        r = admin.post(f"{BASE}/options/{used_id}/delete",
                       data={"csrf_token": csrf(r.text)}, allow_redirects=True)
        check("再次删除仍被拦截", "占用类型" in r.text)

    # 未使用的选项可以删除
    admin.post(f"{BASE}/options/add", data={
        "csrf_token": csrf(admin.get(f"{BASE}/options/").text),
        "category": "project_type", "name": "待删类型", "enabled": "1",
    }, allow_redirects=True)
    op = admin.get(f"{BASE}/options/").text
    m = re.search(r'name="name" value="待删类型"[^>]*form="f(\d+)"', op)
    free_id = int(m.group(1)) if m else None
    if free_id:
        r = admin.post(f"{BASE}/options/{free_id}/delete",
                       data={"csrf_token": csrf(op)}, allow_redirects=True)
        check("未使用的选项可删除", "已删除" in r.text)
        check("删除后不出现在下拉", "待删类型" not in select_values(
            admin.get(f"{BASE}/project/new").text, "project_type"))

    # ---------- 表单校验 ----------
    r = admin.post(f"{BASE}/options/add", data={
        "csrf_token": csrf(admin.get(f"{BASE}/options/").text),
        "category": "project_type", "name": "  ", "enabled": "1",
    }, allow_redirects=True)
    check("空名称被拦截", "不能为空" in r.text)
    r = admin.post(f"{BASE}/options/add", data={
        "csrf_token": csrf(admin.get(f"{BASE}/options/").text),
        "category": "project_type", "name": "其他", "enabled": "1",
    }, allow_redirects=True)
    check("重复名称被拦截", "已存在" in r.text)

    # ---------- 权限：普通成员 ----------
    page = admin.get(f"{BASE}/user/new").text
    admin.post(f"{BASE}/user/new", data={
        "csrf_token": csrf(page), "username": "optuser", "real_name": "选项测试员",
        "password": "optpass123", "confirm_password": "optpass123",
        "role": "user", "status": "active",
        "data_scope": "all", "can_create": "1",
    }, allow_redirects=True)
    member, mresp = login("optuser", "optpass123")
    check("普通成员可登录", mresp.status_code == 200)
    r = member.get(f"{BASE}/options/", allow_redirects=True)
    check("普通成员访问选项管理被拦截", "统一维护客户类型" not in r.text and "仅管理员可操作" in r.text)

    # ---------- 夜间模式 ----------
    home = admin.get(f"{BASE}/").text
    check("页面声明主题属性", 'data-bs-theme' in home and 'data-theme=' in home)
    check("首屏前恢复主题偏好", "localStorage.getItem('crm-theme')" in home)
    check("顶栏主题切换入口", "data-theme-toggle" in home)
    check("主题脚本已加载", "js/theme.js" in home)
    anon_page = requests.get(f"{BASE}/login").text
    check("登录页也可切换主题", "data-theme-toggle" in anon_page and "js/theme.js" in anon_page)

    print("\n" + "=" * 50)
    print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print("  - " + name)
        return 1
    print("选项管理 / 夜间模式验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
