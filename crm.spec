# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置

默认打成单个 EXE（双击即用）；要打目录版（启动更快、免解压）：
    set CRM_BUILD_ONEFILE=0 && pyinstaller --noconfirm crm.spec
"""
import os

SPECDIR = SPECPATH if isinstance(SPECPATH, str) else os.getcwd()
ONEFILE = os.environ.get("CRM_BUILD_ONEFILE", "1") == "1"
NAME = os.environ.get("CRM_BUILD_NAME", "CRMSystem")

ICON = os.path.join(SPECDIR, "build", "app.ico")
ICON = ICON if os.path.exists(ICON) else None

# ---------- 随包数据：模板 + 静态资源 ----------
datas = [
    (os.path.join(SPECDIR, "templates"), "templates"),
    (os.path.join(SPECDIR, "static"), "static"),
]

# ---------- 隐式导入 ----------
# 这些库靠字符串/运行时动态导入，静态分析扫不到，必须显式列出
hiddenimports = [
    "waitress",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.dialects.mssql",
    "wtforms.validators",
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",
    "openpyxl.workbook",
    "tkinter",
    "tkinter.ttk",
]

# ---------- 排除无用大模块：减小体积、降低杀软误报概率 ----------
excludes = [
    "matplotlib", "numpy", "pandas", "scipy", "PIL", "IPython",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "notebook", "jupyter",
    "wx", "gunicorn", "curses", "lib2to3",
]

a = Analysis(
    [os.path.join(SPECDIR, "desktop.py")],
    pathex=[SPECDIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                 # UPX 会明显提高杀软误报率，不启用
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,              # 保留控制台：能看到服务状态与报错
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        icon=ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=NAME,
    )
