# -*- coding: utf-8 -*-
"""新增功能端到端验证：项目级权限、批量操作、头像、三级地区、自定义选项、账号名修改

在隔离实例上运行（推荐）：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_features.py
"""
import base64
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

# 1x1 透明 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


def login(username=ADMIN_USER, password=ADMIN_PASS, session=None):
    s = session or requests.Session()
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


def main():
    if not wait_ready():
        print("实例未就绪")
        return 1

    admin, _ = login()

    # ---------- 准备客户与两个项目 ----------
    page = admin.get(f"{BASE}/customer/new")
    admin.post(
        f"{BASE}/customer/new",
        data={
            "csrf_token": csrf(page.text), "company_name": "权限验证客户",
            "customer_level": "普通", "province": "广东省", "city": "深圳市",
            "district": "南山区", "address": "科技园 1 号",
        },
        allow_redirects=True,
    )
    customer_page = admin.get(f"{BASE}/customer/?q=权限验证客户")
    cid = int(re.search(r"/customer/(\d+)", customer_page.text).group(1))
    detail = admin.get(f"{BASE}/customer/{cid}")
    check("三级地区保存为省市区", "广东省 深圳市 南山区" in detail.text)

    pids = []
    for name in ("权限项目A", "权限项目B"):
        page = admin.get(f"{BASE}/project/new")
        r = admin.post(
            f"{BASE}/project/new",
            data={
                "csrf_token": csrf(page.text), "customer_id": cid, "project_name": name,
                "project_type": "网络", "project_status": "__custom__",
                "project_status_custom": "待收款", "project_amount": "1000",
            },
            allow_redirects=False,
        )
        pids.append(int(re.search(r"/project/(\d+)", r.headers.get("Location", "")).group(1)))
    pid_a, pid_b = pids

    detail = admin.get(f"{BASE}/project/{pid_a}")
    check("自定义项目状态生效", "待收款" in detail.text)
    project_list = admin.get(f"{BASE}/project/")
    check("自定义状态不写入全局候选", 'value="待收款"' not in project_list.text)

    # ---------- 管理员创建普通账号 ----------
    page = admin.get(f"{BASE}/user/new")
    r = admin.post(
        f"{BASE}/user/new",
        data={
            "csrf_token": csrf(page.text), "username": "permuser", "real_name": "权限测试",
            "password": "test123456", "confirm_password": "test123456",
            "role": "user", "status": "active", "data_scope": "assigned",
        },
        allow_redirects=False,
    )
    check("管理员创建账号成功", r.status_code == 302)
    uid = int(re.search(r"/user/(\d+)", r.headers.get("Location", "")).group(1))

    member, r = login("permuser", "test123456")
    check("新账号可直接登录", "经营概览" in r.text)
    check("未授权时项目列表为空", "还没有项目记录" in member.get(f"{BASE}/project/").text)
    r = member.get(f"{BASE}/project/{pid_a}", allow_redirects=False)
    check("未授权项目详情被拦截", r.status_code == 302)

    # ---------- 授予「查看」 ----------
    page = admin.get(f"{BASE}/permission/{uid}")
    admin.post(
        f"{BASE}/permission/{uid}",
        data={"csrf_token": csrf(page.text), "data_scope": "assigned",
              f"p{pid_a}_view": "1"},
        allow_redirects=True,
    )
    listing = member.get(f"{BASE}/project/")
    check("授权后可看到项目A", "权限项目A" in listing.text)
    check("未授权项目B仍不可见", "权限项目B" not in listing.text)
    check("仅查看可进详情", member.get(f"{BASE}/project/{pid_a}").status_code == 200)
    r = member.get(f"{BASE}/project/{pid_a}/edit", allow_redirects=False)
    check("仅查看不能编辑", r.status_code == 302)
    check("仅查看页面无编辑按钮", "编辑项目" not in member.get(f"{BASE}/project/{pid_a}").text)

    # ---------- 升级为「可修改」 ----------
    page = admin.get(f"{BASE}/permission/{uid}")
    admin.post(
        f"{BASE}/permission/{uid}",
        data={"csrf_token": csrf(page.text), "data_scope": "assigned",
              f"p{pid_a}_edit": "1"},
        allow_redirects=True,
    )
    r = member.get(f"{BASE}/project/{pid_a}/edit")
    check("授予修改后可进入编辑页", r.status_code == 200)

    # ---------- 批量操作：只影响有权限的记录 ----------
    page = member.get(f"{BASE}/project/")
    r = member.post(
        f"{BASE}/project/batch",
        data={"csrf_token": csrf(page.text), "action": "status", "value": "已验收",
              "ids": [pid_a, pid_b]},
        allow_redirects=True,
    )
    check("批量修改执行成功", "已将 1 个项目的状态改为" in r.text)
    check("无权限记录被跳过", "跳过" in r.text)
    check("有权限记录状态已更新", "已验收" in member.get(f"{BASE}/project/{pid_a}").text)

    # ---------- 删除权限 ----------
    r = member.post(
        f"{BASE}/project/{pid_b}/delete",
        data={"csrf_token": csrf(member.get(f"{BASE}/project/{pid_a}").text)},
        allow_redirects=True,
    )
    check("无删除权限时项目仍存在", admin.get(f"{BASE}/project/{pid_b}").status_code == 200)

    # ---------- 头像 ----------
    page = member.get(f"{BASE}/profile/")
    check("个人资料页含头像上传", 'name="avatar"' in page.text)
    r = member.post(
        f"{BASE}/profile/avatar",
        data={"csrf_token": csrf(page.text)},
        files={"avatar": ("me.png", PNG, "image/png")},
        allow_redirects=True,
    )
    check("头像上传成功", "头像已更新" in r.text)
    page = member.get(f"{BASE}/profile/")
    m = re.search(r'/profile/avatar/([0-9a-f]{32}\.png)', page.text)
    check("头像已渲染到页面", bool(m))
    if m:
        check("头像文件可访问", member.get(f"{BASE}/profile/avatar/{m.group(1)}").status_code == 200)

    r = member.post(
        f"{BASE}/profile/avatar",
        data={"csrf_token": csrf(page.text)},
        files={"avatar": ("bad.txt", b"hello", "text/plain")},
        allow_redirects=True,
    )
    check("非图片头像被拒绝", "仅支持" in r.text)

    # ---------- 微信号 ----------
    page = member.get(f"{BASE}/profile/")
    member.post(
        f"{BASE}/profile/",
        data={"csrf_token": csrf(page.text), "real_name": "权限测试",
              "wechat": "perm_wx_001", "phone": "13800000000", "email": "a@b.com",
              "department": "测试部"},
        allow_redirects=True,
    )
    check("微信号保存成功", "perm_wx_001" in member.get(f"{BASE}/profile/").text)

    # ---------- 修改账号名 ----------
    page = admin.get(f"{BASE}/user/{uid}")
    r = admin.post(
        f"{BASE}/user/{uid}/profile",
        data={"csrf_token": csrf(page.text), "username": "permuser2",
              "real_name": "权限测试", "wechat": "perm_wx_001",
              "phone": "", "email": "", "department": "测试部"},
        allow_redirects=True,
    )
    check("账号名修改成功", "permuser2" in r.text)
    _, r2 = login("permuser2", "test123456")
    check("新账号名可登录", "经营概览" in r2.text)
    _, r3 = login("permuser", "test123456")
    check("旧账号名已失效", "经营概览" not in r3.text)

    # ---------- 批量客户编辑（管理员） ----------
    page = admin.get(f"{BASE}/customer/")
    r = admin.post(
        f"{BASE}/customer/batch",
        data={"csrf_token": csrf(page.text), "action": "level", "value": "重要",
              "ids": [cid]},
        allow_redirects=True,
    )
    check("批量修改客户等级成功", "已将 1 个客户的客户等级改为" in r.text)
    check("客户等级已更新", "重要" in admin.get(f"{BASE}/customer/{cid}").text)


    # ---------- 含税价格 + 税点：正算 / 反算 / 税点 0 ----------
    def save_project(pid, **over):
        page = admin.get(f"{BASE}/project/{pid}/edit")
        data = {
            "csrf_token": csrf(page.text), "customer_id": cid,
            "project_name": "权限项目A", "project_type": "网络",
            "project_status": "已验收", "service_mode": "现场服务",
            "payment_method": "支付软件转账",
        }
        data.update(over)
        return admin.post(f"{BASE}/project/{pid}/edit", data=data, allow_redirects=True)

    save_project(pid_a, tax_rate="6", project_amount="10000",
                 tax_included_amount="10600", amount_basis="net")
    d = admin.get(f"{BASE}/project/{pid_a}")
    check("正算：10000 含税 6% -> 10600", "10,600.00" in d.text)
    check("服务方式保存成功", "现场服务" in d.text)
    check("付款方式保存成功", "支付软件转账" in d.text)

    save_project(pid_a, tax_rate="13", project_amount="",
                 tax_included_amount="11300", amount_basis="gross")
    d = admin.get(f"{BASE}/project/{pid_a}")
    check("反算：11300 含税 13% -> 10000", "10,000.00" in d.text)
    check("反算后含税价保持 11300", "11,300.00" in d.text)

    save_project(pid_a, tax_rate="0", project_amount="500",
                 tax_included_amount="", amount_basis="net")
    d = admin.get(f"{BASE}/project/{pid_a}")
    check("税点 0 时含税价等于合同金额", d.text.count("500.00") >= 2 and "税点 0%" in d.text)

    # ---------- 收款登记与对账 ----------
    d = admin.get(f"{BASE}/project/{pid_a}")
    r = admin.post(
        f"{BASE}/finance/add/{pid_a}",
        data={"csrf_token": csrf(d.text), "amount": "500",
              "pay_method": "支付软件转账", "pay_channel": "微信",
              "trade_no": "TEST-TX-0001", "payer": "权限验证客户"},
        allow_redirects=True,
    )
    check("登记转账收款成功", "已登记收款" in r.text)

    d = admin.get(f"{BASE}/project/{pid_a}")
    m = re.search(r"/finance/(\d+)/reconcile", d.text)
    check("待对账记录出现在项目详情", bool(m))
    check("未对账时不计入已收", "未收款" in d.text or "部分收款" in d.text)

    # 登记时不填流水号应被拒绝
    d = admin.get(f"{BASE}/project/{pid_a}")
    r = admin.post(
        f"{BASE}/finance/add/{pid_a}",
        data={"csrf_token": csrf(d.text), "amount": "10",
              "pay_method": "支付软件转账", "trade_no": ""},
        allow_redirects=True,
    )
    check("转账缺少流水号被拒绝", "交易流水号" in r.text)

    if m:
        pay_id = int(m.group(1))
        d = admin.get(f"{BASE}/project/{pid_a}")
        r = admin.post(f"{BASE}/finance/{pay_id}/reconcile",
                       data={"csrf_token": csrf(d.text)}, allow_redirects=True)
        check("对账成功", "对账成功" in r.text)
        check("对账后状态为已结清", "已结清" in admin.get(f"{BASE}/project/{pid_a}").text)

        d = admin.get(f"{BASE}/project/{pid_a}")
        r = admin.post(f"{BASE}/finance/{pay_id}/reconcile",
                       data={"csrf_token": csrf(d.text)}, allow_redirects=True)
        check("重复对账被拦截", "已对账" in r.text)

    # 超收拦截：应收 500 已收 500，再登记 100 并对账应被拒绝
    d = admin.get(f"{BASE}/project/{pid_a}")
    admin.post(
        f"{BASE}/finance/add/{pid_a}",
        data={"csrf_token": csrf(d.text), "amount": "100", "pay_method": "现金支付"},
        allow_redirects=True,
    )
    d = admin.get(f"{BASE}/project/{pid_a}")
    m2 = re.search(r"/finance/(\d+)/reconcile", d.text)
    if m2:
        over_id = int(m2.group(1))
        r = admin.post(f"{BASE}/finance/{over_id}/reconcile",
                       data={"csrf_token": csrf(d.text)}, allow_redirects=True)
        check("超收对账被拦截", "超出应收" in r.text)

    r = admin.get(f"{BASE}/finance/")
    check("对账中心可访问", r.status_code == 200 and "收款对账" in r.text)
    check("对账中心显示流水号", "TEST-TX-0001" in r.text)

    # ---------- 项目类型：自定义输入 + 「其他」固定置底 ----------
    page = admin.get(f"{BASE}/project/new")
    admin.post(
        f"{BASE}/project/new",
        data={"csrf_token": csrf(page.text), "customer_id": cid,
              "project_name": "自定义类型项目", "project_type": "__custom__",
              "project_type_custom": "数据库运维", "service_mode": "远程服务",
              "payment_method": "现金支付", "project_amount": "100"},
        allow_redirects=True,
    )
    h = admin.get(f"{BASE}/project/new").text
    block = h.split('name="project_type"')[1].split("</select>")[0]
    values = [v for v in re.findall(r'<option value="([^"]*)"', block)
              if v and v != "__custom__"]
    check("自定义项目类型不写入全局候选", "数据库运维" not in values)
    check("「其他」固定在类型选项最底部", values and values[-1] == "其他")

    # ---------- 布局：数据与安全沉底 + 导出二次确认 ----------
    h = admin.get(f"{BASE}/project/").text
    check("数据与安全沉到侧边栏底部", 'class="nav-bottom"' in h)
    check("导出按钮带二次确认", 'data-confirm-template="#tplExportProjects"' in h)
    check("确认弹窗说明导出影响", "本次导出会写入操作日志" in h)

    anon = requests.get(f"{BASE}/login").text
    check("登录页右上角展示开发团队", "auth-team-badge" in anon)
    check("登录页底部备案式信息", "auth-footer" in anon and "www.powershell.com.cn" in anon)
    check("登录页含技术支持邮箱", "dengpeng@powershell.com.cn" in anon)

    print("\n" + "=" * 50)
    print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print("  - " + name)
        return 1
    print("新增功能验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
