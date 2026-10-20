# -*- coding: utf-8 -*-
"""端到端功能自测

覆盖：登录鉴权、CSRF、客户(含来源渠道/微信/地区)、项目、附件上传/下载/打包/拦截、
Excel 导出、注册审批全流程、账号管理权限、全局搜索、操作日志。

建议在独立的临时数据库上运行（不污染正式数据）：
    bash tests/run_isolated.sh
或指定目标实例：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_e2e.py
"""
import os
import re
import sys
import zipfile
from io import BytesIO

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


def login(username=ADMIN_USER, password=ADMIN_PASS, session=None):
    s = session or requests.Session()
    tok = csrf(s.get(f"{BASE}/login").text)
    r = s.post(f"{BASE}/login",
               data={"username": username, "password": password, "csrf_token": tok},
               allow_redirects=True)
    return s, r


def aid_of(html: str, fname: str) -> int:
    """从详情页中取出指定文件名对应的附件 ID"""
    idx = html.find(fname)
    m = re.search(r"/attachment/(\d+)/download", html[idx:idx + 4000])
    return int(m.group(1))


def wait_ready(timeout: int = 40) -> bool:
    """等服务实例就绪后再开始跑用例，避免启动竞态导致的误报"""
    import time
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}/login", timeout=3)
            if r.status_code == 200:
                return True
            last = f"HTTP {r.status_code}"
        except Exception as exc:
            last = str(exc)[:80]
        time.sleep(0.6)
    print(f"  [FAIL] 目标实例 {BASE} 未就绪（{last}）")
    sys.exit(2)


print(f"目标实例：{BASE}")
wait_ready()
s = requests.Session()

# ============================================================
print("== 1. 未登录访问拦截 ==")
r = s.get(f"{BASE}/customer/", allow_redirects=False)
check("未登录访问客户列表被重定向", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"-> {r.status_code}")
r = s.get(f"{BASE}/", allow_redirects=False)
check("未登录访问首页直接渲染登录页",
      r.status_code == 200 and 'name="password"' in r.text and 'action="/login' in r.text,
      f"-> {r.status_code}")

print("== 2. 登录与注册入口 ==")
r = s.get(f"{BASE}/login")
check("登录页可访问", r.status_code == 200)
check("登录页含注册入口", "/register" in r.text)
r = s.get(f"{BASE}/register")
check("注册页可访问", r.status_code == 200)
check("注册页含审批说明", "审批" in r.text)

s, r = login()
check("管理员正确账密登录成功", "经营概览" in r.text)
s2 = requests.Session()
tok2 = csrf(s2.get(f"{BASE}/login").text)
r = s2.post(f"{BASE}/login", data={"username": ADMIN_USER, "password": "wrong", "csrf_token": tok2})
check("错误密码被拒绝", "账号或密码错误" in r.text)

print("== 3. CSRF 防护 ==")
r = s.post(f"{BASE}/customer/new", data={"company_name": "无token注入"}, allow_redirects=False)
check("缺少 csrf_token 的 POST 被拒绝", r.status_code == 400, f"-> {r.status_code}")

print("== 4. 新增客户（含来源渠道 / 微信号 / 地区）==")
r = s.get(f"{BASE}/customer/new")
tok = csrf(r.text)
check("客户表单含来源渠道字段", 'name="source_channel"' in r.text)
check("客户表单含微信号字段", 'name="wechat"' in r.text)
check("客户表单含所在地区字段", 'name="region"' in r.text)
check("来源渠道含自定义选项", "__custom__" in r.text)

r = s.post(f"{BASE}/customer/new", data={
    "csrf_token": tok, "company_name": "华信科技有限公司", "contact_name": "张伟",
    "contact_phone": "13800138000", "wechat": "zhangwei_work",
    "region": "北京市", "address": "海淀区中关村大街 1 号",
    "contact_email": "zhang@huaxin.com", "business_type": "政企",
    "customer_level": "重要", "source_channel": "互联网",
    "remark": "老客户，重视响应速度",
}, allow_redirects=False)
check("客户创建成功并跳转详情", r.status_code == 302)
cid = int(re.search(r"/customer/(\d+)", r.headers.get("Location", "")).group(1))

r = s.get(f"{BASE}/customer/{cid}")
check("详情页展示微信号", "zhangwei_work" in r.text)
check("详情页展示来源渠道", "互联网" in r.text)
check("详情页展示所在地区", "北京市" in r.text)

# 自定义渠道（模拟前端把自定义值放进 source_channel）
r = s.get(f"{BASE}/customer/new")
r = s.post(f"{BASE}/customer/new", data={
    "csrf_token": csrf(r.text), "company_name": "自定义渠道测试公司",
    "source_channel": "__custom__", "source_channel_custom": "行业协会推荐",
}, allow_redirects=False)
cid_custom = int(re.search(r"/customer/(\d+)", r.headers.get("Location", "")).group(1))
r = s.get(f"{BASE}/customer/{cid_custom}")
check("自定义来源渠道按输入值入库", "行业协会推荐" in r.text)
check("哨兵值未落库", "__custom__" not in r.text)

