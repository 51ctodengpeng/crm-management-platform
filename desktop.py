# -*- coding: utf-8 -*-
"""桌面版入口：打包成 EXE 后双击即启动 Web 服务并自动打开浏览器

用法（源码方式运行也一样）：
    python desktop.py                    # 默认 http://127.0.0.1:8081
    python desktop.py --port 9000        # 换端口
    python desktop.py --host 0.0.0.0     # 允许局域网内其它电脑访问
    python desktop.py --no-browser       # 不自动开浏览器
"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

# 打包后工作目录可能是任意路径，这里确保能 import 到项目模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 安装版（Inno Setup 装到 Program Files 并注册服务）会在安装目录写 installed.ini，
# 用它指定数据目录与监听端口。这一步必须发生在 import config 之前：config 一被导入
# 就会锁定 DATA_DIR，之后再改环境变量已经晚了。
from installinfo import prepare_env_from_install  # noqa: E402

INSTALLED_INFO = prepare_env_from_install()

from config import Config, DATA_DIR, is_frozen  # noqa: E402

DEFAULT_PORT = 8081
DEFAULT_HOST = "127.0.0.1"   # 默认只监听本机，安全；要给别人访问用 --host 0.0.0.0


# ============================== 控制台输出 ==============================
def _enable_ansi() -> bool:
    """Windows 控制台开启 ANSI 转义，让横幅有颜色"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


ANSI = _enable_ansi()
C = {
    "reset": "\033[0m" if ANSI else "",
    "dim": "\033[2m" if ANSI else "",
    "bold": "\033[1m" if ANSI else "",
    "cyan": "\033[36m" if ANSI else "",
    "blue": "\033[94m" if ANSI else "",
    "green": "\033[32m" if ANSI else "",
    "yellow": "\033[33m" if ANSI else "",
    "red": "\033[31m" if ANSI else "",
}


def _set_title(text: str):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def banner(host: str, port: int):
    url = f"http://127.0.0.1:{port}"
    line = "=" * 58
    print()
    print(f"{C['cyan']}{line}{C['reset']}")
    print(f"{C['bold']}{C['blue']}   客户管理系统  CRM System{C['reset']}"
          f"{C['dim']}   v2.0  {'(EXE)' if is_frozen() else '(源码)'}{C['reset']}")
    print(f"{C['cyan']}{line}{C['reset']}")
    print()
    print(f"  {C['green']}服务已启动，请在浏览器访问：{C['reset']}")
    print(f"      {C['bold']}{C['yellow']}{url}{C['reset']}")
    print()
    print(f"  {C['dim']}数据目录 : {DATA_DIR}{C['reset']}")
    print(f"  {C['dim']}数据库   : {Config.DB_PATH.name}{C['reset']}")
    print(f"  {C['dim']}附件目录 : {Config.UPLOAD_FOLDER}{C['reset']}")
    print()
    if host in ("0.0.0.0", "::"):
        print(f"  {C['yellow']}已开启局域网访问：同一网络的电脑可用「本机 IP:{port}」打开{C['reset']}")
    else:
        print(f"  {C['dim']}当前仅本机可访问；想让同事访问请加参数：--host 0.0.0.0{C['reset']}")
    print(f"  {C['dim']}关闭本窗口即停止服务（或按 Ctrl+C）{C['reset']}")
    print()
    print(f"{C['cyan']}{line}{C['reset']}")
    print(f"{C['dim']}   Powershell 中文社区团队开发   ·   www.powershell.com.cn{C['reset']}")
    print(f"{C['cyan']}{line}{C['reset']}")
    print()


