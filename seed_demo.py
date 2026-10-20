# -*- coding: utf-8 -*-
"""可选：写入一批演示数据，方便第一次打开时看到完整界面效果

用法：
    python seed_demo.py          # 追加演示数据（不清空已有数据）
    python seed_demo.py --reset  # 先清空所有业务数据，再写入演示数据

想彻底重置系统：删除 crm.db 与 uploads/ 下的文件，重新运行 init_db.py 即可。
"""
import sys
from datetime import date, datetime
from pathlib import Path

from app import app
from config import Config
from extensions import db
from models import Attachment, Customer, OperationLog, Project, User
from utils import remove_file_on_disk

DEMO_CUSTOMERS = [
    dict(company_name="华云信息技术有限公司", contact_name="张伟", contact_phone="13800138001",
         wechat="zhangwei_it", region="北京市", address="海淀区中关村南大街 12 号 A 座 8 层",
         contact_email="zhangwei@huayun.com", business_type="政企", customer_level="重要",
         source_channel="客户介绍", remark="三年老客户，重视故障响应时效，要求 2 小时内响应。"),
    dict(company_name="恒瑞精密制造股份有限公司", contact_name="李强", contact_phone="13900139002",
         wechat="liqiang_hengrui", region="江苏省苏州市", address="工业园区星湖街 218 号",
         contact_email="liqiang@hengrui.cn", business_type="制造业", customer_level="重要",
         source_channel="互联网", remark="生产系统不能停机，变更需提前一周报备。"),
    dict(company_name="市第一人民医院", contact_name="王医生", contact_phone="13700137003",
         wechat="wangyisheng_hz", region="浙江省杭州市", address="上城区解放路 261 号",
         contact_email="wang@firsthospital.org", business_type="医疗", customer_level="普通",
         source_channel="招投标", remark="信息科对接，涉及等保合规要求。"),
    dict(company_name="启明教育科技有限公司", contact_name="陈晨", contact_phone="13600136004",
         wechat="chenchen_edu", region="广东省深圳市", address="南山区科苑路 15 号",
         contact_email="chenchen@qiming-edu.com", business_type="教育", customer_level="潜在",
         source_channel="行业社群", remark="初次接触，正在评估超融合方案。"),
    dict(company_name="云启新能源股份有限公司", contact_name="刘敏", contact_phone="13500135005",
         wechat="liumin_cloud", region="四川省成都市", address="高新区天府大道 966 号",
         contact_email="liumin@yunqi-newenergy.com", business_type="其他", customer_level="潜在",
         source_channel="行业协会推荐", remark="自定义渠道来源示例：由储能行业协会推荐对接。"),
]

DEMO_PROJECTS = [
    dict(company="华云信息技术有限公司", project_name="AD域控维保项目", project_type="AD域",
         project_amount=86000.0, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
         project_status="进行中",
         project_summary="服务范围：2 台域控巡检、组策略维护、DNS/DHCP 排障、账号生命周期管理。\n"
                         "响应要求：工作日 2 小时内响应，重大故障 30 分钟到场。\n"
                         "踩坑记录：域控时间同步依赖外部 NTP，曾因机房防火墙策略调整导致时间漂移，已在内部 NTP 上做冗余。"),
    dict(company="华云信息技术有限公司", project_name="VMware 虚拟化平台扩容", project_type="虚拟化",
         project_amount=158000.0, start_date=date(2025, 7, 1), end_date=date(2025, 9, 30),
         project_status="已验收",
         project_summary="新增 3 台宿主机接入 vSphere 集群，扩容分布式存储 40TB，完成 P2V 迁移 18 台。\n"
                         "验收结论：迁移窗口内业务零中断，客户签字确认。"),
    dict(company="恒瑞精密制造股份有限公司", project_name="存储阵列故障恢复", project_type="存储故障",
         project_amount=45000.0, start_date=date(2026, 3, 12), end_date=date(2026, 3, 15),
         project_status="已验收",
         project_summary="现象：阵列双控之一宕机，卷处于降级状态。\n"
                         "处理：更换故障控制器，重建缓存，数据一致性校验通过，业务恢复。\n"
                         "遗留建议：阵列已过保，建议 2026 年内完成替换立项。"),
    dict(company="恒瑞精密制造股份有限公司", project_name="生产网安全加固咨询", project_type="技术咨询",
         project_amount=32000.0, start_date=date(2026, 5, 6), end_date=date(2026, 8, 31),
         project_status="进行中",
         project_summary="梳理生产网边界访问关系，输出分区隔离与最小权限方案，配合等保 2.0 三级整改。"),
    dict(company="市第一人民医院", project_name="HIS 数据库性能优化", project_type="技术咨询",
         project_amount=68000.0, start_date=date(2026, 11, 1), end_date=date(2027, 1, 31),
         project_status="待实施",
         project_summary="目标：门诊高峰期 HIS 主库 CPU 峰值由 95% 降至 70% 以下。\n"
                         "手段：慢查询治理、索引重建、读写分离评估。"),
    dict(company="启明教育科技有限公司", project_name="超融合平台规划", project_type="虚拟化",
         project_amount=210000.0, start_date=date(2026, 12, 1), end_date=date(2027, 3, 31),
         project_status="待实施",
         project_summary="现有 6 台老服务器，规划三节点超融合替换，需支持后续在线扩容与跨机房容灾。"),
]

