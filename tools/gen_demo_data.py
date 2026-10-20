# -*- coding: utf-8 -*-
"""生成贴近真实业务的测试数据：50 个客户 + 100 个项目（含收款/对账记录）

特点：
- 固定随机种子（20261017），同一份脚本每次生成同一批数据，便于复现问题；
- 所有字段都有值：客户含姓名/手机/邮箱/微信/公司/职位/省市区/地址/行业/等级/来源/备注，
  项目含名称/客户/类型/状态/服务方式/付款方式/金额/税点/含税价/起止日期/描述；
- 手机号用真实号段、邮箱域名合理、地区与地址一致、项目金额与项目体量匹配；
- 100 个项目金额合计约 2160 万（默认目标），偏差自动校正到 ±0.5% 以内。

用法：
    python tools/gen_demo_data.py                 # 预演，只打印统计，不写库
    python tools/gen_demo_data.py --apply         # 追加写入当前库
    python tools/gen_demo_data.py --apply --reset # 先清空客户/项目/收款再写入（危险）
    python tools/gen_demo_data.py --apply --db D:/path/crm.db
"""
import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

TARGET_TOTAL = 21_600_000.0      # 目标总金额：2160 万
SEED = 20261017
TODAY = date(2026, 10, 17)

# ============================== 基础素材 ==============================
# (公司字号, 拼音, 行业, 公司后缀)
BRANDS = [
    ("华远", "huayuan", "制造业", "科技"), ("智联", "zhilian", "互联网", "信息技术"),
    ("恒泰", "hengtai", "金融", "科技"), ("明远", "mingyuan", "教育", "软件"),
    ("中科", "zhongke", "政企", "信息技术"), ("云海", "yunhai", "互联网", "网络"),
    ("瑞德", "ruide", "医疗", "医疗科技"), ("天成", "tiancheng", "制造业", "智能装备"),
    ("立信", "lixin", "金融", "数据服务"), ("金域", "jinyu", "医疗", "健康科技"),
    ("博通", "botong", "互联网", "网络"), ("联创", "lianchuang", "政企", "系统集成"),
    ("汇智", "huizhi", "教育", "教育科技"), ("宏图", "hongtu", "制造业", "精密制造"),
    ("佳和", "jiahe", "医疗", "医疗器械"), ("创元", "chuangyuan", "互联网", "软件"),
    ("世纪", "shiji", "政企", "信息服务"), ("太和", "taihe", "医疗", "生物科技"),
    ("安泰", "antai", "金融", "金融服务"), ("鼎信", "dingxin", "政企", "信息科技"),
    ("蓝盾", "landun", "政企", "网络安全"), ("沃德", "wode", "制造业", "自动化"),
    ("拓普", "tuopu", "制造业", "电子"), ("龙腾", "longteng", "互联网", "云计算"),
    ("华信", "huaxin", "金融", "咨询"), ("兴业", "xingye", "金融", "资本管理"),
    ("优联", "youlian", "互联网", "电商科技"), ("数联", "shulian", "互联网", "数据科技"),
    ("智海", "zhihai", "教育", "教育集团"), ("联诚", "liancheng", "制造业", "机电"),
    ("康泰", "kangtai", "医疗", "医药"), ("中盛", "zhongsheng", "制造业", "新材料"),
    ("远大", "yuanda", "制造业", "重工"), ("银河", "yinhe", "互联网", "数字科技"),
    ("天源", "tianyuan", "政企", "能源科技"), ("汇川", "huichuan", "制造业", "传动技术"),
    ("精诚", "jingcheng", "医疗", "检验"), ("启明", "qiming", "教育", "培训"),
    ("睿智", "ruizhi", "教育", "职教科技"), ("鼎盛", "dingsheng", "金融", "财富管理"),
    ("嘉华", "jiahua", "互联网", "传媒科技"), ("华创", "huachuang", "政企", "创业服务"),
    ("星辰", "xingchen", "互联网", "卫星数据"), ("元丰", "yuanfeng", "制造业", "化工"),
    ("锦程", "jincheng", "互联网", "物流科技"), ("海纳", "haina", "教育", "人才服务"),
    ("融通", "rongtong", "金融", "支付科技"), ("正邦", "zhengbang", "制造业", "包装"),
    ("广联", "guanglian", "政企", "智慧城市"), ("同辉", "tonghui", "医疗", "医院管理"),
]

