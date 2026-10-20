# -*- coding: utf-8 -*-
"""冒烟：合同要素 / 回款计划 / 经营看板 / 负责人筛选"""
import re

import requests

BASE = "http://127.0.0.1:8081"
s = requests.Session()
s.trust_env = False


def token_of(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


r = s.get(BASE + "/login")
tk = token_of(r.text)
r = s.post(BASE + "/login", data={"username": "admin", "password": "admin123", "csrf_token": tk})
print("login ->", r.status_code)

# ---- 1. 看板 ----
html = s.get(BASE + "/").text
checks = ["应收合计（含税）", "已对账收款", "未收金额", "其中已逾期",
          "近 6 个月签约与回款", "应收账龄", "负责人业绩 Top 5"]
print("看板指标:", {c: (c in html) for c in checks})
print("看板无异常:", "Traceback" not in html and "Internal Server Error" not in html)

# ---- 2. 项目详情：合同要素 + 回款计划 ----
html = s.get(BASE + "/project/1").text
for c in ["合同编号", "签订日期", "账期", "付款条款", "回款计划", "plan-panel"]:
    print(f"  项目详情含「{c}」:", c in html)
tk = token_of(html)

# ---- 3. 新增一期回款计划 ----
r = s.post(BASE + "/finance/plan/1/add", data={
    "csrf_token": tk, "due_date": "2026-11-30", "amount": "12000", "note": "冒烟测试首期"})
print("新增回款计划 ->", r.status_code)
html = s.get(BASE + "/project/1").text
print("  计划已展示:", "冒烟测试首期" in html and "2026-11-30" in html)
m = re.search(r'action="/finance/plan/(\d+)/delete"', html)

# ---- 4. 删除该期计划 ----
if m:
    pid = m.group(1)
    tk = token_of(html)
    r = s.post(BASE + f"/finance/plan/{pid}/delete", data={"csrf_token": tk})
    html = s.get(BASE + "/project/1").text
    print("删除回款计划 ->", r.status_code, "已移除:", "冒烟测试首期" not in html)
else:
    print("未找到计划删除入口")

# ---- 5. 新建项目带合同要素，再删除 ----
tk = token_of(s.get(BASE + "/project/new").text)
r = s.post(BASE + "/project/new", data={
    "csrf_token": tk, "customer_id": "1", "project_name": "冒烟合同测试项目",
    "project_type": "运维服务", "tax_rate": "6", "amount_mode": "tax_included",
    "tax_included_amount": "50000", "start_date": "2026-10-01", "end_date": "2026-12-31",
    "project_status": "进行中", "service_mode": "远程服务", "payment_method": "支付软件转账",
    "owner": "张伟", "contract_no": "HT-2026-SMOKE", "sign_date": "2026-09-28",
    "credit_days": "45", "payment_terms": "首付 50%，验收后 30 日内结清尾款",
    "project_summary": "冒烟测试用，稍后删除",
})
print("新建项目 ->", r.status_code)
html = r.text
print("  合同号已保存:", "HT-2026-SMOKE" in html)
print("  负责人已保存:", "张伟" in html)
print("  签订日期已保存:", "2026-09-28" in html)
print("  账期 45 天:", ("45" in html))
m = re.search(r'/project/(\d+)/delete', html)
new_pid = m.group(1) if m else None
print("  新项目 id:", new_pid)

# ---- 6. 结束早于开始应被拒 ----
if new_pid:
    tk = token_of(s.get(BASE + f"/project/{new_pid}/edit").text)
    r = s.post(BASE + f"/project/{new_pid}/edit", data={
        "csrf_token": tk, "customer_id": "1", "project_name": "冒烟合同测试项目",
        "project_type": "运维服务", "tax_rate": "6", "amount_mode": "tax_included",
        "tax_included_amount": "50000", "start_date": "2026-12-31", "end_date": "2026-10-01",
        "project_status": "进行中", "service_mode": "远程服务", "payment_method": "支付软件转账",
    })
    print("结束早于开始被拒:", r.status_code == 200 and "结束日期不能早于开始日期" in r.text)

    # 清理
    tk = token_of(s.get(BASE + f"/project/{new_pid}").text)
    r = s.post(BASE + f"/project/{new_pid}/delete", data={"csrf_token": tk})
    print("删除测试项目 ->", r.status_code, "已清除:", "冒烟合同测试项目" not in s.get(BASE + "/project/").text)

# ---- 7. 负责人筛选 / 排序 ----
for q in ["?owner=张伟", "?owner=", "?sort=amount&order=desc"]:
    resp = s.get(BASE + "/project/" + q)
    print(f"项目列表 {q or '(默认)'} ->", resp.status_code)
resp = s.get(BASE + "/customer/?owner=张伟")
print("客户列表 owner 筛选 ->", resp.status_code)

# ---- 8. 字典页语义控件 ----
html = s.get(BASE + "/options/").text
print("字典页含语义控件:", "status_kind" in html, "level_color" in html, "is_transfer" in html)

# ---- 9. 待办页与导出 ----
print("待办页 ->", s.get(BASE + "/todos/").status_code)
r = s.get(BASE + "/export/projects.xlsx?owner=张伟")
print("项目筛选导出 ->", r.status_code, len(r.content), "bytes")
