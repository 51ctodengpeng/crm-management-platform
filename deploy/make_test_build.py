# -*- coding: utf-8 -*-
"""生成一份仅用于本地验证的安装脚本变体，并编译。

为什么要变体：真实安装包 PrivilegesRequired=admin，会弹 UAC；当前会话没有管理员
权限，弹窗后无人点击。变体只改三处，其余与正式脚本完全一致，用来验证：
  · 文件落位、开始菜单/桌面快捷方式
  · installed.ini 是否正确写入（含中文路径编码）
  · 数据目录是否被正确创建、数据库是否初始化
  · 卸载是否清干净

真正无法在无提权环境验证的只有：服务注册、防火墙放行、Program Files 安装。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "deploy" / "installer.iss"
DST = ROOT / "deploy" / "_test.iss"
ISCC = Path(r"C:\Users\dengpeng\AppData\Local\Programs\Inno Setup 6\ISCC.exe")


def main():
    src = SRC.read_text(encoding="utf-8-sig")
    text = src
    subs = [
        ('#define MyDataDir "{commonappdata}\\CRM客户管理系统"',
         '#define MyDataDir "D:\\tmp\\crmdata"'),
        ("PrivilegesRequired=admin", "PrivilegesRequired=lowest"),
        ("OutputDir=output", "OutputDir=output-test"),
        ('OutputBaseFilename=CRM客户管理系统_安装程序_v{#MyAppVersion}',
         'OutputBaseFilename=CRM_测试构建_v{#MyAppVersion}'),
    ]
    for old, new in subs:
        if old not in text:
            sys.exit("脚本里找不到待替换内容：%r" % old)
        text = text.replace(old, new)

    DST.write_text(text, encoding="utf-8-sig")
    print("已生成", DST.name)
    r = subprocess.run([str(ISCC), str(DST)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines():
        if line.startswith(("Successful", "Error", "Compile aborted")) or "Resulting Setup" in line:
            print(" ", line)
    if r.returncode != 0:
        print((r.stdout or "")[-1500:])
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
