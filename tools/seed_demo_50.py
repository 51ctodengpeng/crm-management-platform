# -*- coding: utf-8 -*-
"""生成 50 家虚构客户 + 对应项目（合计约 2000 万元）的演示数据。

合规约定（重要）：
- 所有企业名称均由「生造字号 + 行业词 + 组织形式」拼成，字号取生僻字组合，
  并统一加「（示例）」后缀，确保不与任何真实注册主体同名或近似；
- 电话用 138-0000-XXXX 这类占位段，邮箱统一 @example.com（RFC2606 保留域），
  微信为 wx_demo_ 前缀，地址中的路名取自同批生造字号；
- 每条记录的备注都写明「演示数据｜虚构主体」，便于审计与清理。

用法：
    CRM_DB_PATH=/tmp/demo.db python tools/seed_demo_50.py --dry-run
    CRM_DB_PATH=/tmp/demo.db python tools/seed_demo_50.py
    python tools/seed_demo_50.py --reset      # 先删除本脚本生成的数据再重建
"""
import argparse
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from config import Config  # noqa: E402
from extensions import db  # noqa: E402
from models import Customer, PaymentPlan, Project  # noqa: E402
from utils import option_items  # noqa: E402

MARK = "（示例）"
REMARK = "演示数据｜虚构主体，不对应任何真实企业或个人信息"

# ---------- 生造字号：两个字均取自生僻组合，不构成常见品牌词 ----------
ZI_A = ["泓", "昶", "翎", "熹", "澜", "屿", "栖", "昀", "叙", "沚",
        "烨", "箬", "焓", "璎", "湄", "珩", "岑", "栩", "沂", "璟"]
ZI_B = ["屿", "澜", "栖", "昀", "叙", "川", "珩", "栩", "沂", "璟",
        "湄", "岑", "沚", "烨", "箬", "焓", "璎", "泓", "昶", "翎"]

CITIES = [
    ("杭州市", "滨江区"), ("苏州市", "工业园区"), ("宁波市", "鄞州区"),
    ("合肥市", "高新区"), ("长沙市", "岳麓区"), ("郑州市", "金水区"),
    ("青岛市", "崂山区"), ("福州市", "鼓楼区"), ("无锡市", "新吴区"),
    ("东莞市", "松山湖"), ("常州市", "武进区"), ("嘉兴市", "南湖区"),
    ("泉州市", "丰泽区"), ("南昌市", "红谷滩区"), ("贵阳市", "观山湖区"),
    ("武汉市", "光谷"), ("成都市", "高新区"), ("西安市", "雁塔区"),
    ("济南市", "历下区"), ("厦门市", "思明区"),
]

ORG = ["智能科技有限公司", "信息技术有限公司", "数字科技有限公司",
       "系统集成有限公司", "软件技术有限公司"]

SURNAME = ["周", "沈", "顾", "陆", "林", "许", "程", "苏", "章", "邵",
           "闻", "钟", "简", "翟", "席", "柏", "阙", "宗", "滕", "季"]
GIVEN = ["予安", "亦舟", "知远", "清和", "书言", "思勉", "惟宁", "朗川",
         "念初", "叙白", "观澜", "承宇", "怀瑾", "静姝", "竹青", "栖迟",
         "望舒", "与同", "见山", "听雪"]

SELLERS = ["林可", "周予", "陆岑", "沈叙"]

SERVICES = [
    ("IT 运维服务", "运维服务"), ("桌面终端与域控维护", "运维服务"),
    ("数据备份与容灾建设", "系统集成"), ("网络改造与机房整理", "系统集成"),
    ("ERP 实施与流程梳理", "软件实施"), ("客户管理平台部署", "软件实施"),
    ("设备巡检与质保服务", "运维服务"), ("安全加固与等保整改", "安全服务"),
    ("视频会议系统升级", "系统集成"), ("服务器采购与上架", "硬件供货"),
]

STATUS = ["待实施", "实施中", "已验收", "已结项"]


def _coins(n: int, rng: random.Random) -> list[str]:
    seen, out = set(), []
    while len(out) < n:
        word = rng.choice(ZI_A) + rng.choice(ZI_B)
        if word in seen or ZI_A.index(word[0]) % 20 == ZI_B.index(word[1]) % 20:
            continue
        seen.add(word)
        out.append(word)
    return out


def _status_weights(options):
    """状态权重：在实施/已验收为主，终止少量，避免演示数据显得全是烂尾"""
    base = {"待实施": 25, "进行中": 38, "已验收": 26, "已终止": 6,
            "实施中": 35, "已结项": 28}
    return [base.get(x, 15) for x in options]