CITIES = [
    ("广东省", "深圳市", ["南山区", "福田区", "宝安区"]),
    ("广东省", "广州市", ["天河区", "番禺区", "黄埔区"]),
    ("北京市", "北京市", ["海淀区", "朝阳区", "丰台区"]),
    ("上海市", "上海市", ["浦东新区", "徐汇区", "闵行区"]),
    ("浙江省", "杭州市", ["西湖区", "滨江区", "余杭区"]),
    ("江苏省", "南京市", ["玄武区", "江宁区", "鼓楼区"]),
    ("四川省", "成都市", ["高新区", "武侯区", "金牛区"]),
    ("湖北省", "武汉市", ["洪山区", "江汉区", "东湖新技术开发区"]),
    ("陕西省", "西安市", ["雁塔区", "高新区", "未央区"]),
    ("江苏省", "苏州市", ["工业园区", "姑苏区", "吴中区"]),
    ("山东省", "青岛市", ["市南区", "崂山区", "黄岛区"]),
    ("湖南省", "长沙市", ["岳麓区", "芙蓉区", "开福区"]),
    ("天津市", "天津市", ["滨海新区", "南开区", "河西区"]),
    ("重庆市", "重庆市", ["渝北区", "江北区", "九龙坡区"]),
    ("安徽省", "合肥市", ["蜀山区", "包河区", "高新区"]),
    ("福建省", "福州市", ["鼓楼区", "仓山区", "晋安区"]),
    ("河南省", "郑州市", ["金水区", "郑东新区", "高新区"]),
    ("山东省", "济南市", ["历下区", "高新区", "槐荫区"]),
]

ROADS = ["科技园路", "高新大道", "创业一路", "中山大道", "人民路", "解放路", "软件园东路",
         "天河北路", "湖滨路", "文化路", "金融街", "产业园二路", "航站路", "滨江大道"]

POSITIONS = ["IT 经理", "运维主管", "技术总监", "信息中心主任", "采购经理", "总经理",
             "系统架构师", "网络工程师", "项目经理", "行政主管", "信息科科长", "基础设施负责人",
             "运维经理", "信息化专员"]

DOMAINS = ["qq.com", "163.com", "126.com", "outlook.com", "sina.com", "foxmail.com"]

SURNAMES = ["张", "王", "李", "赵", "陈", "刘", "杨", "黄", "周", "吴",
            "徐", "孙", "马", "朱", "胡", "郭", "何", "高", "林", "罗",
            "郑", "梁", "谢", "宋", "唐"]

GIVEN = ["伟民", "晓东", "建华", "志强", "海涛", "俊杰", "文斌", "丽娟", "秀兰", "国庆",
         "立新", "春燕", "永强", "雅琴", "振宇", "小峰", "雪梅", "明辉", "文轩", "思远",
         "家豪", "梦琪", "志刚", "一鸣", "雅雯", "子健", "佳怡", "浩然", "静怡", "嘉伟",
         "雨欣", "天宇", "若彤", "泽宇", "欣怡", "书豪", "婉婷", "柏霖", "语彤", "俊豪",
         "子轩", "梦洁", "宇航", "晨曦", "博文", "雨薇", "浩宇", "诗涵", "家乐", "思琪"]

