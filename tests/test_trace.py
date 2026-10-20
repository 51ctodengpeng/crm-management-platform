# -*- coding: utf-8 -*-
"""溯源码与省市区结构化验证

覆盖两块能力，重点确认「新增字段不影响既有字段」：
1. 操作日志的来源信息：客户端类型识别、筛选、旧数据兼容；访问地址已按需求移除；
2. 客户省 / 市 / 区三级结构化：落库、层级自洽、region 拼接口径不变、手动填写不误写。

在隔离实例上运行（推荐）：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_trace.py
"""
import os
import re
import sys

import requests

BASE = os.environ.get("CRM_TEST_BASE", "http://127.0.0.1:8081")
ADMIN_USER = os.environ.get("CRM_TEST_ADMIN", "admin")
ADMIN_PASS = os.environ.get("CRM_TEST_PASS", "admin123")

# 桌面 Chrome 的 UA：用于验证「桌面浏览器 · Chrome 版本 · Windows」的归一化结果
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1")
WECHAT_UA = ("Mozilla/5.0 (Linux; Android 13; V2148A Build/TP1A.220624.014) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Version/4.0 Chrome/107.0.0.0 Mobile Safari/537.36 "
             "MicroMessenger/8.0.49.2600(0x2800313D)")

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


def login(username=ADMIN_USER, password=ADMIN_PASS, session=None, ua=None):
    s = session or requests.Session()
    headers = {"User-Agent": ua} if ua else None
    page = s.get(f"{BASE}/login", headers=headers)
    return s, s.post(
        f"{BASE}/login",
        data={"username": username, "password": password, "csrf_token": csrf(page.text)},
        headers=headers, allow_redirects=True,
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


def tbody(html: str) -> str:
    """只取表格结果区，避免下拉框里的选项造成假阳性"""
    m = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    return m.group(1) if m else ""


def insert_legacy_log(username="legacy_user"):
    """往隔离 SQLite 库插一条补列前的日志（client_type 为 NULL）"""
    if os.environ.get("CRM_DB_HOST"):
        return False
    db_path = os.environ.get("CRM_DB_PATH")
    if not db_path or not os.path.exists(db_path):
        return False
    import sqlite3
    from datetime import datetime
    try:
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO operation_log (username, action, target_type, target_id, detail, ip, create_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, "登录", "系统", None, "历史数据（无客户端与地址）", "10.0.0.9", datetime.now()),
        )
        con.commit()
        con.close()
        return True
    except Exception as exc:
        print(f"  [WARN] 插入历史日志失败：{exc}")
        return False


def create_customer(session, name, **extra):
    """按表单提交新建客户，返回新客户 id"""
    page = session.get(f"{BASE}/customer/new")
    data = {"csrf_token": csrf(page.text), "company_name": name, "customer_level": "普通"}
    data.update(extra)
    session.post(f"{BASE}/customer/new", data=data, allow_redirects=True)
    listed = session.get(f"{BASE}/customer/?q={name}")
    hit = re.search(r"/customer/(\d+)", listed.text)
    return int(hit.group(1)) if hit else 0


def create_project(session, customer_id, name):
    """在指定客户下新建项目，返回新项目 id（用于验证项目日志能否反查客户省市）"""
    page = session.get(f"{BASE}/project/new")
    resp = session.post(
        f"{BASE}/project/new",
        data={
            "csrf_token": csrf(page.text), "customer_id": str(customer_id),
            "project_name": name, "project_amount": "10,000.00",
            "project_status": "进行中",
        },
        allow_redirects=False,
    )
    hit = re.search(r"/project/(\d+)", resp.headers.get("Location", "") or "")
    return int(hit.group(1)) if hit else 0