r = s.get(f"{BASE}/customer/?channel=互联网")
check("按来源渠道筛选生效", "华信科技有限公司" in r.text and "自定义渠道测试公司" not in r.text)
r = s.get(f"{BASE}/customer/?channel=行业协会推荐")
check("自定义渠道可参与筛选", "自定义渠道测试公司" in r.text)
r = s.get(f"{BASE}/customer/?q=zhangwei_work")
check("客户搜索支持微信号", "华信科技有限公司" in r.text)
r = s.get(f"{BASE}/customer/?q=北京市")
check("客户搜索支持地区", "华信科技有限公司" in r.text)
r = s.get(f"{BASE}/customer/?q=不存在的公司xyz")
check("客户搜索无结果时提示正确", "没有符合条件的客户" in r.text)

print("== 5. 新增项目 ==")
r = s.get(f"{BASE}/project/new")
r = s.post(f"{BASE}/project/new", data={
    "csrf_token": csrf(r.text), "customer_id": cid, "project_name": "AD域维保项目",
    "project_type": "AD域", "project_amount": "86,000.00",
    "start_date": "2026-01-01", "end_date": "2026-12-31",
    "project_status": "进行中", "project_summary": "域控日常巡检、组策略维护、故障响应。",
}, allow_redirects=False)
check("项目创建成功", r.status_code == 302)
pid = int(re.search(r"/project/(\d+)", r.headers.get("Location", "")).group(1))
r = s.get(f"{BASE}/project/{pid}")
check("金额带千分位能被正确解析", "86,000.00" in r.text)
r = s.get(f"{BASE}/project/?status=进行中")
check("项目状态筛选生效", "AD域维保项目" in r.text)
r = s.get(f"{BASE}/project/?status=已终止")
check("筛选不匹配时无结果", "AD域维保项目" not in r.text)

print("== 6. 附件上传 / 长文件名缩略 ==")
r = s.get(f"{BASE}/project/{pid}")
tok = csrf(r.text)
up_url = BASE + re.search(r'action="(/attachment/upload/\d+)"', r.text).group(1)
LONG_NAME = "2026年度AD域控维保服务合同及技术服务附件清单最终确认版.pdf"
files = [
    ("files", ("2026年度维保合同.pdf", b"%PDF-1.4 fake contract content", "application/pdf")),
    ("files", ("项目报价单.docx", b"PK\x03\x04 fake docx", "application/octet-stream")),
    ("files", (LONG_NAME, b"%PDF-1.4 long name file", "application/pdf")),
]
r = s.post(up_url, data={"csrf_token": tok, "remark": "合同与报价"},
           files=files, allow_redirects=True)
check("多文件一次上传成功", "成功上传 3 个附件" in r.text)

check("长文件名被缩略显示", "…" in r.text)
check("完整文件名保留在 title 中可悬停查看", f'title="{LONG_NAME}"' in r.text)
check("文件列表带截断与操作按钮样式类",
      "attach-name" in r.text and "attach-actions" in r.text)
check("显示附件数量与合计大小", "合计" in r.text and "个文件" in r.text)
check("提供打包下载入口", "/attachment/download-all/" in r.text)
check("显示上传人", "admin" in r.text)

