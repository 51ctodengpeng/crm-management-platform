# -*- coding: utf-8 -*-
"""业务增强验证：客户多联系人、列表排序与区间筛选、日志按对象/时间筛选、
详情页内嵌操作记录

在隔离实例上运行（推荐）：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_extra.py
"""
import os
import re
import sys

import requests

BASE = os.environ.get("CRM_TEST_BASE", "http://127.0.0.1:8081")
ADMIN_USER = os.environ.get("CRM_TEST_ADMIN", "admin")
ADMIN_PASS = os.environ.get("CRM_TEST_PASS", "admin123")

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
    s.trust_env = False
    page = s.get(f"{BASE}/login")
    return s, s.post(f"{BASE}/login", data={
        "username": username, "password": password, "csrf_token": csrf(page.text)},
        allow_redirects=True)


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


def main():
    if not wait_ready():
        print("实例未就绪")
        return 1

    admin, r = login()
    if "经营概览" not in r.text:
        print("管理员登录失败")
        return 1

    # ==================== 1. 多联系人 ====================
    print("== 1. 客户多联系人 ==")
    page = admin.get(f"{BASE}/customer/new")
    admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(page.text), "company_name": "多联系人测试公司",
        "contact_name": "张三", "position": "技术总监",
        "contact_phone": "13600000001", "contact_email": "zs@example.com"},
        allow_redirects=True)
    listing = admin.get(f"{BASE}/customer/?q=多联系人测试公司")
    cid = int(re.search(r"/customer/(\d+)", listing.text).group(1))
    detail = admin.get(f"{BASE}/customer/{cid}")
    check("表单录入的对接人成为主联系人", "张三" in detail.text and "主联系人" in detail.text)
    check("同名字段同步到客户主档", "13600000001" in detail.text)
    check("详情页有联系人区块", 'id="contacts"' in detail.text)

    # 新增第二个联系人
    r = admin.post(f"{BASE}/customer/{cid}/contact/add", data={
        "csrf_token": csrf(detail.text), "name": "李四", "position": "财务经理",
        "phone": "13600000002", "email": "ls@example.com", "remark": "负责付款"},
        allow_redirects=True)
    check("新增联系人成功", "已添加联系人 李四" in r.text)
    check("列表显示两个联系人", "张三" in r.text and "李四" in r.text)
    detail = admin.get(f"{BASE}/customer/{cid}")
    check("主联系人仍为张三", re.search(r"张三.*?主联系人", detail.text, re.S) is not None)

    # 切换主联系人
    m = re.search(r'/customer/' + str(cid) + r'/contact/(\d+)/primary', detail.text)
    check("存在设为主联系人入口", bool(m))
    if m:
        r = admin.post(f"{BASE}/customer/{cid}/contact/{m.group(1)}/primary",
                       data={"csrf_token": csrf(detail.text)}, allow_redirects=True)
        check("切换主联系人成功", "设为主联系人" in r.text)
        page_html = admin.get(f"{BASE}/customer/{cid}").text
        # 主档字段应同步为李四
        check("主档对接人同步为李四", re.search(r"对接人.*?李四", page_html, re.S) is not None)
        check("主档电话同步", "13600000002" in page_html)
        # 主联系人徽标必须紧跟在李四名字后，且不再跟张三
        check("新主联系人带徽标",
              re.search(r"李四\s*<span class=\"badge-soft blue\">主联系人", page_html) is not None)
        check("原主联系人不再是主联系人",
              re.search(r"张三\s*<span class=\"badge-soft blue\">主联系人", page_html) is None)

    # 删除联系人
    detail = admin.get(f"{BASE}/customer/{cid}")
    m = re.search(r'/customer/' + str(cid) + r'/contact/(\d+)/delete', detail.text)
    if m:
        r = admin.post(f"{BASE}/customer/{cid}/contact/{m.group(1)}/delete",
                       data={"csrf_token": csrf(detail.text)}, allow_redirects=True)
        check("删除联系人成功", "已删除联系人" in r.text)

    # ==================== 2. 客户列表排序与区间 ====================
    print("== 2. 客户列表排序与日期区间 ==")
    r = admin.get(f"{BASE}/customer/?sort=name&order=asc")
    check("按名称升序可访问", r.status_code == 200)
    check("排序控件回显", 'value="name" selected' in r.text or "按公司名称" in r.text)
    body = r.text
    names = re.findall(r'class="company-link"[^>]*>([^<]+)<', body)
    if len(names) >= 2:
        import locale
        check("名称确实升序", names == sorted(names, key=lambda x: x.lower()))
    r = admin.get(f"{BASE}/customer/?created_from=2000-01-01&created_to=2000-12-31")
    check("创建时间区间可筛选（历史区间为空）", "共 0 位客户" in r.text)
    r = admin.get(f"{BASE}/customer/?created_from=2020-01-01&created_to=2099-12-31")
    check("创建时间区间包含今天", "多联系人测试公司" in r.text)

    # ==================== 3. 项目列表排序与金额区间 ====================
    print("== 3. 项目列表排序与金额区间 ==")
    r = admin.get(f"{BASE}/project/?sort=amount&order=desc")
    check("按金额降序可访问", r.status_code == 200)
    check("金额排序控件就位", 'value="amount" selected' in r.text)
    r = admin.get(f"{BASE}/project/?amount_min=999999999")
    check("金额下限生效", "共 0 个项目" in r.text)
    r = admin.get(f"{BASE}/project/?amount_min=1&amount_max=999999999")
    check("金额区间有结果", "共 0 个项目" not in r.text)
    r = admin.get(f"{BASE}/project/?end_from=2000-01-01&end_to=2000-12-31")
    check("结束日期区间生效", "共 0 个项目" in r.text)
    r = admin.get(f"{BASE}/project/?sort=end_date&order=asc&amount_min=1")
    check("按结束日期排序可访问", r.status_code == 200)

    # 导出带筛选参数
    r = admin.get(f"{BASE}/export/projects.xlsx?amount_min=1&sort=amount&order=desc")
    check("导出接受排序与区间参数", r.status_code == 200 and r.content[:2] == b"PK")

    # ==================== 4. 日志筛选 ====================
    print("== 4. 操作日志筛选 ==")
    r = admin.get(f"{BASE}/logs")
    check("日志页有对象类型筛选", 'name="target_type"' in r.text)
    check("日志页有日期区间", 'name="date_from"' in r.text and 'name="date_to"' in r.text)
    check("管理员有操作人筛选", 'name="operator"' in r.text)

    r = admin.get(f"{BASE}/logs?target_type=客户&target_id={cid}")
    check("按对象过滤只显示该客户", "多联系人测试公司" in r.text and "对象 ID" in r.text)
    r = admin.get(f"{BASE}/logs?date_from=2000-01-01&date_to=2000-01-02")
    check("历史时间区间无记录", "暂无日志记录" in r.text)
    r = admin.get(f"{BASE}/logs?operator={ADMIN_USER}")
    check("按操作人筛选有结果", "暂无日志记录" not in r.text)

    # ==================== 5. 详情页内嵌操作记录 ====================
    print("== 5. 详情页内嵌操作记录 ==")
    detail = admin.get(f"{BASE}/customer/{cid}")
    check("客户详情有最近操作卡", "最近操作" in detail.text)
    check("客户操作记录含新增联系人", "新增联系人" in detail.text or "主联系人" in detail.text)
    check("可跳转到该客户的完整日志", f"target_id={cid}" in detail.text)

    # 项目侧
    cust = admin.get(f"{BASE}/customer/?q=多联系人测试公司").text
    pid = None
    r = admin.get(f"{BASE}/project/?q=多联系人测试公司")
    # 建一个项目以便验证
    page = admin.get(f"{BASE}/project/new")
    admin.post(f"{BASE}/project/new", data={
        "csrf_token": csrf(page.text), "customer_id": str(cid), "project_name": "操作记录验证项目",
        "project_type": "虚拟化", "tax_rate": "6", "amount_basis": "gross",
        "tax_included_amount": "10600", "start_date": "2026-01-01",
        "end_date": "2026-12-31", "project_status": "进行中"}, allow_redirects=True)
    plist = admin.get(f"{BASE}/project/?q=操作记录验证项目").text
    m = re.search(r"/project/(\d+)", plist)
    check("验证项目创建成功", bool(m))
    if m:
        pid = int(m.group(1))
        pdetail = admin.get(f"{BASE}/project/{pid}").text
        check("项目详情有操作记录卡", 'id="oplogs"' in pdetail)
        check("项目操作记录含新增日志", "新增项目" in pdetail)
        check("项目可跳转完整日志", f"target_id={pid}" in pdetail)

    print("\n" + "=" * 50)
    print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print("  - " + name)
        return 1
    print("业务增强验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