def main():
    if not wait_ready():
        print("实例未就绪")
        return 1

    # ==================== 1. 客户端类型：三种 UA ====================
    print("== 1. 客户端类型识别 ==")
    desktop, _ = login(ua=CHROME_UA)
    mobile, _ = login(ua=MOBILE_UA)
    wechat, _ = login(ua=WECHAT_UA)
    api_client = requests.Session()          # 默认 UA: python-requests/x.y
    api, _ = login(session=api_client)

    logs = desktop.get(f"{BASE}/logs")
    check("日志页 200", logs.status_code == 200, f"-> {logs.status_code}")
    check("表头含「客户端」列", ">客户端<" in logs.text)
    body = tbody(logs.text)
    rows = body
    check("桌面 Chrome 识别为桌面浏览器", "桌面浏览器" in rows)
    check("Chrome 版本已提取", "Chrome 131" in rows)
    check("操作系统已识别", "Windows 10/11" in rows)
    check("移动 Safari 识别为移动浏览器", "移动浏览器" in rows)
    check("微信 UA 识别为微信内置", "微信内置" in rows)
    check("脚本客户端识别为接口调用", "接口调用" in rows)
    check("请求库名称已归一化", "python-requests" in rows)

    # 客户端筛选：只保留某一类，且不把其他类混进来
    only_wechat = desktop.get(f"{BASE}/logs?client=微信内置")
    rows = tbody(only_wechat.text)
    check("按客户端筛选可用", only_wechat.status_code == 200)
    check("筛选后仍有微信记录", "微信内置" in rows)
    check("筛选后不含接口调用记录", "接口调用" not in rows)

    # ==================== 2. 访问地址已移除 ====================
    print("== 2. 访问地址已移除 ==")
    mobile.get(f"{BASE}/export/customers.xlsx?token=secret-value&keyword=trace")
    logs = desktop.get(f"{BASE}/logs")
    rows = tbody(logs.text)
    check("表头不再含「访问地址」列", ">访问地址<" not in logs.text)
    check("导出动作仍正常写日志", "导出客户台账" in rows)
    check("地址参数不再出现在日志页", "token=***" not in logs.text)

    # 翻页保留筛选条件（原先只带 q/action）
    paged = desktop.get(f"{BASE}/logs?client=桌面浏览器&page=1")
    check("筛选可在页面回显", 'value="桌面浏览器" selected' in paged.text)

    # ==================== 3. 旧日志兼容 ====================
    print("== 3. 历史日志兼容 ==")
    # 直接往隔离库塞一条「补列前」形态的记录（无客户端、无地址），确认页面照常渲染。
    # MySQL 测试库跳过该步（run_mysql.sh 的回环里没有本地 db 文件）。
    legacy = insert_legacy_log()
    if legacy:
        logs = desktop.get(f"{BASE}/logs")
        rows = tbody(logs.text)
        check("旧日志照常读取", "10.0.0.9" in rows)
        check("旧日志空值显示为破折号", "—" in rows)
    else:
        print("  [SKIP] 非 SQLite 隔离库，跳过旧数据插入")
    check("历史 IP 列仍在", ">IP<" in logs.text)

    # ==================== 4. 省市区三级结构化 ====================
    print("== 4. 省市区结构化 ==")
    cid = create_customer(desktop, "省市区验证客户A", province="江苏省", city="苏州市",
                          district="吴中区", address="星湖街 1 号")
    detail = desktop.get(f"{BASE}/customer/{cid}")
    check("客户详情可访问", detail.status_code == 200)
    check("region 拼接口径不变", "江苏省 苏州市 吴中区" in detail.text)
    check("省字段已落库", 'title="省级">江苏省' in detail.text)
    check("市字段已落库", 'title="市级">苏州市' in detail.text)
    check("区字段已落库", 'title="区县级">吴中区' in detail.text)

    # 只填省级：下级必须为空，不能出现「省空区有值」
    cid2 = create_customer(desktop, "省市区验证客户B", province="北京市",
                           city="", district="")
    detail2 = desktop.get(f"{BASE}/customer/{cid2}")
    check("只填省级时区域文本正确", "北京市" in detail2.text)
    check("只填省级时无市级徽标", 'title="市级"' not in detail2.text)
    check("只填省级时无区级徽标", 'title="区县级"' not in detail2.text)

    # 手动填写：region 原文保留，三级列不写入（避免把整段文本塞进省字段）
    cid3 = create_customer(desktop, "省市区验证客户C", region="上海市浦东新区张江镇")
    detail3 = desktop.get(f"{BASE}/customer/{cid3}")
    check("手动填写保留原文", "上海市浦东新区张江镇" in detail3.text)
    check("手动填写不写省级徽标", 'title="省级"' not in detail3.text)

    # 层级不完整的提交（只有区）：整体丢弃，region 不为空不成立
    cid4 = create_customer(desktop, "省市区验证客户D", province="", city="", district="西湖区")
    detail4 = desktop.get(f"{BASE}/customer/{cid4}")
    check("孤立区县不落库", 'title="区县级"' not in detail4.text)

    # 编辑历史客户（只有 region、没有三级列）不报错，且保存后仍可展示
    edit = desktop.get(f"{BASE}/customer/{cid3}/edit")
    check("仅 region 的老客户可正常编辑", edit.status_code == 200 and 'name="province"' in edit.text)

    # ==================== 5. 日志中的客户省市 ====================
    print("== 5. 日志中的客户省市 ==")
    logs = desktop.get(f"{BASE}/logs")
    check("表头含「客户省市」列", ">客户省市<" in logs.text)

    # 客户日志：省 + 市 紧凑拼接，中间不加空格/斜杠
    only_a = desktop.get(f"{BASE}/logs?target_type=客户&target_id={cid}")
    rows = tbody(only_a.text)
    check("客户日志展示省 + 市", 'title="江苏省苏州市"' in rows, f"-> {cid}")
    check("省市拼接不带分隔符", "江苏省 苏州市" not in rows)

    # 只有省级：原样展示，不补占位、不猜市级
    only_b = desktop.get(f"{BASE}/logs?target_type=客户&target_id={cid2}")
    rows_b = tbody(only_b.text)
    check("只有省级时原样展示", 'title="北京市"' in rows_b, f"-> {cid2}")
    check("省市重复时不叠加", "北京市北京市" not in rows_b)

    # 项目日志：经 project.customer_id 反查所属客户地区
    pid = create_project(desktop, cid, "省市联动验证项目")
    check("验证用项目创建成功", pid > 0, f"-> {pid}")
    proj_logs = desktop.get(f"{BASE}/logs?target_type=项目&target_id={pid}")
    check("项目日志继承客户省市", 'title="江苏省苏州市"' in tbody(proj_logs.text))

    # 缺失场景：不涉及客户的日志、客户未填写地区，统一显示「—」
    sys_logs = desktop.get(f"{BASE}/logs?target_type=系统")
    check("不涉及客户的日志显示占位符", "无客户归属地" in tbody(sys_logs.text))
    empty_logs = desktop.get(f"{BASE}/logs?target_type=客户&target_id={cid4}")
    check("客户未填地区显示占位符", "无客户归属地" in tbody(empty_logs.text))

    # ==================== 6. 不影响既有列表与搜索 ====================
    print("== 6. 既有读取路径不受影响 ==")
    listed = desktop.get(f"{BASE}/customer/?q=省市区验证客户")
    check("列表页正常", listed.status_code == 200)
    check("列表仍显示 region 文本", "江苏省 苏州市 吴中区" in listed.text)
    searched = desktop.get(f"{BASE}/customer/?q=苏州市")
    check("按地区搜索仍命中", "省市区验证客户A" in searched.text)

    print("")
    if fail:
        print(f"失败 {len(fail)} 项：{', '.join(fail)}")
        return 1
    print(f"全部通过：{ok_count} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