DEMO_FILES = [
    ("华云信息技术有限公司", "AD域控维保项目", "2026年度AD域维保服务合同.pdf",
     b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
     b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
     b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R"
     b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
     b"4 0 obj<</Length 92>>stream\nBT /F1 18 Tf 60 760 Td (AD Maintenance Contract 2026) Tj ET\nendstream endobj\n"
     b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
     b"trailer<</Root 1 0 R>>\n%%EOF\n", "2026 年度维保合同，含 SLA 条款"),
    ("华云信息技术有限公司", "AD域控维保项目", "服务响应SLA说明.txt",
     "服务等级约定：\n1. 一般咨询：4 小时内响应\n2. 一般故障：2 小时内响应，24 小时内闭环\n"
     "3. 重大故障（影响全司登录）：30 分钟内响应，2 小时内到场\n".encode("utf-8"), "SLA 条款摘录"),
    ("华云信息技术有限公司", "VMware 虚拟化平台扩容", "扩容项目验收单.pdf",
     b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
     b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
     b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R"
     b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
     b"4 0 obj<</Length 90>>stream\nBT /F1 18 Tf 60 760 Td (Acceptance Report - VMware) Tj ET\nendstream endobj\n"
     b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
     b"trailer<</Root 1 0 R>>\n%%EOF\n", "客户签字验收单"),
    ("恒瑞精密制造股份有限公司", "存储阵列故障恢复", "故障处理报告.txt",
     "【故障时间】2026-03-12 09:20\n【恢复时间】2026-03-15 16:40\n"
     "【根因】控制器 B 缓存电池失效导致控制器自我保护关机\n"
     "【处理动作】更换控制器 -> 重建缓存 -> 数据一致性校验 -> 恢复业务\n"
     "【后续建议】阵列已过保，尽快立项替换\n".encode("utf-8"), "故障处理全过程记录"),
]


def make_file(name: str, content: bytes, remark: str, project: Project, upload_dir: Path) -> Attachment:
    import uuid
    ext = name.rsplit(".", 1)[-1].lower()
    store_name = f"{uuid.uuid4().hex}.{ext}"
    path = upload_dir / store_name
    path.write_bytes(content)
    return Attachment(
        project_id=project.id,
        file_original_name=name,
        file_store_name=store_name,
        file_path=str(path),
        file_ext=ext,
        file_size=path.stat().st_size,
        uploader="admin",
        upload_time=datetime.now(),
        remark=remark,
    )


DEMO_ACCOUNTS = [
    dict(username="liming", password="liming123", real_name="李明", department="售前部",
         phone="13611110001", email="liming@company.com", role="user", status="pending",
         apply_reason="负责售前技术支持，需要查看客户项目与合同附件。"),
    dict(username="wangfang", password="wangfang123", real_name="王芳", department="售后服务部",
         phone="13611110002", email="wangfang@company.com", role="user", status="active",
         apply_reason="售后服务工程师，需要维护项目记录。"),
    dict(username="zhaolei", password="zhaolei123", real_name="赵磊", department="商务部",
         phone="13611110003", email="zhaolei@company.com", role="user", status="disabled",
         apply_reason="商务对接，负责合同归档。"),
]


def main():
    reset = "--reset" in sys.argv
    Config.ensure_dirs()
    upload_dir = Path(Config.UPLOAD_FOLDER)

    with app.app_context():
        db.create_all()

        if reset:
            for att in db.session.scalars(db.select(Attachment)).all():
                remove_file_on_disk(att.file_path)
            db.session.query(Attachment).delete()
            db.session.query(Project).delete()
            db.session.query(Customer).delete()
            db.session.query(OperationLog).delete()
            db.session.execute(db.text("DELETE FROM user WHERE role != 'admin'"))
            db.session.commit()
            print("[重置] 已清空客户、项目、附件与日志")

        name_to_customer = {}
        for data in DEMO_CUSTOMERS:
            c = Customer(**data)
            db.session.add(c)
            name_to_customer[data["company_name"]] = c
        db.session.commit()
        print(f"[1/4] 写入 {len(DEMO_CUSTOMERS)} 个客户")

        name_to_project = {}
        for data in DEMO_PROJECTS:
            data = dict(data)
            customer = name_to_customer[data.pop("company")]
            p = Project(customer_id=customer.id, **data)
            db.session.add(p)
            name_to_project[data["project_name"]] = p
        db.session.commit()
        print(f"[2/4] 写入 {len(DEMO_PROJECTS)} 个项目")

        for company, pname, fname, content, remark in DEMO_FILES:
            project = name_to_project[pname]
            db.session.add(make_file(fname, content, remark, project, upload_dir))

        # 成员账号：覆盖「待审批 / 已启用 / 已停用」三种状态，便于体验审批流程
        account_count = 0
        for data in DEMO_ACCOUNTS:
            if db.session.scalar(db.select(User).where(User.username == data["username"])):
                continue
            password = data.pop("password")
            u = User(**data)
            u.set_password(password)
            db.session.add(u)
            account_count += 1
        db.session.commit()
        print(f"[4/4] 写入 {account_count} 个成员账号（含 1 个待审批，可在「账号管理」体验审批流程）")
        db.session.add(OperationLog(username="admin", action="新增", target_type="系统",
                                    detail="写入演示数据", ip="127.0.0.1"))
        db.session.commit()
        print(f"[3/4] 写入 {len(DEMO_FILES)} 个示例附件")

        print("\n演示数据写入完成，刷新浏览器即可看到。")
        print("如需清空演示数据重来：python seed_demo.py --reset 或删除 crm.db 与 uploads/ 后重新 init_db.py")


if __name__ == "__main__":
    main()