# ============================== 端口与实例探测 ==============================
def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _looks_like_our_app(port: int) -> bool:
    """探测该端口上跑的是不是本系统（用于识别"重复双击"）"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理
        with opener.open(f"http://127.0.0.1:{port}/login", timeout=2) as resp:
            body = resp.read(4096).decode("utf-8", "ignore")
            return resp.status == 200 and ("login" in body or "客户管理" in body)
    except Exception:
        return False


def _find_free_port(start: int, limit: int = 30) -> int:
    for offset in range(limit):
        candidate = start + offset
        if not _port_in_use(candidate):
            return candidate
    raise SystemExit(f"从 {start} 起连续 {limit} 个端口都被占用，请用 --port 指定其它端口")


def _open_browser_later(url: str, delay: float = 1.5):
    def worker():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


# ============================== 主流程 ==============================
def parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(
        prog="CRMSystem",
        description="客户管理系统（本地 Web 服务）",
        add_help=True,
    )
    p.add_argument("--port", type=int, default=int(os.environ.get("CRM_PORT", DEFAULT_PORT)),
                   help=f"监听端口，默认 {DEFAULT_PORT}")
    p.add_argument("--host", default=os.environ.get("CRM_HOST", DEFAULT_HOST),
                   help="监听地址，默认 127.0.0.1（仅本机）；局域网访问用 0.0.0.0")
    p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("--no-gui", action="store_true",
                   help="跳过启动设置窗口，直接用 --port 或默认端口启动")
    p.add_argument("--reset-admin", metavar="PASSWORD",
                   help="把管理员账号 admin 的密码重置为指定值后退出")
    # ---- Windows 服务相关（安装包调用，普通用户一般不需要手动执行）----
    p.add_argument("--install-service", action="store_true",
                   help="注册为 Windows 服务并设为开机自启（需管理员权限）")
    p.add_argument("--remove-service", action="store_true",
                   help="停止并删除 Windows 服务（需管理员权限）")
    p.add_argument("--run-service", action="store_true",
                   help="以服务身份运行（由服务控制管理器调用，请勿手工执行）")
    return p.parse_args(argv)


def maybe_ask_port(args):
    """EXE 双击启动时弹出配置窗口：可改端口、回车确认、5 秒无操作用默认端口。

    返回 (port, auto)；用户关闭窗口表示取消启动，返回 None。
    以下情况直接跳过窗口：命令行显式指定了 --port / --reset-admin、
    加了 --no-gui、或源码方式运行（脚本/自动化场景不打扰）。
    """
    argv = sys.argv[1:]
    if "--port" in argv or "--reset-admin" in argv or "--no-gui" in argv:
        return args.port, False
    if not is_frozen() and os.environ.get("CRM_ASK_PORT") != "1":
        return args.port, False

    try:
        from launcher_ui import ask_port
        result = ask_port(default_port=DEFAULT_PORT, seconds=5)
    except Exception as exc:  # 窗口万一异常也不阻塞启动
        print(f"{C['dim']}（启动设置窗口不可用：{exc}，改用默认端口 {args.port}）{C['reset']}")
        return args.port, False

    if result is None:
        return None
    port, auto = result
    if port != args.port:
        print(f"{C['dim']}已按设置改用端口 {port}{C['reset']}")
    return port, auto


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # ---- Windows 服务模式：不走启动窗口、不打印横幅 ----
    if args.run_service:
        import win_service
        win_service.run_service()
        return 0
    if args.install_service:
        import win_service
        win_service.install_service()
        print(f"{C['green']}已注册 Windows 服务：{win_service.DISPLAY_NAME} "
              f"（{win_service.SERVICE_NAME}，开机自启）{C['reset']}")
        return 0
    if args.remove_service:
        import win_service
        win_service.remove_service()
        print(f"{C['green']}已删除 Windows 服务：{win_service.SERVICE_NAME}{C['reset']}")
        return 0

    picked = maybe_ask_port(args)
    if picked is None:
        print(f"{C['dim']}已取消启动，再见。{C['reset']}")
        return 0
    args.port, auto_started = picked
    if auto_started:
        print(f"{C['dim']}5 秒内未操作，已按默认端口 {args.port} 自动启动{C['reset']}")

    from app import app, BOOTSTRAP_RESULT

    # 首次运行提示默认管理员账号
    if BOOTSTRAP_RESULT.get("created_admin"):
        print()
        print(f"{C['yellow']}{'*' * 58}{C['reset']}")
        print(f"{C['yellow']}  首次运行，已自动创建管理员账号：{C['reset']}")
        print(f"{C['bold']}      账号 {BOOTSTRAP_RESULT['username']}   "
              f"密码 {BOOTSTRAP_RESULT['password']}{C['reset']}")
        print(f"{C['yellow']}  请登录后立即到「修改密码」中改掉默认密码！{C['reset']}")
        print(f"{C['yellow']}{'*' * 58}{C['reset']}")

    # 密码重置模式
    if args.reset_admin:
        from extensions import db
        from models import User
        with app.app_context():
            user = db.session.scalar(db.select(User).where(User.username == "admin"))
            if user is None:
                user = User(username="admin", real_name="admin", role="admin", status="active")
                db.session.add(user)
            user.set_password(args.reset_admin)
            user.role, user.status = "admin", "active"
            db.session.commit()
        # 安装向导里显式设过密码，之前自动生成的那份临时口令文件就该作废
        try:
            from bootstrap import INITIAL_PWD_FILE
            stale = DATA_DIR / INITIAL_PWD_FILE
            if stale.exists():
                stale.unlink()
        except Exception:
            pass
        print(f"{C['green']}已将管理员 admin 的密码重置为：{args.reset_admin}{C['reset']}")
        return 0

    # 重复双击：端口已被本系统占用，直接打开浏览器，不再起第二个实例
    if _port_in_use(args.port) and _looks_like_our_app(args.port):
        url = f"http://127.0.0.1:{args.port}"
        print(f"{C['yellow']}检测到服务已在运行，直接为你打开浏览器：{url}{C['reset']}")
        webbrowser.open(url)
        time.sleep(1.5)
        return 0

    port = _find_free_port(args.port) if _port_in_use(args.port) else args.port
    if port != args.port:
        print(f"{C['yellow']}端口 {args.port} 被其它程序占用，已自动改用 {port}{C['reset']}")

    banner(args.host, port)
    if not args.no_browser:
        _open_browser_later(f"http://127.0.0.1:{port}")

    _set_title(f"{Config.APP_NAME}  -  {port} 端口运行中")
    try:
        from waitress import serve
        print(f"{C['dim']}引擎：waitress（生产级 WSGI，非调试服务器）{C['reset']}")
        print()
        serve(app, host=args.host, port=port, threads=8, ident="CRM-System")
    except ImportError:
        print(f"{C['dim']}引擎：Flask 内置服务器（未安装 waitress）{C['reset']}")
        print()
        app.run(host=args.host, port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print(f"\n{C['cyan']}已停止服务，再见。{C['reset']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # 打包后双击运行，出错不能让窗口一闪而过
        import traceback
        print()
        print(f"{C['red']}启动失败：{exc}{C['reset']}")
        traceback.print_exc()
        # 只有“用户双击”这种交互场景才等回车。安装程序 / 服务控制管理器调用时
        # 没有控制台可交互，等回车会让安装流程永久卡死。
        _non_interactive = any(a in sys.argv[1:] for a in (
            "--install-service", "--remove-service", "--run-service", "--reset-admin"))
        if is_frozen() and not _non_interactive:
            try:
                input("\n按回车键关闭窗口...")
            except EOFError:
                pass
        sys.exit(1)