TOPICS = [
    ("AD 域控升级与终端纳管", "AD域"),
    ("虚拟化平台扩容改造", "虚拟化"),
    ("核心存储双活改造", "存储故障"),
    ("园区核心网络割接", "网络"),
    ("机房搬迁与业务割接", "其他"),
    ("邮件系统迁移至 Exchange Online", "其他"),
    ("备份容灾体系一期建设", "存储故障"),
    ("桌面云与终端标准化部署", "虚拟化"),
    ("超融合集群实施", "虚拟化"),
    ("等保 2.0 安全加固整改", "其他"),
    ("堡垒机与运维审计上线", "其他"),
    ("服务器利旧整合与性能调优", "技术咨询"),
    ("数据库读写分离改造", "技术咨询"),
    ("VPN 远程接入改造", "网络"),
    ("监控告警平台建设", "其他"),
    ("机房动环与环境监控改造", "其他"),
    ("Windows Server 版本升级", "AD域"),
    ("文件服务器迁移与权限梳理", "AD域"),
    ("双机热备与高可用改造", "存储故障"),
    ("IT 运维外包驻场服务", "技术咨询"),
]

STATUS_PLAN = (
    ["已验收"] * 42 + ["进行中"] * 28 + ["待实施"] * 14 + ["已终止"] * 6
    + ["已回款"] * 5 + ["暂停中"] * 3 + ["待客户验收"] * 2
)

REMARK_HEAD = ["老客户", "通过招投标中标", "客户介绍", "长期合作", "续约客户", "首次合作", "行业标杆"]
REMARK_TAIL = ["响应要求高，需 4 小时内到场", "对接人偏好微信沟通", "季度回访一次",
               "涉及多家供应商协同", "合同走年度框架", "验收需提供完整交付文档",
               "对价格敏感，关注性价比", "内部流程较长，回款周期约 60 天"]


def build_customers(rng: random.Random) -> list[dict]:
    """50 个客户：字段全填、彼此不重复"""
    names = []
    pool = GIVEN[:]
    rng.shuffle(pool)
    for i in range(50):
        names.append(SURNAMES[i % len(SURNAMES)] + pool[i % len(pool)])
    # 极端情况下可能重名，做一次去重兜底
    names = list(dict.fromkeys(names))
    while len(names) < 50:
        names.append(SURNAMES[len(names) % 25] + pool[(len(names) * 7) % len(pool)])

    used_phone, used_email, used_wx = set(), set(), set()
    customers = []
    for i, (brand, py, industry, suffix) in enumerate(BRANDS):
        province, city, districts = CITIES[i % len(CITIES)]
        district = rng.choice(districts)
        company = f"{city}{brand}{suffix}有限公司"

        # 手机号：真实号段 + 随机后 8 位，保证唯一
        while True:
            phone = f"1{rng.choice('3589')}{rng.choice('0123456789')}{rng.randrange(10**7, 10**8)}"
            if phone not in used_phone:
                used_phone.add(phone)
                break

        # 邮箱：企业域名 / 常见邮箱服务商两种，保证唯一
        if i % 3 == 0:
            email = f"{py}.it@{py}-tech.com"
        elif i % 3 == 1:
            email = f"{py}{rng.randrange(10, 99)}@{rng.choice(DOMAINS)}"
        else:
            email = f"{py}.{rng.choice(['contact', 'service', 'purchase'])}@{rng.choice(DOMAINS)}"
        if email in used_email:
            email = f"{py}{i}@{rng.choice(DOMAINS)}"
        used_email.add(email)

        wechat = f"wxid_{py}{rng.randrange(1000, 9999)}"
        while wechat in used_wx:
            wechat = f"wxid_{py}{rng.randrange(1000, 9999)}"
        used_wx.add(wechat)

        address = (f"{city}{district}{rng.choice(ROADS)}{rng.randrange(8, 588)}号"
                   f"{rng.choice(['A', 'B', 'C', 'D'])}座{rng.randrange(3, 26)}层")

        customers.append({
            "company_name": company,
            "brand": brand,
            "contact_name": names[i],
            "position": rng.choice(POSITIONS),
            "phone": phone,
            "email": email,
            "wechat": wechat,
            "province": province,
            "city": city,
            "district": district,
            "region": f"{province} {city} {district}",
            "address": address,
            "business_type": industry,
            "customer_level": rng.choices(["重要", "普通", "潜在"], weights=[3, 5, 2])[0],
            "source_channel": rng.choice(
                ["互联网", "客户介绍", "老客户续约", "展会活动", "电话拜访", "渠道代理",
                 "招投标", "行业社群"]),
            "remark": f"{rng.choice(REMARK_HEAD)}；{rng.choice(REMARK_TAIL)}",
        })
    return customers


