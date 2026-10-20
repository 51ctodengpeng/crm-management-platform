# -*- coding: utf-8 -*-
"""路由层：附件上传 / 下载 / 预览 / 删除 / 打包下载"""
import io
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import login_required
from werkzeug.utils import secure_filename

from extensions import db
from models import Attachment, Project
from permissions import PERM_DELETE, PERM_EDIT, PERM_VIEW, has_project_permission
from utils import (
    allowed_file,
    log_action,
    option_items,
    remove_file_on_disk,
    resolve_abs_path,
    save_upload,
)

bp = Blueprint("attachment", __name__)


def _clean_category(value: str | None) -> str:
    """附件分类：必须是字典里启用的分类，非法值回落「其他」"""
    text = (value or "").strip()
    categories = option_items("attachment_category")
    if text in categories:
        return text
    return "其他"


def version_key(project_id: int, filename: str) -> str:
    """版本归并键：项目 + 文件名主干（忽略扩展名与大小写）

    用主干而不是全名，是因为「合同.docx」与「合同.pdf」通常也属于同一份材料的
    不同格式，归到一组更符合使用习惯。
    """
    stem = Path(filename or "").stem.strip().lower()[:120]
    return f"{project_id}:{stem}" if stem else ""


def _guard(project_id: int, action: str) -> bool:
    """附件操作继承所属项目的权限位；无权限时给出提示"""
    if has_project_permission(project_id, action):
        return True
    label = {"view": "查看", "edit": "修改", "delete": "删除"}.get(action, "操作")
    flash(
        f"没有该项目附件的{label}权限，如需开通请联系管理员在「权限管理」中授权",
        "danger",
    )
    return False


@bp.route("/upload/<int:pid>", methods=["POST"])
@login_required
def upload(pid: int):
    project = db.get_or_404(Project, pid)
    if not _guard(pid, PERM_EDIT):
        return redirect(url_for("project.detail", pid=pid) + "#attachments")
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("请选择要上传的文件", "warning")
        return redirect(url_for("project.detail", pid=pid))

    remark = (request.form.get("remark") or "").strip()
    category = _clean_category(request.form.get("category"))
    max_mb = current_app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    ok_count, errors, versioned = 0, [], 0

    for fs in files:
        ok, reason = allowed_file(fs.filename or "")
        if not ok:
            errors.append(f"{fs.filename}：{reason}")
            continue

        meta, err = save_upload(fs)
        if err or not meta:
            errors.append(err or f"{fs.filename}：保存失败")
            continue

        if meta["file_size"] > current_app.config["MAX_CONTENT_LENGTH"]:
            remove_file_on_disk(meta["file_path"])
            errors.append(f"{meta['file_original_name']}：超过 {max_mb}MB 限制")
            continue

        # 版本归并：同一项目下「文件名主干」相同的视为同一份文件的不同版本，
        # 新上传的版本号 +1，旧版本保留但标记为非最新（列表默认只显示最新版）
        key = version_key(pid, meta["file_original_name"])
        prev = db.session.scalars(
            db.select(Attachment).where(Attachment.version_key == key)
            .order_by(Attachment.version.desc())
        ).all()
        version = (prev[0].version or 1) + 1 if prev else 1
        for old in prev:
            if old.is_latest:
                old.is_latest = False
                versioned += 1

        db.session.add(Attachment(project_id=pid, remark=remark, category=category,
                                  version=version, version_key=key, is_latest=True, **meta))
        ok_count += 1

    if ok_count:
        db.session.commit()
        log_action("上传", "附件", pid,
                   f"向项目「{project.project_name}」上传 {ok_count} 个附件（{category}）")
        tip = f"成功上传 {ok_count} 个附件，已归入「{category}」"
        if versioned:
            tip += f"；{versioned} 个旧版本已归档"
        flash(tip, "success")
    for e in errors:
        flash(f"上传失败 · {e}", "danger")

    return redirect(url_for("project.detail", pid=pid) + "#attachments")


def _download_name(att: Attachment) -> str:
    """下载时使用的文件名：保留中文，剔除危险字符"""
    safe = secure_filename(att.file_original_name)
    suffix = Path(att.file_original_name).suffix.lstrip(".")
    return att.file_original_name if safe in ("", suffix) else safe