pdf_aid = aid_of(r.text, "2026年度维保合同.pdf")
r = s.get(f"{BASE}/attachment/{pdf_aid}/download")
check("附件下载成功且内容一致", r.status_code == 200 and b"fake contract" in r.content)
check("下载响应头带文件名", "attachment" in r.headers.get("Content-Disposition", ""))
r = s.get(f"{BASE}/attachment/{pdf_aid}/preview")
check("PDF 在线预览返回正确 MIME",
      r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/pdf"))

r = s.get(f"{BASE}/project/{pid}")
r = s.post(f"{BASE}/attachment/{pdf_aid}/remark",
           data={"csrf_token": csrf(r.text), "remark": "已盖章原件"},
           allow_redirects=True)
check("附件备注可编辑", "附件备注已更新" in r.text and "已盖章原件" in r.text)

print("== 7. 打包下载全部附件 ==")
r = s.get(f"{BASE}/attachment/download-all/{pid}")
check("打包下载返回 zip", r.status_code == 200 and r.content[:2] == b"PK")
try:
    zf = zipfile.ZipFile(BytesIO(r.content))
    names = zf.namelist()
    check("zip 内含全部附件", len(names) == 3, f"{names}")
    check("zip 内保留原始长文件名", LONG_NAME in names)
    check("zip 内容可正常解压", zf.read(LONG_NAME).startswith(b"%PDF"))
except Exception as exc:  # pragma: no cover
    check("zip 解析", False, str(exc))

print("== 8. 危险文件拦截 ==")
for fname, content, label, expect in [
    ("evil.exe", b"MZ\x90\x00", "exe", "禁止上传"),
    ("shell.php", b"<?php ?>", "php", "禁止上传"),
    ("run.sh", b"#!/bin/sh", "sh", "禁止上传"),
    ("video.mp4", b"\x00\x00\x00", "mp4", "不支持的文件类型"),
]:
    r = s.get(f"{BASE}/project/{pid}")
    r = s.post(up_url, data={"csrf_token": csrf(r.text)},
               files=[("files", (fname, content, "application/octet-stream"))],
               allow_redirects=True)
    check(f"{label} 文件被拒绝", expect in r.text)

print("== 9. Excel 导出 ==")
r = s.get(f"{BASE}/export/customers.xlsx")
check("客户台账导出为 xlsx", r.status_code == 200 and r.content[:2] == b"PK", f"{len(r.content)} bytes")
r = s.get(f"{BASE}/export/projects.xlsx")
check("项目台账导出为 xlsx", r.status_code == 200 and r.content[:2] == b"PK", f"{len(r.content)} bytes")

print("== 10. 注册 → 待审批 → 审批通过 → 可登录 ==")
NEW_USER, NEW_PASS = "testuser01", "test123456"
anon = requests.Session()
r = anon.get(f"{BASE}/register")
r = anon.post(f"{BASE}/register", data={
    "csrf_token": csrf(r.text), "username": NEW_USER, "real_name": "测试用户",
    "password": NEW_PASS, "confirm_password": NEW_PASS,
    "phone": "13800001111", "department": "测试部",
    "contact_email": "test01@company.com", "apply_reason": "自动化测试注册",
}, allow_redirects=False)
check("注册提交后跳转等待页", r.status_code == 302 and "register/done" in r.headers.get("Location", ""))

_, r = login(NEW_USER, NEW_PASS)
check("待审批账号登录被拦截", "等待管理员审批" in r.text)
check("待审批账号未进入系统", "经营概览" not in r.text)

r = anon.get(f"{BASE}/register")
r = anon.post(f"{BASE}/register", data={
    "csrf_token": csrf(r.text), "username": NEW_USER, "real_name": "重复",
    "password": NEW_PASS, "confirm_password": NEW_PASS,
})
check("重复账号注册被拒绝", "已被注册" in r.text)
r = anon.get(f"{BASE}/register")
r = anon.post(f"{BASE}/register", data={
    "csrf_token": csrf(r.text), "username": "weakpwduser", "real_name": "弱密码",
    "password": "123", "confirm_password": "123",
})
check("弱密码注册被拒绝", "至少 8 位" in r.text)

r = s.get(f"{BASE}/")
check("管理员首页出现待审批提醒", "等待审批" in r.text or "前往审批" in r.text)
r = s.get(f"{BASE}/user/?status=pending")
check("待审批账号出现在账号管理", NEW_USER in r.text)
uid = int(re.search(r"/user/(\d+)", r.text).group(1))

r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/approve", data={"csrf_token": csrf(r.text)},
           allow_redirects=True)
check("审批通过成功", "现在可以登录" in r.text)

user_sess, r = login(NEW_USER, NEW_PASS)
check("审批通过后可以登录", "经营概览" in r.text)
check("普通用户登录后看到自己姓名", "测试用户" in r.text)

print("== 11. 账号管理权限隔离 ==")
r = user_sess.get(f"{BASE}/user/", allow_redirects=False)
check("普通用户访问账号管理被拦截", r.status_code == 302)
r = user_sess.get(f"{BASE}/user/", allow_redirects=True)
check("普通用户被提示无权限", "仅管理员可操作" in r.text)
nav = r.text.split("sidebar-nav")[1].split("</nav>")[0]
check("普通用户侧边栏无账号管理入口", "/user/" not in nav)

print("== 12. 停用 / 启用 / 角色 / 重置密码 ==")
r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/toggle", data={"csrf_token": csrf(r.text)}, allow_redirects=True)
check("停用账号成功", "已停用账号" in r.text)
_, r = login(NEW_USER, NEW_PASS)
check("停用账号无法登录", "已被停用" in r.text)

r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/toggle", data={"csrf_token": csrf(r.text)}, allow_redirects=True)
check("重新启用账号成功", "已启用账号" in r.text)

r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/role", data={"csrf_token": csrf(r.text), "role": "admin"},
           allow_redirects=True)