def _round2(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build(count: int = 50, seed: int = 20261017):
    rng = random.Random(seed)
    coins = _coins(count, rng)
    industries = option_items("customer_business") or ["制造业", "互联网", "政企"]
    sources = option_items("customer_source") or ["转介绍", "官网咨询"]
    levels = option_items("customer_level") or ["重要", "普通", "潜在"]
    ptypes = option_items("project_type") or ["运维服务", "系统集成"]
    pstatus = [x for x in (option_items("project_status") or STATUS)
               if x not in ("其他", "自定义…")] or STATUS
    modes = option_items("service_mode") or ["远程服务", "现场服务"]
    pays = option_items("pay_method") or ["支付软件转账"]

    today = date(2026, 10, 17)
    rows = []
    for i, coin in enumerate(coins):
        city, district = CITIES[i % len(CITIES)]
        ind = industries[i % len(industries)]
        company = f"{city}{coin}{ORG[i % len(ORG)]}{MARK}"
        person = rng.choice(SURNAME) + rng.choice(GIVEN)
        created = today - timedelta(days=rng.randint(20, 330))
        last_follow = created + timedelta(days=rng.randint(3, 40))
        if last_follow > today:
            last_follow = today - timedelta(days=2)
        rows.append({
            "company_name": company,
            "contact_name": person,
            "position": rng.choice(["技术总监", "项目经理", "采购经理", "总经理助理", "IT 主管"]),
            "contact_phone": f"1380000{i + 1:04d}",
            "contact_email": f"demo{i + 1:02d}@example.com",
            "wechat": f"wx_demo_{i + 1:02d}",
            "region": city,
            "address": f"{city}{district}{coin}路 {rng.randint(8, 288)} 号 {rng.randint(1, 6)} 号楼",
            "business_type": ind,
            "customer_level": rng.choice(levels),
            "source_channel": rng.choice(sources),
            "remark": REMARK,
            "last_follow_at": last_follow,
            "next_follow_at": today + timedelta(days=rng.randint(-6, 21)),
            "owner": SELLERS[i % len(SELLERS)],
            "created_at": datetime.combine(created, datetime.min.time()) + timedelta(
                hours=rng.randint(9, 18), minutes=rng.randint(0, 59)),
            "_coin": coin,
            "_projects": [],
        })

    # 项目：50 家里 12 家做 2 个项目，其余 1 个 → 62 个
    multi = set(rng.sample(range(count), 12))
    seq = 0
    for idx, row in enumerate(rows):
        for k in range(2 if idx in multi else 1):
            seq += 1
            svc, ptype = SERVICES[(idx * 2 + k) % len(SERVICES)]
            # 金额分三档，模拟真实结构：少数大单、一批中单、多数中小单
            tier = rng.choices([0, 1, 2], weights=[12, 30, 58])[0]
            if tier == 0:
                net = round(rng.uniform(880_000, 1_380_000), -2)
            elif tier == 1:
                net = round(rng.uniform(330_000, 720_000), -2)
            else:
                net = round(rng.uniform(72_000, 300_000), -2)
            rate = 13 if "硬件" in svc else 6
            sign = today - timedelta(days=rng.randint(25, 320))
            months = rng.choice([6, 12, 12, 18, 24])
            row["_projects"].append({
                "project_name": f"{row['_coin']} {sign.year} 年 {svc}",
                "project_type": ptype if ptype in ptypes else ptypes[0],
                "project_amount": _round2(net),
                "tax_rate": rate,
                "sign_date": sign,
                "start_date": sign + timedelta(days=rng.randint(3, 20)),
                "end_date": sign + timedelta(days=months * 30),
                "project_status": rng.choices(
                    pstatus, weights=_status_weights(pstatus))[0] if pstatus else "实施中",
                "service_mode": rng.choice(modes),
                "payment_method": rng.choice(pays),
                "owner": row["owner"],
                "contract_no": f"HT-{sign.year}-{seq:04d}",
                "payment_terms": "预付款 30%，验收后付 40%，质保期满付尾款 30%",
                "credit_days": rng.choice([15, 30, 30, 45, 60]),
                "project_summary": f"{row['company_name']} 委托的{svc}，含现场实施与一年质保。",
            })
    return rows


def org_prefix(company: str) -> str:
    """从公司名里取出「城市+字号」这一段，用于项目名，避免项目名过长"""
    tail = company
    for c in ("科技有限公司", "信息技术有限公司", "智能装备有限公司",
              "系统集成有限公司", "数字科技有限公司"):
        if tail.endswith(c + MARK):
            return tail[:-len(c + MARK)][2:]
    return tail[:6]


def scale_to(rows, target_net: float):
    """按比例缩放到目标不含税总额，并保持金额分布形态"""
    cur = sum(p["project_amount"] for r in rows for p in r["_projects"])
    factor = target_net / cur
    for r in rows:
        for p in r["_projects"]:
            p["project_amount"] = float(
                (Decimal(str(p["project_amount"] * factor)) / 100).quantize(Decimal("1")) * 100
            )


def payload(rows):
    out = []
    for r in rows:
        out.append({
            "company_name": r["company_name"],
            "contact_name": r["contact_name"],
            "position": r["position"],
            "contact_phone": r["contact_phone"],
            "contact_email": r["contact_email"],
            "wechat": r["wechat"],
            "region": r["region"],
            "address": r["address"],
            "business_type": r["business_type"],
            "customer_level": r["customer_level"],
            "source_channel": r["source_channel"],
            "remark": r["remark"],
            "last_follow_at": r["last_follow_at"].isoformat(),
            "next_follow_at": r["next_follow_at"].isoformat(),
            "owner": r["owner"],
            "created_at": r["created_at"].isoformat(sep=" "),
            "projects": [{
                **{k: v for k, v in p.items() if k in (
                    "project_name", "project_type", "project_amount", "tax_rate",
                    "project_status", "service_mode", "payment_method", "owner",
                    "contract_no", "payment_terms", "credit_days", "project_summary")},
                "sign_date": p["sign_date"].isoformat(),
                "start_date": p["start_date"].isoformat(),
                "end_date": p["end_date"].isoformat(),
            } for p in r["_projects"]],
        })
    return out


def insert(data):
    created_c, created_p, created_plan = 0, 0, 0
    for item in data:
        if db.session.scalar(db.select(Customer).filter_by(company_name=item["company_name"])):
            continue
        cust = Customer(
            company_name=item["company_name"], contact_name=item["contact_name"],
            position=item["position"], contact_phone=item["contact_phone"],
            contact_email=item["contact_email"], wechat=item["wechat"],
            region=item["region"], address=item["address"],
            business_type=item["business_type"], customer_level=item["customer_level"],
            source_channel=item["source_channel"], remark=item["remark"],
            last_follow_at=date.fromisoformat(item["last_follow_at"]),
            next_follow_at=date.fromisoformat(item["next_follow_at"]),
            owner=item["owner"],
            created_at=datetime.fromisoformat(item["created_at"]),
        )
        db.session.add(cust)
        db.session.flush()
        created_c += 1
        for p in item["projects"]:
            net = p["project_amount"]
            gross = _round2(net * (1 + p["tax_rate"] / 100.0))
            proj = Project(
                customer_id=cust.id,
                project_name=p["project_name"], project_type=p["project_type"],
                project_amount=net, tax_rate=p["tax_rate"], tax_included_amount=gross,
                start_date=date.fromisoformat(p["start_date"]),
                end_date=date.fromisoformat(p["end_date"]),
                project_status=p["project_status"], service_mode=p["service_mode"],
                payment_method=p["payment_method"], owner=p["owner"],
                contract_no=p["contract_no"], sign_date=date.fromisoformat(p["sign_date"]),
                payment_terms=p["payment_terms"], credit_days=p["credit_days"],
                project_summary=p["project_summary"],
                created_at=datetime.fromisoformat(item["created_at"]),
            )
            db.session.add(proj)
            db.session.flush()
            created_p += 1
            for idx, (ratio, note, offset) in enumerate(
                    [(30, "预付款", 7), (40, "验收款", 90), (30, "尾款", 180)], start=1):
                db.session.add(PaymentPlan(
                    project_id=proj.id, seq=idx,
                    due_date=date.fromisoformat(p["sign_date"]) + timedelta(days=offset),
                    ratio=ratio, amount=_round2(gross * ratio / 100.0), note=note,
                ))
                created_plan += 1
    db.session.commit()
    return created_c, created_p, created_plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--target", type=float, default=20_500_000,
                    help="不含税总额目标（默认 2050 万，含税约 2150 万）")
    ap.add_argument("--reset", action="store_true", help="先删除本脚本生成的数据")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    app = create_app(Config)
    with app.app_context():
        rows = build(args.count)
        scale_to(rows, args.target)
        data = payload(rows)

        net = sum(p["project_amount"] for it in data for p in it["projects"])
        gross = sum(_round2(p["project_amount"] * (1 + p["tax_rate"] / 100.0))
                    for it in data for p in it["projects"])
        print(f"客户 {len(data)} 家｜项目 {sum(len(i['projects']) for i in data)} 个")
        print(f"不含税合计 {net:,.2f} 元（{net / 10000:,.1f} 万）")
        print(f"含税合计   {gross:,.2f} 元（{gross / 10000:,.1f} 万）")

        if args.dry_run:
            return

        if args.reset:
            names = [it["company_name"] for it in data]
            old = db.session.scalars(
                db.select(Customer).where(Customer.company_name.in_(names))).all()
            for c in old:
                db.session.delete(c)
            db.session.commit()
            print(f"已清理旧数据 {len(old)} 家客户（其项目/回款计划级联删除）")
        c, p, plan = insert(data)
        print(f"写入完成：客户 {c} 家、项目 {p} 个、回款计划 {plan} 期")


if __name__ == "__main__":
    main()
