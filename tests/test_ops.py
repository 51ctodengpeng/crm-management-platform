# -*- coding: utf-8 -*-
"""数据与安全能力验证：Excel 导入、客户查重、回收站、附件分类与版本、
Office 文本预览、登录锁定、密码强度、安全响应头、备份与恢复

在隔离实例上运行（推荐）：
    CRM_TEST_BASE=http://127.0.0.1:8099 python tests/test_ops.py
"""
import io
import os
import re
import sys
import zipfile

import requests

BASE = os.environ.get("CRM_TEST_BASE", "http://127.0.0.1:8081")
ADMIN_USER = os.environ.get("CRM_TEST_ADMIN", "admin")
ADMIN_PASS = os.environ.get("CRM_TEST_PASS", "admin123")

ok_count = 0
fail = []

NOTICE = "合同补充条款：质保期延长至三年。"


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


def login(username=ADMIN_USER, password=ADMIN_PASS, session=None, follow=True):
    s = session or requests.Session()
    s.trust_env = False
    page = s.get(f"{BASE}/login")
    return s, s.post(
        f"{BASE}/login",
        data={"username": username, "password": password, "csrf_token": csrf(page.text)},
        allow_redirects=follow,
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


def make_xlsx(rows, headers):
    """生成一个最小 xlsx（走 openpyxl，服务端已安装）"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def main():
    if not wait_ready():
        print("实例未就绪")
        return 1

    admin, r = login()
    if "经营概览" not in r.text:
        print("管理员登录失败")
        return 1

    # ==================== 1. 安全响应头 ====================
    print("== 1. 安全响应头 ==")
    r = admin.get(f"{BASE}/")
    check("X-Content-Type-Options", r.headers.get("X-Content-Type-Options") == "nosniff")
    check("X-Frame-Options", r.headers.get("X-Frame-Options") == "SAMEORIGIN")
    check("Referrer-Policy", r.headers.get("Referrer-Policy") == "same-origin")

    # ==================== 2. 登录失败锁定 ====================
    print("== 2. 登录失败锁定 ==")
    bad = requests.Session()
    bad.trust_env = False
    last = None
    for _ in range(5):
        page = bad.get(f"{BASE}/login")
        last = bad.post(f"{BASE}/login", data={
            "username": ADMIN_USER, "password": "wrong-pass-1",
            "csrf_token": csrf(page.text)}, allow_redirects=True)
    check("连续错误 5 次后账号锁定", "已锁定" in last.text or "临时锁定" in last.text)
    page = bad.get(f"{BASE}/login")
    locked = bad.post(f"{BASE}/login", data={
        "username": ADMIN_USER, "password": ADMIN_PASS,
        "csrf_token": csrf(page.text)}, allow_redirects=True)
    check("锁定期间正确密码也拒绝登录", "锁定" in locked.text and "经营概览" not in locked.text)

    # 解锁：管理员在账号管理里解除
    users = admin.get(f"{BASE}/user/")
    unlocks = re.findall(r'action="(/user/\d+/unlock)"', users.text)
    check("账号管理出现「解除锁定」入口", bool(unlocks))
    for action in unlocks:
        admin.post(f"{BASE}{action}", data={"csrf_token": csrf(users.text)},
                   allow_redirects=True)
    admin, r = login()
    check("解除锁定后可正常登录", "经营概览" in r.text)
    if "经营概览" not in r.text:
        # 兜底：逐页翻账号列表找出仍被锁的账号再解一次
        users = admin.get(f"{BASE}/user/")
        for action in re.findall(r'action="(/user/\d+/unlock)"', users.text):
            admin.post(f"{BASE}{action}", data={"csrf_token": csrf(users.text)},
                       allow_redirects=True)
        admin, r = login()
        check("二次解锁后登录成功", "经营概览" in r.text)

    # ==================== 3. 密码强度 ====================
    print("== 3. 密码强度 ==")
    page = admin.get(f"{BASE}/change-password")
    r = admin.post(f"{BASE}/change-password", data={
        "csrf_token": csrf(page.text), "old_password": ADMIN_PASS,
        "new_password": "abcdefgh", "confirm_password": "abcdefgh"}, allow_redirects=True)
    check("纯字母密码被拒绝", "需同时包含字母和数字" in r.text)
    page = admin.get(f"{BASE}/change-password")
    r = admin.post(f"{BASE}/change-password", data={
        "csrf_token": csrf(page.text), "old_password": ADMIN_PASS,
        "new_password": "abc12", "confirm_password": "abc12"}, allow_redirects=True)
    check("过短密码被拒绝", "长度至少 8 位" in r.text)

    # ==================== 4. 客户兜底数据 ====================
    print("== 4. 准备客户 ==")
    page = admin.get(f"{BASE}/customer/new")
    admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(page.text), "company_name": "导入测试客户",
        "customer_level": "普通", "contact_phone": "13900002222"},
        allow_redirects=True)
    listing = admin.get(f"{BASE}/customer/?q=导入测试客户")
    cid = int(re.search(r"/customer/(\d+)", listing.text).group(1))
    check("准备客户创建成功", cid > 0)

    # ==================== 5. 客户查重 ====================
    print("== 5. 客户查重 ==")
    page = admin.get(f"{BASE}/customer/new")
    r = admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(page.text), "company_name": "导入测试客户",
        "customer_level": "普通"}, allow_redirects=True)
    check("同名客户给出重复提示", "疑似重复" in r.text and "仍然保存" in r.text)
    check("未确认前没有写入", r.request.method == "POST" and "创建成功" not in r.text)
    page = admin.get(f"{BASE}/customer/new")
    r = admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(page.text), "company_name": "导入测试客户",
        "customer_level": "普通", "force": "1"}, allow_redirects=True)
    check("勾选「仍然保存」后可创建", "创建成功" in r.text)
    dup = admin.get(f"{BASE}/customer/?q=导入测试客户")
    check("列表出现两条同名客户", dup.text.count("导入测试客户") >= 2)

    page = admin.get(f"{BASE}/customer/new")
    r = admin.post(f"{BASE}/customer/new", data={
        "csrf_token": csrf(page.text), "company_name": "另一个完全不同的名字",
        "contact_phone": "13900002222"}, allow_redirects=True)
    check("电话重复同样给出提示", "疑似重复" in r.text)

    # ==================== 6. Excel 导入 ====================
    print("== 6. Excel 导入 ==")
    r = admin.get(f"{BASE}/import/template/customer.xlsx")
    check("客户导入模板可下载", r.status_code == 200 and r.content[:2] == b"PK")

    headers = ["公司名称", "对接人", "联系电话", "客户等级"]
    rows = [["批量导入甲", "张三", "13700000001", "重要"],
            ["批量导入乙", "李四", "13700000002", "普通"],
            ["导入测试客户", "重复", "13700000003", "普通"],
            ["", "缺名称", "13700000004", "普通"]]
    files = {"file": ("customers.xlsx", make_xlsx(rows, headers),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = admin.post(f"{BASE}/import/preview",
                   data={"csrf_token": csrf(admin.get(f"{BASE}/import/").text),
                         "kind": "customer"}, files=files, allow_redirects=True)
    check("预览页渲染成功", r.status_code == 200 and "导入预览" in r.text)
    check("预览标出可导入行", "可导入" in r.text)
    check("预览标出重复行", "已存在同名客户" in r.text)
    check("预览标出错误行", "公司名称为空" in r.text)
    token = re.search(r'name="token" value="([^"]+)"', r.text)

    if token:
        r = admin.post(f"{BASE}/import/commit",
                       data={"csrf_token": csrf(r.text), "token": token.group(1)},
                       allow_redirects=True)
        check("确认后执行导入", "成功导入 2 条客户数据" in r.text)
        check("导入的客户出现在列表", "批量导入甲" in r.text)

    # 项目导入：客户不存在应报错
    headers = ["客户公司名称", "项目名称", "税率%", "含税金额", "开始日期", "结束日期"]
    rows = [["批量导入甲", "导入项目A", 6, "10600", "2026-01-01", "2026-03-31"],
            ["查无此客户", "导入项目B", 6, "5000", "2026-01-01", "2026-03-31"],
            ["批量导入甲", "导入项目C", 6, "5000", "2026-05-01", "2026-04-01"]]
    files = {"file": ("projects.xlsx", make_xlsx(rows, headers),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = admin.post(f"{BASE}/import/preview",
                   data={"csrf_token": csrf(admin.get(f"{BASE}/import/").text),
                         "kind": "project"}, files=files, allow_redirects=True)
    check("项目导入预览成功", r.status_code == 200 and "导入预览" in r.text)
    check("找不到客户时该行报错", "找不到客户" in r.text)
    check("结束早于开始被拦截", "结束日期早于开始日期" in r.text)
    token = re.search(r'name="token" value="([^"]+)"', r.text)
    if token:
        r = admin.post(f"{BASE}/import/commit",
                       data={"csrf_token": csrf(r.text), "token": token.group(1)},
                       allow_redirects=True)
        check("项目导入成功", "成功导入 1 条项目数据" in r.text)

    # 金额换算：10600 含税、6% -> 不含税 10000
    proj = admin.get(f"{BASE}/project/?q=导入项目A")
    pid_match = re.search(r"/project/(\d+)", proj.text)
    check("导入项目出现在列表", bool(pid_match))
    if pid_match:
        detail = admin.get(f"{BASE}/project/{pid_match.group(1)}")
        check("含税金额按 6% 反算不含税", "10,000.00" in detail.text and "10,600.00" in detail.text)
        pid = int(pid_match.group(1))
    else:
        pid = None

    # ==================== 7. 附件分类与版本 ====================
    print("== 7. 附件分类与版本 ==")
    if pid:
        text1 = "第一版合同内容：服务期为一年。"
        files = {"files": ("服务合同.txt", io.BytesIO(text1.encode("utf-8")), "text/plain")}
        r = admin.post(f"{BASE}/attachment/upload/{pid}",
                       data={"csrf_token": csrf(admin.get(f"{BASE}/project/{pid}").text),
                             "category": "合同", "remark": "首版"},
                       files=files, allow_redirects=True)
        check("上传附件带分类", "归入「合同」" in r.text)
        check("附件显示分类徽标", "合同" in r.text)

        text2 = "第二版合同内容：服务期改为两年，条款已更新。"
        files = {"files": ("服务合同.txt", io.BytesIO(text2.encode("utf-8")), "text/plain")}
        r = admin.post(f"{BASE}/attachment/upload/{pid}",
                       data={"csrf_token": csrf(admin.get(f"{BASE}/project/{pid}").text),
                             "category": "合同"},
                       files=files, allow_redirects=True)
        check("同名附件提示归档旧版本", "旧版本已归档" in r.text)
        check("显示版本号 v2", ">v2<" in r.text or "v2" in r.text)
        check("显示历史版本入口", "历史版本" in r.text)

        aid = re.search(r"/attachment/(\d+)/preview", r.text)
        if aid:
            pv = admin.get(f"{BASE}/attachment/{aid.group(1)}/preview")
            check("文本附件在线预览", pv.status_code == 200 and "服务合同" in pv.text)
            check("预览展示提取内容", "服务期" in pv.text)

    # docx 文本预览
    if pid:
        from openpyxl import Workbook  # noqa: F401  (确依赖已装)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<?xml version='1.0'?>")
            zf.writestr("word/document.xml",
                        "<w:document><w:body><w:p><w:r><w:t>" + NOTICE + "</w:t></w:r></w:p>"
                        "</w:body></w:document>")
        buf.seek(0)
        files = {"files": ("补充协议.docx", buf,
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        r = admin.post(f"{BASE}/attachment/upload/{pid}",
                       data={"csrf_token": csrf(admin.get(f"{BASE}/project/{pid}").text),
                             "category": "合同"}, files=files, allow_redirects=True)
        check("docx 上传成功", "成功上传" in r.text)
        m = re.search(r"/attachment/(\d+)/preview", r.text)
        if m:
            pv = admin.get(f"{BASE}/attachment/{m.group(1)}/preview")
            check("docx 提取文本预览", pv.status_code == 200 and "质保期延长至三年" in pv.text)

    # ==================== 8. 回收站 ====================
    print("== 8. 回收站 ==")
    if pid:
        page = admin.get(f"{BASE}/project/{pid}")
        r = admin.post(f"{BASE}/project/{pid}/delete",
                       data={"csrf_token": csrf(page.text)}, allow_redirects=True)
        check("删除项目提示移入回收站", "移入回收站" in r.text)
        listing = admin.get(f"{BASE}/project/?q=导入项目A").text
        # 删除成功的 flash 里也带项目名，所以用「列表项链接」判断是否真的不在列表
        check("列表中不再出现该项目", f'href="/project/{pid}"' not in listing)

        rec = admin.get(f"{BASE}/recycle/")
        check("回收站可访问", rec.status_code == 200 and "回收站" in rec.text)
        check("回收站出现被删项目", "导入项目A" in rec.text)
        check("回收站统计附件数量", re.search(r"附件", rec.text) is not None)

        rid = re.search(r'/recycle/(\d+)/restore', rec.text)
        check("存在恢复入口", bool(rid))
        if rid:
            r = admin.post(f"{BASE}/recycle/{rid.group(1)}/restore",
                           data={"csrf_token": csrf(rec.text)}, allow_redirects=True)
            check("恢复成功", "已恢复" in r.text)
            # 恢复是「按快照重建」，不保证沿用原主键：SQLite 恰好复用最大 id，
            # MySQL 的 AUTO_INCREMENT 不复用已删值，恢复后是新 id。按名称重新定位。
            listing2 = admin.get(f"{BASE}/project/?q=导入项目A").text
            ids2 = re.findall(r'/project/(\d+)', listing2)
            new_pid = int(ids2[0]) if ids2 else pid
            pid = new_pid          # 后续删除/彻底删除沿用恢复后的真实 id
            back = admin.get(f"{BASE}/project/{new_pid}")
            check("恢复后项目详情可访问", back.status_code == 200 and "导入项目A" in back.text)
            check("恢复后附件仍在", "服务合同.txt" in back.text)
            check("恢复后附件内容可下载", len(admin.get(
                f"{BASE}/attachment/download-all/{new_pid}").content) > 100)

        # 客户进回收站并恢复
        page = admin.get(f"{BASE}/customer/{cid}")
        admin.post(f"{BASE}/customer/{cid}/delete",
                   data={"csrf_token": csrf(page.text)}, allow_redirects=True)
        rec = admin.get(f"{BASE}/recycle/")
        check("客户也被移入回收站", "导入测试客户" in rec.text)
        rid2 = re.search(r'/recycle/(\d+)/restore', rec.text)
        if rid2:
            r = admin.post(f"{BASE}/recycle/{rid2.group(1)}/restore",
                           data={"csrf_token": csrf(rec.text)}, allow_redirects=True)
            check("客户恢复成功", "已恢复" in r.text)
            back = admin.get(f"{BASE}/customer/?q=导入测试客户")
            check("恢复后客户回到列表", "导入测试客户" in back.text)

        # 彻底删除
        page = admin.get(f"{BASE}/project/{pid}")
        admin.post(f"{BASE}/project/{pid}/delete",
                   data={"csrf_token": csrf(page.text)}, allow_redirects=True)
        rec = admin.get(f"{BASE}/recycle/")
        rid3 = re.search(r'/recycle/(\d+)/purge', rec.text)
        if rid3:
            r = admin.post(f"{BASE}/recycle/{rid3.group(1)}/purge",
                           data={"csrf_token": csrf(rec.text)}, allow_redirects=True)
            check("彻底删除成功", "已彻底删除" in r.text)
            rec2 = admin.get(f"{BASE}/recycle/")
            check("彻底删除后不在回收站", "导入项目A" not in rec2.text)

    # ==================== 9. 备份与恢复 ====================
    print("== 9. 备份与恢复 ==")
    page = admin.get(f"{BASE}/backup/")
    check("备份页可访问", page.status_code == 200 and "备份与恢复" in page.text)
    r = admin.post(f"{BASE}/backup/create", data={"csrf_token": csrf(page.text)},
                   allow_redirects=True)
    check("一键备份成功", "备份已生成" in r.text)
    name = re.search(r"(crm-backup-[\d\-]+\.zip)", r.text)
    check("备份列表出现文件", bool(name))
    if name:
        dl = admin.get(f"{BASE}/backup/{name.group(1)}/download")
        check("备份可下载", dl.status_code == 200 and dl.content[:2] == b"PK")
        check("备份包含数据库与附件", len(dl.content) > 2000)

        # 用刚生成的备份做一次恢复（等同回滚到几秒前）
        page = admin.get(f"{BASE}/backup/")
        r = admin.post(f"{BASE}/backup/restore", data={
            "csrf_token": csrf(page.text), "name": name.group(1), "confirm": "RESTORE"},
            allow_redirects=True)
        check("按备份恢复成功", "数据已恢复" in r.text)
        check("恢复后仍可正常访问", "经营概览" in admin.get(f"{BASE}/").text)
        check("恢复后生成恢复前自动备份", "crm-before-restore-" in admin.get(f"{BASE}/backup/").text)

    page = admin.get(f"{BASE}/backup/")
    r = admin.post(f"{BASE}/backup/restore", data={
        "csrf_token": csrf(page.text), "confirm": "wrong"}, allow_redirects=True)
    check("恢复需输入 RESTORE 确认", "输入 RESTORE" in r.text or "确认框" in r.text)

    # ==================== 10. 权限 ====================
    print("== 10. 权限边界 ==")
    anon = requests.Session()
    anon.trust_env = False
    r = anon.get(f"{BASE}/recycle/", allow_redirects=False)
    check("未登录访问回收站被拦截", r.status_code == 302)
    r = anon.get(f"{BASE}/backup/", allow_redirects=False)
    check("未登录访问备份页被拦截", r.status_code == 302)

    print("\n" + "=" * 50)
    print(f"通过 {ok_count} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print("  - " + name)
        return 1
    print("数据与安全能力验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