check("提升为管理员成功", "角色已调整为管理员" in r.text)
user_sess, r = login(NEW_USER, NEW_PASS)
check("提升后具备账号管理权限", "账号管理" in r.text)
r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/role", data={"csrf_token": csrf(r.text), "role": "user"},
           allow_redirects=True)
check("取消管理员成功", "角色已调整为普通用户" in r.text)

r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/{uid}/reset-password",
           data={"csrf_token": csrf(r.text), "new_password": "reset12345"},
           allow_redirects=True)
check("管理员重置密码成功", "已重置" in r.text)
_, r = login(NEW_USER, "reset12345")
check("重置后的新密码可登录", "经营概览" in r.text)
_, r = login(NEW_USER, NEW_PASS)
check("旧密码已失效", "账号或密码错误" in r.text)

print("== 13. 自我保护 ==")
r = s.get(f"{BASE}/user/")
tok = csrf(r.text)
r = s.post(f"{BASE}/user/1/toggle", data={"csrf_token": tok}, allow_redirects=True)
check("不能停用自己的账号", "不能停用" in r.text)
r = s.get(f"{BASE}/user/")
r = s.post(f"{BASE}/user/1/role", data={"csrf_token": csrf(r.text), "role": "user"},
           allow_redirects=True)
check("不能取消自己的管理员权限", "不能取消自己的管理员权限" in r.text)

print("== 14. 全局搜索 ==")
r = s.get(f"{BASE}/search?q=华信")
check("全局搜索命中客户", "华信科技有限公司" in r.text)
r = s.get(f"{BASE}/search?q=AD域")
check("全局搜索命中项目", "AD域维保项目" in r.text)
r = s.get(f"{BASE}/search?q=zhangwei_work")
check("全局搜索支持微信号", "华信科技有限公司" in r.text)
r = s.get(f"{BASE}/search")
check("空关键字展示引导文案", "输入关键字" in r.text)

print("== 15. 仪表盘与日志 ==")
r = s.get(f"{BASE}/")
check("仪表盘展示客户来源渠道分布", "客户来源渠道分布" in r.text)
check("仪表盘展示附件总数", "附件总数" in r.text)
check("仪表盘展示项目状态分布", "项目状态分布" in r.text)
r = s.get(f"{BASE}/logs")
for kw in ("注册", "通过", "上传", "导出", "新增"):
    check(f"日志包含「{kw}」记录", kw in r.text)
r = s.get(f"{BASE}/logs?action=注册")
check("日志按动作筛选生效", "注册" in r.text)

print("== 16. 每页条数切换 ==")
r = s.get(f"{BASE}/customer/?per_page=10")
check("每页条数参数生效", 'value="10" selected' in r.text)
r = s.get(f"{BASE}/customer/?per_page=999")
check("非法每页条数回退默认值", 'value="20" selected' in r.text)

print("== 17. 附件删除同步磁盘 ==")
r = s.get(f"{BASE}/project/{pid}")
r = s.post(f"{BASE}/attachment/{pdf_aid}/delete", data={"csrf_token": csrf(r.text)},
           allow_redirects=True)
check("附件删除成功", "已删除" in r.text)
check("删除后列表中不再出现该附件", f"/attachment/{pdf_aid}/delete" not in r.text)
check("删除后附件计数减 1", "附件（2）" in r.text)
r = s.get(f"{BASE}/attachment/{pdf_aid}/download", allow_redirects=False)
check("已删除附件的下载链接失效", r.status_code == 404, f"-> {r.status_code}")

print("== 18. 修改密码 ==")
r = s.get(f"{BASE}/change-password")
r = s.post(f"{BASE}/change-password", data={
    "csrf_token": csrf(r.text), "old_password": ADMIN_PASS,
    "new_password": "adminTmp123", "confirm_password": "adminTmp123",
}, allow_redirects=True)
check("修改密码后强制重新登录", "请重新登录" in r.text)
s, r = login(ADMIN_USER, "adminTmp123")
check("新密码可登录", "经营概览" in r.text)
r = s.get(f"{BASE}/change-password")
r = s.post(f"{BASE}/change-password", data={
    "csrf_token": csrf(r.text), "old_password": "adminTmp123",
    "new_password": ADMIN_PASS, "confirm_password": ADMIN_PASS,
}, allow_redirects=True)
check("密码已还原为默认值", "请重新登录" in r.text)

print("== 19. 退出登录 ==")
s, r = login()
r = s.get(f"{BASE}/logout", allow_redirects=True)
check("退出后回到登录页", "登 录" in r.text or "请登录" in r.text)
r = s.get(f"{BASE}/", allow_redirects=False)
check("退出后首页回到登录页", r.status_code == 200 and 'name="password"' in r.text,
      f"-> {r.status_code}")

print("\n" + "=" * 50)
print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
if fail:
    print("失败项：")
    for f in fail:
        print("  - " + f)
    sys.exit(1)
print("全部功能自测通过")
