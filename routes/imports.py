# -*- coding: utf-8 -*-
"""路由层：Excel 批量导入（客户 / 项目）

流程刻意分成两步：
  1) 上传 -> 解析校验 -> 预览（每行标注「可导入 / 跳过 / 错误」以及原因）
  2) 确认后才真正写库，避免选错文件直接污染数据
预览结果暂存在数据目录的临时 JSON 里（不塞 session，避免 Cookie 超限），
session 只保存一个随机 token，提交时按 token 取回。
"""
import json
import re
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import current_user, login_required
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from config import Config
from extensions import db
from models import Customer, Project, normalize_tax_rate
from permissions import apply_customer_scope, can_create_data
from utils import (
    clean_owner,
    log_action,
    parse_amount,
    parse_date,
    resolve_custom_choice,
)

bp = Blueprint("imports", __name__)

MAX_ROWS = 500
TOKEN_KEY = "import_token"
# 同一会话同时保留的暂存 token 数（多开标签页各自预览再分别提交）
TOKENS_KEY = "import_tokens"
MAX_SESSION_TOKENS = 5
# 暂存文件保留时长：用户传完文件不提交，超过这个时间就自动清掉，
# 避免数据目录里长期堆着含客户手机号 / 邮箱的明文 JSON
STAGE_TTL_HOURS = 24 * 7

# 列定义：(表头, 字段, 是否必填, 说明)
CUSTOMER_COLUMNS = [
    ("公司名称", "company_name", True, "必填，重复名称会自动跳过"),
    ("对接人", "contact_name", False, "客户主要联系人"),
    ("职位", "position", False, ""),
    ("联系电话", "contact_phone", False, "手机或座机"),
    ("邮箱", "contact_email", False, ""),
    ("微信号", "wechat", False, ""),
    ("所在地区", "region", False, "省 / 市"),
    ("详细地址", "address", False, ""),
    ("行业类型", "business_type", False, "需与「选项管理」中的行业类型一致，否则按自定义值保存"),
    ("客户等级", "customer_level", False, "重要 / 普通 / 潜在"),
    ("来源渠道", "source_channel", False, ""),
    ("负责人", "owner", False, "销售归属"),
    ("备注", "remark", False, ""),
]

PROJECT_COLUMNS = [
    ("客户公司名称", "customer_name", True, "必填，须与系统内客户名称完全一致"),
    ("项目名称", "project_name", True, "必填"),
    ("项目类型", "project_type", False, ""),
    ("服务方式", "service_mode", False, "远程服务 / 现场服务 / 远程+现场"),
    ("付款方式", "payment_method", False, ""),
    ("税率%", "tax_rate", False, "默认 6"),
    ("含税金额", "tax_included_amount", False, "数字，可带千分位"),
    ("开始日期", "start_date", False, "2026-01-31 或 Excel 日期格式"),
    ("结束日期", "end_date", False, "不得早于开始日期"),
    ("项目状态", "project_status", False, ""),
    ("负责人", "owner", False, ""),
    ("合同编号", "contract_no", False, ""),
    ("签订日期", "sign_date", False, ""),
    ("账期(天)", "credit_days", False, "默认 30"),
    ("付款条款", "payment_terms", False, ""),
    ("项目简述", "project_summary", False, ""),
]

KINDS = {
    "customer": {"label": "客户", "columns": CUSTOMER_COLUMNS, "model": Customer},
    "project": {"label": "项目", "columns": PROJECT_COLUMNS, "model": Project},
}


