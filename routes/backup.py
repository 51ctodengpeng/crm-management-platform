# -*- coding: utf-8 -*-
"""路由层：数据备份与恢复

这里只做 HTTP 相关的事：取表单、校验权限与参数、调用 backup_store、给提示、跳转。
真正"库怎么快照、zip 怎么打、怎么灌回去"的逻辑在 backup_store.py，便于单独演进与测试。

对外的 URL、表单字段名、提示文案、下载文件名都与拆分前完全一致。
"""
import tempfile
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import login_required

from backup_store import (
    create_backup,
    db_info,
    human_size,
    list_backups,
    restore_from_zip,
)
from config import Config
from utils import admin_required, log_action

bp = Blueprint("backup", __name__)

# 兼容：历史上调用方会从本模块导入 create_backup（详见 backup_store.py）。
__all__ = ["bp", "create_backup"]


@bp.route("/")
@login_required
@admin_required
def index():
    upload_folder = Path(Config.UPLOAD_FOLDER)
    return render_template(
        "backup/index.html",
        backups=list_backups(),
        db_info=db_info(),
        human=human_size,
        upload_count=sum(1 for p in upload_folder.rglob("*") if p.is_file())
        if upload_folder.exists() else 0,
        backup_dir=str(Path(Config.DATA_DIR) / "backups"),
    )


@bp.route("/create", methods=["POST"])
@login_required
@admin_required
def create():
    try:
        path = create_backup(note=(request.form.get("note") or "").strip()[:120])
    except OSError as exc:
        current_app.logger.exception("备份失败")
        flash(f"备份失败：{exc}", "danger")
        return redirect(url_for("backup.index"))
    log_action("备份", "系统", None, f"生成数据备份：{path.name}")
    flash(f"备份已生成：{path.name}（{human_size(path.stat().st_size)}）", "success")
    return redirect(url_for("backup.index"))


def _resolve_backup(name: str) -> Path | None:
    """把 URL 里的文件名解析成 backups 目录内的真实路径，越界或不存在返回 None

    备份包名来自 URL，必须限定在 backups 目录内，否则 ../ 就能读到任意文件。
    """
    folder = (Path(Config.DATA_DIR) / "backups").resolve()
    target = (folder / name).resolve()
    if target.parent != folder or not target.exists():
        return None
    return target


@bp.route("/<path:name>/download")
@login_required
@admin_required
def download(name: str):
    """下载备份文件：只允许读取 backups 目录内已存在的 zip"""
    target = _resolve_backup(name)
    if target is None:
        flash("备份文件不存在", "danger")
        return redirect(url_for("backup.index"))
    log_action("下载", "系统", None, f"下载数据备份：{name}")
    return send_file(target, as_attachment=True, download_name=name)


@bp.route("/<path:name>/delete", methods=["POST"])
@login_required
@admin_required
def delete(name: str):
    target = _resolve_backup(name)
    if target is not None:
        target.unlink()
        log_action("删除", "系统", None, f"删除数据备份：{name}")
        flash(f"已删除备份 {name}", "success")
    else:
        flash("备份文件不存在", "danger")
    return redirect(url_for("backup.index"))


@bp.route("/restore", methods=["POST"])
@login_required
@admin_required
def restore():
    """恢复数据：可上传备份包，也可选择服务器上已有的备份"""
    confirm = (request.form.get("confirm") or "").strip().upper()
    if confirm != "RESTORE":
        flash("请在下方的确认框里输入 RESTORE 后再执行恢复", "warning")
        return redirect(url_for("backup.index"))

    uploaded = request.files.get("file")
    name = (request.form.get("name") or "").strip()
    tmp_upload = None
    if uploaded and uploaded.filename:
        if not uploaded.filename.lower().endswith(".zip"):
            flash("请上传本系统生成的 .zip 备份包", "danger")
            return redirect(url_for("backup.index"))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as fh:
            uploaded.save(fh)
            tmp_upload = Path(fh.name)
        source = tmp_upload
    elif name:
        resolved = _resolve_backup(name)
        if resolved is None:
            flash("选择的备份不存在", "danger")
            return redirect(url_for("backup.index"))
        source = resolved
    else:
        flash("请选择要恢复的备份，或上传一个备份包", "warning")
        return redirect(url_for("backup.index"))

    ok, msg = restore_from_zip(source)
    if tmp_upload:
        tmp_upload.unlink(missing_ok=True)
    log_action("恢复", "系统", None, f"{msg}（来源：{source.name}）")
    flash(msg + ("，已重新加载数据" if ok else ""), "success" if ok else "danger")
    return redirect(url_for("backup.index"))
