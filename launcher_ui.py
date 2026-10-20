# -*- coding: utf-8 -*-
"""启动配置窗口：设置端口、回车确认、5 秒无操作自动用默认端口

设计要点：
- 只在 EXE 双击启动（或设 CRM_ASK_PORT=1）时弹出；命令行带 --port 时直接跳过；
- 窗口内固定展示开发团队与官网信息；
- 用户一旦开始输入，倒计时自动暂停（避免"正打着字被自动启动"的糟糕体验）；
- tkinter 不可用时返回 None，由 desktop.py 回落为纯控制台启动，不影响功能。
"""
import socket

TEAM = "Powershell 中文社区团队开发"
SITE = "www.powershell.com.cn"

BG = "#FFFFFF"
HEADER_BG = "#F2F7FF"
ACCENT = "#006EFF"
TEXT = "#1F2D3D"
MUTED = "#7C8DA6"
DANGER = "#D93026"


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _next_free(port: int, limit: int = 30) -> int:
    for offset in range(limit):
        if not _port_in_use(port + offset):
            return port + offset
    return port


def ask_port(default_port: int = 8081, seconds: int = 5):
    """弹出配置窗口。返回 (port, auto_started)；用户关闭窗口=取消启动，返回 None。

    tkinter 不可用时抛 RuntimeError，由 desktop.py 决定回退方式（默认端口继续启动）。
    """
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception as exc:
        raise RuntimeError(f"tkinter 不可用（{exc}）") from exc

    state = {"port": default_port, "auto": True, "count": seconds, "typing": False}

    root = tk.Tk()
    root.title("客户管理系统 · 启动设置")
    root.resizable(False, False)
    root.configure(bg=BG)
    root.attributes("-topmost", True)

    try:
        yh = tkfont.nametofont("TkDefaultFont").copy()
        yh.configure(family="Microsoft YaHei UI", size=10)
    except Exception:
        yh = None
    root.option_add("*Font", yh)

    W, H = 520, 332
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{max(40, (sh - H) // 2 - 40)}")

    # ---------- 顶部标题区 ----------
    tk.Frame(root, bg=HEADER_BG, height=86).place(x=0, y=0, width=W)
    tk.Label(root, text="客户管理系统", bg=HEADER_BG, fg=TEXT,
             font=("Microsoft YaHei UI", 17, "bold")).place(x=28, y=16)
    tk.Label(root, text="启动配置 · 本地服务，启动后自动打开浏览器", bg=HEADER_BG, fg=MUTED,
             font=("Microsoft YaHei UI", 9)).place(x=28, y=52)

    # ---------- 端口输入区 ----------
    tk.Label(root, text="服务端口", bg=BG, fg=TEXT,
             font=("Microsoft YaHei UI", 10, "bold")).place(x=28, y=112)
    var = tk.StringVar(value=str(default_port))
    entry = tk.Entry(root, textvariable=var, width=14, justify="center",
                     font=("Consolas", 15, "bold"),
                     relief="solid", bd=1, highlightthickness=1,
                     highlightbackground="#C6DCF7", highlightcolor=ACCENT)
    entry.place(x=28, y=138, width=170, height=38)
    entry.focus_set()
    entry.icursor("end")

    tk.Label(root, text="输入端口后按回车键确认，\n或点击下方「启动服务」。", bg=BG, fg=MUTED,
             justify="left", font=("Microsoft YaHei UI", 9)).place(x=214, y=140)

    hint = tk.Label(root, text="", bg=BG, fg=DANGER, font=("Microsoft YaHei UI", 9),
                    wraplength=W - 56, justify="left")
    hint.place(x=28, y=186)

    # ---------- 倒计时 ----------
    cd = tk.Label(root, text="", bg=BG, fg=ACCENT, font=("Microsoft YaHei UI", 9, "bold"))
    cd.place(x=28, y=222)

    # ---------- 按钮区 ----------
    def start(explicit=True):
        raw = var.get().strip()
        if not raw.isdigit() or not (1024 <= int(raw) <= 65535):
            hint.config(text="端口需为 1024 – 65535 之间的整数，请检查后重试。")
            entry.focus_set()
            return
        port = int(raw)
        state["port"], state["auto"] = port, False
        root.destroy()

    def cancel():
        state["port"] = None
        root.destroy()

    btn_start = tk.Button(root, text="启动服务", command=start, width=13, height=1,
                          bg=ACCENT, fg="white", activebackground="#0B5ED7",
                          activeforeground="white", relief="flat", cursor="hand2",
                          font=("Microsoft YaHei UI", 10, "bold"))
    btn_start.place(x=28, y=250)
    tk.Button(root, text="退出", command=cancel, width=8, height=1,
              bg="#EEF3FA", fg=TEXT, activebackground="#DCE7F5", relief="flat",
              cursor="hand2", font=("Microsoft YaHei UI", 10)).place(x=196, y=250)

    # ---------- 底部团队信息（固定展示） ----------
    tk.Frame(root, bg="#E9F1FB", height=1).place(x=28, y=292, width=W - 56)
    tk.Label(root, text=f"{TEAM}　·　{SITE}", bg=BG, fg=MUTED,
             font=("Microsoft YaHei UI", 9)).place(x=28, y=300)

    # ---------- 行为绑定 ----------
    def on_key(_event=None):
        if not state["typing"]:
            state["typing"] = True
            state["auto"] = False
            cd.config(text="检测到输入，已暂停自动启动。按回车键立即启动。", fg=MUTED)
        hint.config(text="")

    def on_return(_event=None):
        start()

    def tick():
        if state["typing"] or not state["auto"]:
            return
        if state["count"] <= 0:
            state["port"], state["auto"] = default_port, True
            root.destroy()
            return
        cd.config(text=f"{state['count']} 秒内未操作，将自动以默认端口 {default_port} 启动…")
        state["count"] -= 1
        root.after(1000, tick)

    entry.bind("<Key>", on_key)
    entry.bind("<Return>", on_return)
    root.bind("<Escape>", lambda _e: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)

    tick()
    root.mainloop()

    if state["port"] is None:
        return None
    return state["port"], state["auto"]