# ============================== 模板 ==============================
def _template(kind: str) -> BytesIO:
    meta = KINDS[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = f"{meta['label']}导入"
    headers = [c[0] for c in meta["columns"]]
    ws.append(headers)
    fill = PatternFill("solid", fgColor="006EFF")
    for idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(idx)].width = max(12, len(header) * 2 + 6)
    ws.freeze_panes = "A2"

    # 示例行：帮助填写，导入时会按「公司名称」等必填项校验，示例数据不必删除也能识别
    if kind == "customer":
        ws.append(["示例科技有限公司", "张三", "技术总监", "13800000000", "zhangsan@example.com",
                   "zhangsan_wx", "广东 深圳", "南山区科技园 1 号", "互联网", "普通",
                   "客户介绍", "", "示例行，导入前请删除"])
    else:
        ws.append(["示例科技有限公司", "示例项目", "虚拟化", "远程服务", "支付软件转账", 6, 50000,
                   "2026-01-01", "2026-03-31", "进行中", "", "HT-2026-001", "2026-01-01", 30,
                   "预付 50%，验收后结清", "示例行，导入前请删除"])

    tip = wb.create_sheet("填写说明")
    tip.append(["列名", "是否必填", "说明"])
    for name, _, required, note in meta["columns"]:
        tip.append([name, "必填" if required else "选填", note])
    tip.column_dimensions["A"].width = 18
    tip.column_dimensions["B"].width = 10
    tip.column_dimensions["C"].width = 60
    for cell in tip[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@bp.route("/template/<kind>.xlsx")
@login_required
def download_template(kind: str):
    if kind not in KINDS:
        flash("未知的导入类型", "danger")
        return redirect(url_for("imports.index"))
    buf = _template(kind)
    return send_file(buf, as_attachment=True,
                     download_name=f"{KINDS[kind]['label']}导入模板.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ============================== 解析 ==============================
def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_name(name: str) -> str:
    """公司名归一化：去空格与全角括号差异，用于查重"""
    text = (name or "").strip().lower()
    return re.sub(r"[\s（）()]+", "", text)


def _row_payload(kind: str, raw: dict) -> dict:
    """把一行原始文本转成业务字段（不做存在性校验）"""
    data = {}
    if kind == "customer":
        data["company_name"] = raw.get("company_name", "")[:128]
        data["contact_name"] = raw.get("contact_name", "")[:64]
        data["position"] = raw.get("position", "")[:32]
        data["contact_phone"] = raw.get("contact_phone", "")[:32]
        data["contact_email"] = raw.get("contact_email", "")[:128]
        data["wechat"] = raw.get("wechat", "")[:64]
        data["region"] = raw.get("region", "")[:64]
        data["address"] = raw.get("address", "")
        data["business_type"] = raw.get("business_type", "")[:32]
        data["customer_level"] = raw.get("customer_level", "")[:16] or "普通"
        data["source_channel"] = raw.get("source_channel", "")[:32]
        data["owner"] = clean_owner(raw.get("owner", ""))
        data["remark"] = raw.get("remark", "")
    else:
        data["customer_name"] = raw.get("customer_name", "")
        data["project_name"] = raw.get("project_name", "")[:128]
        data["project_type"] = raw.get("project_type", "")[:32]
        data["service_mode"] = raw.get("service_mode", "")[:32]
        data["payment_method"] = raw.get("payment_method", "")[:32]
        data["tax_rate"] = normalize_tax_rate(raw.get("tax_rate") or 6)
        data["tax_included_amount"] = parse_amount(raw.get("tax_included_amount"))
        data["start_date"] = parse_date(raw.get("start_date", ""))
        data["end_date"] = parse_date(raw.get("end_date", ""))
        data["project_status"] = raw.get("project_status", "")[:16]
        data["owner"] = clean_owner(raw.get("owner", ""))
        data["contract_no"] = raw.get("contract_no", "")[:64]
        data["sign_date"] = parse_date(raw.get("sign_date", ""))
        try:
            data["credit_days"] = max(0, min(365, int(float(raw.get("credit_days") or 30))))
        except (TypeError, ValueError):
            data["credit_days"] = 30
        data["payment_terms"] = raw.get("payment_terms", "")
        data["project_summary"] = raw.get("project_summary", "")
    return data


def _parse_workbook(kind: str, stream) -> tuple[list[dict], list[str]]:
    """读取并校验 Excel，返回 (行结果列表, 全局错误)"""
    meta = KINDS[kind]
    try:
        # read_only 走流式读取，不会把整本工作簿展开到内存：
        # 一个 20MB 的 xlsx 解压后可能有几百 MB，一次性 load 会把 worker 撑爆
        wb = load_workbook(stream, data_only=True, read_only=True)
    except Exception:
        return [], ["文件无法解析，请使用 .xlsx 模板（不支持 .xls 老格式）"]

    ws = wb[wb.sheetnames[0]]
    header_cells = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    header_row = [_cell_text(value) for value in header_cells]
    header_map = {name: idx for idx, name in enumerate(header_row) if name}
    missing = [c[0] for c in meta["columns"] if c[2] and c[0] not in header_map]
    if missing:
        return [], [f"缺少必填列：{'、'.join(missing)}（请下载最新模板）"]

    existing_customers = {}
    for c in db.session.scalars(apply_customer_scope(db.select(Customer))).all():
        existing_customers.setdefault(_normalize_name(c.company_name), c)

    project_keys = set()
    if kind == "project":
        for pid, cid, pname in db.session.execute(
            db.select(Project.id, Project.customer_id, Project.project_name)
        ).all():
            project_keys.add((cid, (pname or "").strip().lower()))

    results, seen = [], set()
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(_cell_text(v) == "" for v in row):
            continue
        if len(results) >= MAX_ROWS:
            results.append({"row": row_idx, "status": "error", "message":
                            f"超过单次 {MAX_ROWS} 行上限，请拆分文件后再导入", "data": {}})
            break

        raw = {}
        for name, field, _, _ in meta["columns"]:
            idx = header_map.get(name)
            raw[field] = _cell_text(row[idx]) if idx is not None and idx < len(row) else ""

        item = {"row": row_idx, "status": "new", "message": "", "data": {}}

        if kind == "customer":
            data = _row_payload(kind, raw)
            if not data["company_name"]:
                item.update(status="error", message="公司名称为空")
            elif _normalize_name(data["company_name"]) in existing_customers:
                item.update(status="skip", message="系统中已存在同名客户，已跳过")
            elif _normalize_name(data["company_name"]) in seen:
                item.update(status="skip", message="文件内重复的公司名称，已跳过")
            else:
                item["data"] = data
            seen.add(_normalize_name(data["company_name"]))
        else:
            data = _row_payload(kind, raw)
            cust = existing_customers.get(_normalize_name(data["customer_name"]))
            key = (cust.id if cust else None, (data["project_name"] or "").strip().lower())
            if not data["customer_name"]:
                item.update(status="error", message="客户公司名称为空")
            elif not cust:
                item.update(status="error", message=f"系统中找不到客户「{data['customer_name']}」，请先导入客户")
            elif not data["project_name"]:
                item.update(status="error", message="项目名称为空")
            elif data["start_date"] and data["end_date"] and data["end_date"] < data["start_date"]:
                item.update(status="error", message="结束日期早于开始日期")
            elif key in project_keys:
                item.update(status="skip", message="该客户下已存在同名项目，已跳过")
            else:
                data["customer_id"] = cust.id
                item["data"] = data
                project_keys.add(key)
        results.append(item)
    return results, []


# ============================== 暂存 ==============================
def _stage_dir() -> Path:
    folder = Path(Config.DATA_DIR) / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _stage_path(token: str) -> Path:
    return _stage_dir() / f"import-{token}.json"


def _sweep_stage(now: datetime | None = None) -> int:
    """清掉过期的暂存文件，返回删除个数

    只在写入新暂存文件时顺手做一次，成本极低；不引入定时任务。
    """
    cutoff = (now or datetime.now()).timestamp() - STAGE_TTL_HOURS * 3600
    removed = 0
    try:
        for path in _stage_dir().glob("import-*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def _save_stage(kind: str, results: list[dict]) -> str:
    _sweep_stage()
    token = uuid.uuid4().hex
    payload = {"kind": kind, "created_at": datetime.now().isoformat(), "rows": results}
    _stage_path(token).write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    session[TOKEN_KEY] = token
    # 记下本会话签发过的 token（有上限），提交时据此判断"这个 token 是不是我的"，
    # 同时保证多标签页各自预览后再分别提交仍然可用
    issued = [t for t in (session.get(TOKENS_KEY) or []) if t != token]
    session[TOKENS_KEY] = (issued + [token])[-MAX_SESSION_TOKENS:]
    return token


def _load_stage(token: str):
    path = _stage_path(token or "")
    if not token or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _drop_stage(token: str):
    if token:
        _stage_path(token).unlink(missing_ok=True)
        issued = [t for t in (session.get(TOKENS_KEY) or []) if t != token]
        session[TOKENS_KEY] = issued
    session.pop(TOKEN_KEY, None)


def _owned_token(token: str | None) -> str | None:
    """只接受本会话签发过的 token，其它一律当作失效"""
    if token and token in (session.get(TOKENS_KEY) or []):
        return token
    return None


# ============================== 路由 ==============================
@bp.route("/")
@login_required
def index():
    if not can_create_data(current_user):
        flash("没有新增数据的权限，无法使用批量导入", "danger")
        return redirect(url_for("index"))
    return render_template("imports/index.html", kinds=KINDS, max_rows=MAX_ROWS)


@bp.route("/preview", methods=["POST"])
@login_required
def preview():
    if not can_create_data(current_user):
        flash("没有新增数据的权限，无法使用批量导入", "danger")
        return redirect(url_for("index"))

    kind = request.form.get("kind") or "customer"
    if kind not in KINDS:
        flash("请选择导入类型", "danger")
        return redirect(url_for("imports.index"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择要导入的 Excel 文件", "warning")
        return redirect(url_for("imports.index"))
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("只支持 .xlsx 格式，请先下载模板填写", "danger")
        return redirect(url_for("imports.index"))

    results, errors = _parse_workbook(kind, file.stream)
    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("imports.index"))
    if not results:
        flash("文件里没有可导入的数据行", "warning")
        return redirect(url_for("imports.index"))

    token = _save_stage(kind, results)
    counts = {
        "new": sum(1 for r in results if r["status"] == "new"),
        "skip": sum(1 for r in results if r["status"] == "skip"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    return render_template(
        "imports/preview.html", kind=kind, meta=KINDS[kind], rows=results,
        counts=counts, token=token, columns=KINDS[kind]["columns"],
    )


@bp.route("/commit", methods=["POST"])
@login_required
def commit():
    if not can_create_data(current_user):
        flash("没有新增数据的权限，无法使用批量导入", "danger")
        return redirect(url_for("index"))

    token = _owned_token(request.form.get("token") or session.get(TOKEN_KEY))
    payload = _load_stage(token)
    if not payload:
        flash("导入会话已失效，请重新上传文件", "warning")
        return redirect(url_for("imports.index"))

    kind = payload.get("kind")
    rows = [r for r in payload.get("rows", []) if r.get("status") == "new"]
    if not rows:
        _drop_stage(token)
        flash("没有可导入的行，请检查预览中的错误提示", "warning")
        return redirect(url_for("imports.index"))

    created, failed = 0, []
    for row in rows:
        data = row.get("data") or {}
        try:
            if kind == "customer":
                obj = Customer(**data)
            else:
                cust = db.session.get(Customer, data.get("customer_id"))
                if not cust:
                    failed.append(f"第 {row['row']} 行：客户已被删除")
                    continue
                fields = {k: v for k, v in data.items()
                          if k not in ("customer_name", "tax_included_amount")}
                # 暂存 JSON 会把 date 序列化成字符串，写库前要还原成 date
                for key in ("start_date", "end_date", "sign_date"):
                    if isinstance(fields.get(key), str):
                        fields[key] = parse_date(fields[key])
                fields["project_type"] = resolve_custom_choice(
                    form={"project_type": data.get("project_type")},
                    field="project_type", fallback="其他") or "其他"
                fields["project_status"] = resolve_custom_choice(
                    form={"project_status": data.get("project_status")},
                    field="project_status", fallback="待实施") or "待实施"
                fields["service_mode"] = data.get("service_mode") or "远程服务"
                fields["payment_method"] = data.get("payment_method") or "支付软件转账"
                obj = Project(**fields)
                obj.apply_gross(data.get("tax_included_amount") or 0, data.get("tax_rate"))
            db.session.add(obj)
            db.session.commit()
            created += 1
        except Exception as exc:                     # 单行失败不影响其他行
            db.session.rollback()
            failed.append(f"第 {row['row']} 行：{exc}")

    _drop_stage(token)
    if created:
        log_action("导入", KINDS[kind]["label"], None,
                   f"批量导入 {KINDS[kind]['label']} {created} 条"
                   + (f"，失败 {len(failed)} 条" if failed else ""))
        flash(f"成功导入 {created} 条{KINDS[kind]['label']}数据", "success")
    if failed:
        for msg in failed[:5]:
            flash(f"导入失败 · {msg}", "danger")
        if len(failed) > 5:
            flash(f"还有 {len(failed) - 5} 条失败记录未显示", "warning")
    target = "customer.list_customers" if kind == "customer" else "project.list_projects"
    return redirect(url_for(target))