@bp.route("/<int:aid>/download")
@login_required
def download(aid: int):
    att = db.get_or_404(Attachment, aid)
    if not _guard(att.project_id, PERM_VIEW):
        return redirect(url_for("project.list_projects"))
    path = resolve_abs_path(att.file_path)
    if not path or not path.exists():
        flash("文件在服务器上已丢失，请重新上传", "danger")
        return redirect(url_for("project.detail", pid=att.project_id))

    log_action("下载", "附件", att.id, f"下载附件：{att.file_original_name}")
    resp = send_file(
        path,
        as_attachment=True,
        download_name=_download_name(att),
        mimetype="application/octet-stream",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/download-all/<int:pid>")
@login_required
def download_all(pid: int):
    """把某个项目的全部附件打包成 zip 一次下载，便于合同发票归档"""
    project = db.get_or_404(Project, pid)
    if not _guard(pid, PERM_VIEW):
        return redirect(url_for("project.list_projects"))
    if not project.attachments:
        flash("该项目还没有附件可打包", "warning")
        return redirect(url_for("project.detail", pid=pid) + "#attachments")

    buf = io.BytesIO()
    added, used_names = 0, set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for att in sorted(project.attachments, key=lambda a: a.id):
            path = resolve_abs_path(att.file_path)
            if not path or not path.exists():
                continue
            # zip 内重名自动加序号，避免互相覆盖
            arcname = att.file_original_name or att.file_store_name
            stem, dot, ext = arcname.rpartition(".")
            base, idx = arcname, 1
            while arcname in used_names:
                arcname = f"{stem}({idx}).{ext}" if dot else f"{base}({idx})"
                idx += 1
            used_names.add(arcname)
            zf.write(path, arcname=arcname)
            added += 1

    if not added:
        flash("附件文件在服务器上已丢失，无法打包", "danger")
        return redirect(url_for("project.detail", pid=pid) + "#attachments")

    buf.seek(0)
    log_action("下载", "附件", pid,
               f"打包下载项目「{project.project_name}」的 {added} 个附件")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe_project = secure_filename(project.project_name) or f"project_{pid}"
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"{safe_project}_附件_{stamp}.zip",
        mimetype="application/zip",
    )


@bp.route("/<int:aid>/preview")
@login_required
def preview(aid: int):
    """图片 / PDF / 文本在线预览"""
    att = db.get_or_404(Attachment, aid)
    if not _guard(att.project_id, PERM_VIEW):
        return redirect(url_for("project.list_projects"))
    path = resolve_abs_path(att.file_path)
    if not path or not path.exists():
        abort(404)

    ext = (att.file_ext or "").lower()

    # Office 文档（docx / pptx / xlsx）与纯文本走「文本提取」预览，
    # 让用户不下载也能确认内容是否为要找的那份材料
    if ext in {"docx", "pptx", "xlsx", "txt", "csv", "md", "log", "json"}:
        from office_text import extract_text
        text, note = extract_text(path, ext)
        return render_template(
            "attachment/preview_text.html", attachment=att, text=text, note=note,
            project=att.project,
        )

    # 不显式带 charset，交给 Flask 按 mimetype 自行补全，避免出现重复的 charset 参数
    mimetype = {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp",
    }.get(ext)
    if not mimetype:
        flash("该类型文件不支持在线预览，请下载后查看", "warning")
        return redirect(url_for("project.detail", pid=att.project_id))

    return send_file(path, mimetype=mimetype, as_attachment=False,
                     download_name=att.file_original_name)


@bp.route("/<int:aid>/delete", methods=["POST"])
@login_required
def delete(aid: int):
    att = db.get_or_404(Attachment, aid)
    pid = att.project_id
    if not _guard(pid, PERM_DELETE):
        return redirect(url_for("project.detail", pid=pid) + "#attachments")
    name = att.file_original_name

    removed = remove_file_on_disk(att.file_path)  # 先删磁盘，再删数据库记录
    db.session.delete(att)
    db.session.commit()
    log_action("删除", "附件", aid, f"删除附件：{name}")
    flash(f"附件「{name}」已删除" + ("" if removed else "（磁盘文件原本已不存在）"), "success")
    return redirect(url_for("project.detail", pid=pid) + "#attachments")


@bp.route("/<int:aid>/remark", methods=["POST"])
@login_required
def update_remark(aid: int):
    """快速修改附件备注（例：把合同标注为 2026 年度）"""
    att = db.get_or_404(Attachment, aid)
    if not _guard(att.project_id, PERM_EDIT):
        return redirect(url_for("project.detail", pid=att.project_id) + "#attachments")
    att.remark = (request.form.get("remark") or "").strip()
    db.session.commit()
    log_action("修改", "附件", aid, f"修改附件备注：{att.file_original_name}")
    flash("附件备注已更新", "success")
    return redirect(url_for("project.detail", pid=att.project_id) + "#attachments")