def build_projects(rng: random.Random, customers: list[dict]) -> list[dict]:
    """100 个项目：每个客户 1~3 个，金额合计对齐目标总额"""
    # 先保证每个客户至少 1 个项目，再随机分配剩余
    owners = list(range(len(customers)))
    extra = 100 - len(owners)
    owners += rng.choices(range(len(customers)), k=extra)
    rng.shuffle(owners)

    statuses = STATUS_PLAN[:]
    rng.shuffle(statuses)

    projects = []
    used_names = {}
    for idx, cid in enumerate(owners):
        cust = customers[cid]
        topic, ptype = TOPICS[idx % len(TOPICS)]
        base = f"{cust['brand']}{topic}"
        used_names[base] = used_names.get(base, 0) + 1
        name = base if used_names[base] == 1 else f"{base}（{used_names[base]} 期）"

        # 金额分档：小 25% / 中 40% / 大 25% / 超大 10%
        band = rng.choices(["s", "m", "l", "xl"], weights=[25, 40, 25, 10])[0]
        amount = {
            "s": rng.randrange(28000, 82000, 100),
            "m": rng.randrange(120000, 250000, 100),
            "l": rng.randrange(250000, 460000, 100),
            "xl": rng.randrange(460000, 780000, 100),
        }[band]

        status = statuses[idx]
        duration = rng.randrange(14, 120)

        if status == "待实施":
            start = TODAY + timedelta(days=rng.randrange(5, 90))
            end = start + timedelta(days=duration)
        elif status == "进行中":
            start = TODAY - timedelta(days=rng.randrange(10, 90))
            end = max(TODAY + timedelta(days=rng.randrange(10, 70)), start + timedelta(days=duration))
        elif status in ("已终止", "暂停中"):
            start = TODAY - timedelta(days=rng.randrange(200, 500))
            end = start + timedelta(days=rng.randrange(20, 70))
        else:                                   # 已验收 / 已回款 / 待客户验收
            start = TODAY - timedelta(days=rng.randrange(90, 620))
            end = start + timedelta(days=duration)

        service_mode = rng.choices(["远程服务", "现场服务", "远程+现场"],
                                   weights=[6, 3, 2])[0]
        payment_method = rng.choices(["支付软件转账", "现金支付"], weights=[8, 2])[0]
        tax_rate = rng.choices([0, 3, 6, 9, 13], weights=[1, 1, 10, 1, 3])[0]

        summary_mode = {
            "远程服务": "以远程方式实施，工作时段内响应，变更统一走窗口期。",
            "现场服务": "工程师驻场实施，含 2 次现场巡检与一次交接培训。",
            "远程+现场": "前期远程评估与方案确认，关键割接环节安排现场支持。",
        }[service_mode]
        summary = (f"{cust['company_name']}的{topic}。范围包含现状调研、方案设计、实施落地与验收交付；"
                   f"{summary_mode}交付物含实施记录、拓扑与配置文档、验收报告。")

        projects.append({
            "customer_index": cid,
            "project_name": name,
            "project_type": ptype,
            "project_amount": float(amount),
            "tax_rate": float(tax_rate),
            "service_mode": service_mode,
            "payment_method": payment_method,
            "project_status": status,
            "start_date": start,
            "end_date": end,
            "project_summary": summary,
        })

    # ---- 金额校正：整体缩放到目标总额，再按百元取整，残差补到一条中等记录 ----
    raw_total = sum(p["project_amount"] for p in projects)
    factor = TARGET_TOTAL / raw_total
    for p in projects:
        p["project_amount"] = max(8000.0, round(p["project_amount"] * factor / 100) * 100)

    diff = TARGET_TOTAL - sum(p["project_amount"] for p in projects)
    candidates = [p for p in projects if 100000 <= p["project_amount"] <= 400000]
    if candidates:
        fix = candidates[len(candidates) // 2]
        fix["project_amount"] = round((fix["project_amount"] + diff) / 100) * 100

    return projects


def build_payments(rng: random.Random, projects: list[dict]) -> list[dict]:
    """按项目状态生成收款与对账记录：已验收多数全额到账，进行中多为 30% 首付款"""
    payments = []
    seq = 0
    for p in projects:
        status = p["project_status"]
        receivable = round(p["project_amount"] * (1 + p["tax_rate"] / 100), 2)
        if status in ("已回款", "已验收"):
            roll = rng.random()
            if roll < 0.55:
                plans = [(1.0, "matched")]
            elif roll < 0.82:
                plans = [(0.4, "matched"), (0.6, "pending")]
            elif roll < 0.92:
                plans = [(0.3, "matched")]
            else:
                continue
        elif status in ("进行中", "待客户验收"):
            if rng.random() < 0.55:
                plans = [(0.3, "matched")]
            elif rng.random() < 0.5:
                plans = [(0.3, "matched"), (0.2, "pending")]
            else:
                continue
        else:
            continue

        span = max(1, (p["end_date"] - p["start_date"]).days)
        for ratio, state in plans:
            seq += 1
            amount = round(receivable * ratio, 2)
            method = p["payment_method"]
            is_transfer = method == "支付软件转账"
            channel = rng.choice(["微信", "支付宝", "银行转账", "云闪付"]) if is_transfer else ""
            trade_no = ""
            if is_transfer:
                prefix = {"微信": "WX", "支付宝": "ALI", "银行转账": "BANK", "云闪付": "UP"}[channel]
                trade_no = f"{prefix}{TODAY:%Y%m%d}{seq:04d}{rng.randrange(1000, 9999)}"
            paid = p["start_date"] + timedelta(days=rng.randrange(0, span + 10))
            payments.append({
                "project_index": projects.index(p),
                "amount": amount,
                "pay_method": method,
                "pay_channel": channel,
                "trade_no": trade_no,
                "payer": p["customer_company"],
                "paid_at": paid,
                "status": state,
                "remark": "首付款" if ratio < 0.5 else ("尾款" if ratio >= 0.9 else "进度款"),
            })
    return payments


# ============================== 落库 ==============================
def apply_to_db(customers, projects, payments, reset=False):
    from app import create_app
    from extensions import db
    from models import Customer, Payment, Project

    app = create_app()
    with app.app_context():
        if reset:
            Payment.query.delete()
            Project.query.delete()
            Customer.query.delete()
            db.session.commit()

        name_to_customer = {}
        for c in customers:
            obj = Customer(
                company_name=c["company_name"], contact_name=c["contact_name"],
                position=c["position"], contact_phone=c["phone"],
                contact_email=c["email"], wechat=c["wechat"],
                region=c["region"], address=c["address"],
                business_type=c["business_type"], customer_level=c["customer_level"],
                source_channel=c["source_channel"], remark=c["remark"],
            )
            db.session.add(obj)
            name_to_customer[c["company_name"]] = obj
        db.session.commit()

        created_projects = []
        for p in projects:
            cust = db.session.get(Customer, None)  # 占位，下面按名字取
            cust = name_to_customer[p["customer_company"]]
            obj = Project(
                customer_id=cust.id, project_name=p["project_name"],
                project_type=p["project_type"], project_status=p["project_status"],
                service_mode=p["service_mode"], payment_method=p["payment_method"],
                tax_rate=p["tax_rate"], start_date=p["start_date"],
                end_date=p["end_date"], project_summary=p["project_summary"],
            )
            obj.apply_net(p["project_amount"], p["tax_rate"])
            db.session.add(obj)
            created_projects.append(obj)
        db.session.commit()

        if reset:
            Payment.query.delete()
            db.session.commit()
        for pay in payments:
            proj = created_projects[pay["project_index"]]
            db.session.add(Payment(
                project_id=proj.id, amount=pay["amount"], pay_method=pay["pay_method"],
                pay_channel=pay["pay_channel"], trade_no=pay["trade_no"], payer=pay["payer"],
                paid_at=pay["paid_at"], status=pay["status"], remark=pay["remark"],
                created_by="系统导入",
                reconciled_at=datetime.now() if pay["status"] == "matched" else None,
                reconciled_by="系统导入" if pay["status"] == "matched" else None,
            ))
        db.session.commit()

        total = db.session.scalar(db.select(db.func.sum(Project.project_amount))) or 0
        return len(created_projects), len(payments), float(total)


def main():
    parser = argparse.ArgumentParser(description="生成 50 客户 + 100 项目测试数据")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库")
    parser.add_argument("--reset", action="store_true", help="写库前清空客户/项目/收款")
    parser.add_argument("--db", help="指定数据库文件（等价于设置 CRM_DB_PATH）")
    args = parser.parse_args()

    if args.db:
        import os
        os.environ["CRM_DB_PATH"] = str(Path(args.db).resolve())

    rng = random.Random(SEED)
    customers = build_customers(rng)
    projects = build_projects(rng, customers)
    for p in projects:
        p["customer_company"] = customers[p["customer_index"]]["company_name"]
    payments = build_payments(rng, projects)

    total = sum(p["project_amount"] for p in projects)
    gross = sum(p["project_amount"] * (1 + p["tax_rate"] / 100) for p in projects)
    print(f"客户 {len(customers)} 个 / 项目 {len(projects)} 个 / 收款 {len(payments)} 笔")
    print(f"项目金额合计：{total:,.2f} 元（{total / 10000:,.1f} 万）")
    print(f"含税合计：    {gross:,.2f} 元")
    print(f"偏差：{(total - TARGET_TOTAL) / TARGET_TOTAL * 100:+.3f}%")

    from collections import Counter
    print("状态分布：", dict(Counter(p["project_status"] for p in projects)))
    print("服务方式：", dict(Counter(p["service_mode"] for p in projects)))
    print("付款方式：", dict(Counter(p["payment_method"] for p in projects)))
    print("金额分档：",
          f"最小 {min(p['project_amount'] for p in projects):,.0f} / "
          f"最大 {max(p['project_amount'] for p in projects):,.0f}")
    print("收款：", dict(Counter(p["status"] for p in payments)),
          f"合计 {sum(p['amount'] for p in payments):,.2f} 元")

    print("\n样例客户：", customers[0]["company_name"], customers[0]["contact_name"],
          customers[0]["phone"], customers[0]["email"], customers[0]["region"])
    print("样例项目：", projects[0]["project_name"], projects[0]["project_amount"],
          projects[0]["project_status"], projects[0]["service_mode"])
    print("样例收款：", payments[0] if payments else "无")

    dup_company = len(customers) - len({c["company_name"] for c in customers})
    dup_phone = len(customers) - len({c["phone"] for c in customers})
    dup_email = len(customers) - len({c["email"] for c in customers})
    trades = [p["trade_no"] for p in payments if p["trade_no"]]
    dup_trade = len(trades) - len(set(trades))
    empty = [c for c in customers if not all(str(v).strip() for v in c.values())]
    empty_p = [p for p in projects
               if not all(str(p.get(k, "")).strip()
                          for k in ("project_name", "project_type", "project_status",
                                    "service_mode", "payment_method", "project_summary"))]
    print(f"\n唯一性检查：公司重复 {dup_company} / 手机重复 {dup_phone} / "
          f"邮箱重复 {dup_email} / 流水号重复 {dup_trade}")
    print(f"空字段检查：客户 {len(empty)} 条 / 项目 {len(empty_p)} 条")

    if not args.apply:
        print("\n（预演模式，未写入数据库。加 --apply 写库）")
        return 0

    n_proj, n_pay, db_total = apply_to_db(customers, projects, payments, reset=args.reset)
    print(f"\n已写入：项目 {n_proj} 个，收款 {n_pay} 笔；库内项目总额 {db_total:,.2f} 元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
