# -*- coding: utf-8 -*-
"""一键打包为 Windows EXE

用法：
    python build_exe.py                # 单文件模式：dist/CRMSystem.exe
    python build_exe.py --onedir      # 目录模式：启动更快，dist/CRMSystem/ 文件夹
    python build_exe.py --name Foo    # 自定义输出名
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
NAME = "CRMSystem"


def sh(args, **kw):
    print("  $", " ".join(args))
    r = subprocess.run(args, **kw)
    if r.returncode != 0:
        sys.exit(f"\n命令失败（退出码 {r.returncode}）：{' '.join(args)}")


def main():
    ap = argparse.ArgumentParser(description="打包客户管理系统为 Windows EXE")
    ap.add_argument("--onedir", action="store_true", help="打成目录版（启动更快）")
    ap.add_argument("--name", default=NAME, help=f"输出名称，默认 {NAME}")
    ap.add_argument("--no-icon", action="store_true", help="不生成/使用应用图标")
    args = ap.parse_args()

    # 前置检查
    try:
        import PyInstaller  # noqa
        import waitress     # noqa
    except ImportError as e:
        sys.exit(f"缺少依赖 {e.name}，请先执行：{PY} -m pip install -r requirements-build.txt")

    print("=" * 56)
    print("  客户管理系统 -> Windows EXE 打包")
    print("=" * 56)

    # 1. 图标
    icon = ROOT / "build" / "app.ico"
    if not args.no_icon and not icon.exists():
        print("\n[1/3] 生成应用图标")
        sh([PY, str(ROOT / "tools" / "make_icon.py")])
    elif icon.exists():
        print(f"\n[1/3] 复用已有图标 {icon.name}")

    # 2. 打包
    print("\n[2/3] 运行 PyInstaller（需要几分钟，请耐心等待）...")
    env = os.environ.copy()
    env["CRM_BUILD_ONEFILE"] = "0" if args.onedir else "1"
    env["CRM_BUILD_NAME"] = args.name
    sh([PY, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "crm.spec")],
       cwd=str(ROOT), env=env)

    # 3. 产物说明
    dist = ROOT / "dist"
    out = dist / args.name if args.onedir else dist / f"{args.name}.exe"
    print("\n[3/3] 打包完成")
    print("-" * 56)
    if not out.exists():
        sys.exit(f"未找到产物：{out}")
    size_mb = (out.stat().st_size / 1024 / 1024) if out.is_file() else \
        sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"  产物路径 : {out}")
    print(f"  体积     : {size_mb:.1f} MB")
    print(f"  运行方式 : 双击 {out.name} 即可，浏览器自动打开 http://127.0.0.1:8081")
    print()
    if args.onedir:
        print("  注意：目录版请整个文件夹一起拷贝分发。")
    else:
        print("  说明：单文件 EXE 首次启动需解压，等待 3~10 秒属正常现象。")
    print("  提示 : 个别杀毒软件可能误报，添加信任即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
