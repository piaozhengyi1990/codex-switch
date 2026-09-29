#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 模型切换器 — 图形界面。

界面只做壳：所有读写配置的动作都调用 codex_cn.py 的命令函数
（cc.run_op 包装，捕获输出并拿到结构化结果），引擎行为与命令行完全一致。

功能（9 项）：
  1. 状态卡 + 配置自检（13 项）
  2. 切换器：选服务商 -> 选模型 -> 预览 diff -> 确认切换
  3. API Key 管理：Windows 凭据管理器存储、掩码显示、一键清除
  4. 备份历史 + 一键还原
  5. 一键回滚：切回原生内置 / 还原原始快照
  6. 设置：官方附加字段开关、Key 写入方式、打开目录
  7. 操作日志：界面滚动 + %LOCALAPPDATA%\CodexSwitch\logs\switch.log 轮转落盘
  8. 关于 / 版本
  9. 切换后检测 Codex 是否在运行并强制提示重启
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from types import SimpleNamespace

import codex_cn as cc

APP_NAME = "Codex 模型切换器"
APP_VERSION = "1.0.0"

# ---- 配色 ----
BG = "#f4f5f7"
CARD = "#ffffff"
FG = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
OK_C = "#16a34a"
BAD_C = "#dc2626"
BANNER_BG = "#fef3c7"
BANNER_FG = "#92400e"
LINE = "#e5e7eb"

FONT = ("Microsoft YaHei UI", 9)
FONT_S = ("Microsoft YaHei UI", 8)
FONT_H = ("Microsoft YaHei UI", 13, "bold")
FONT_B = ("Microsoft YaHei UI", 9, "bold")


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# Codex 自己的进程名（tasklist 输出转小写后比对）。
# 用精确名单而不是 "名字里含 codex 就算" —— 后者会把本工具自己、
# 用户的 codex-notes.exe 之类一起误判成 Codex 正在运行。
CODEX_PROCESS_NAMES = {
    "codex.exe",
    "codex",
    "codex desktop.exe",
    "codex-cli.exe",
    "codex cli.exe",
}


def codex_running() -> bool:
    """检测 Codex（桌面端或 CLI）是否在运行，排除本程序自身。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception:  # noqa: BLE001
        return False
    mine = set()
    for p in (sys.executable, sys.argv[0] if sys.argv else ""):
        if p:
            try:
                mine.add(Path(p).name.lower())
            except Exception:  # noqa: BLE001
                pass
    for line in (out.stdout or "").splitlines():
        parts = line.split('","')
        name = parts[0].lstrip('"').strip().lower() if parts else ""
        if not name or name in mine:
            continue
        if name in CODEX_PROCESS_NAMES:
            return True
    return False


def open_path(path) -> None:
    p = Path(path)
    if not p.exists():
        messagebox.showinfo(APP_NAME, f"目录不存在：{p}")
        return
    try:
        os.startfile(str(p))  # noqa: S606
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"无法打开目录：{exc}")


def app_log_dir() -> Path:
    """本工具自己的日志目录。

    默认 %LOCALAPPDATA%\\CodexSwitch\\logs：按 Windows 规范放用户应用数据，
    不需要管理员权限；也避免把我们自己的日志混进客户的 ~/.codex 里
    （那个目录归 Codex 管，卸载/重连时可能被一起清掉）。

    环境变量 CODEXSWITCH_LOG_DIR 可覆盖（测试和便携部署用）。
    """
    override = os.environ.get("CODEXSWITCH_LOG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "CodexSwitch" / "logs"
    return Path.home() / ".local" / "share" / "CodexSwitch" / "logs"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1080x780")
        self.minsize(940, 660)
        self.configure(bg=BG)

        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.current_pid: str | None = None      # 表单当前选中
        self.current_model: str | None = None    # 表单当前选中的模型
        self.active_provider: str | None = None  # 配置里实际生效的 provider
        self.active_model: str | None = None
        self.provider_rows: list[dict] = []
        self._pending: list = []
        self._destroyed = False

        self._make_logger()
        self._build_style()
        self._build_header()
        self._build_banner()
        self._build_tabs()
        self._build_log()
        self._build_statusbar()

        self._pump_job = None
        self._boot_job = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump_job = self.after(80, self._pump)
        self._boot_job = self.after(250, self.refresh_all)

    # ------------------------------------------------------------------ 日志
    def _make_logger(self) -> None:
        # 首选系统应用数据目录；那里写不进去（权限/盘符被拔）就退到临时目录，
        # 两条路都失败才放弃日志——界面日志仍然可用，不影响功能。
        temp = os.environ.get("TEMP") or os.environ.get("TMP") or str(
            Path.home() / "AppData" / "Local" / "Temp")
        for log_dir in (app_log_dir(), Path(temp) / "CodexSwitch" / "logs"):
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                self.log_path = log_dir / "switch.log"
                handler = RotatingFileHandler(
                    self.log_path, maxBytes=512 * 1024, backupCount=3,
                    encoding="utf-8")
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s %(message)s"))
                self.logger = logging.getLogger("codex_switch")
                self.logger.setLevel(logging.INFO)
                self.logger.handlers = [handler]
                return
            except OSError:
                self.log_path = None
                self.logger = None

    # ------------------------------------------------------------------ 框架
    def _build_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure(".", font=FONT, background=BG, foreground=FG)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", font=FONT_B, padding=(14, 7))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=CARD, foreground=FG, font=FONT)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED,
                     font=FONT_S)
        st.configure("TButton", font=FONT, padding=(10, 5))
        st.configure("Treeview", font=FONT, rowheight=24, background=CARD,
                     fieldbackground=CARD, foreground=FG)
        st.configure("Treeview.Heading", font=FONT_B, background="#eef0f4")
        st.configure("TCheckbutton", font=FONT, background=CARD)
        st.configure("TLabelframe", background=CARD, foreground=FG, font=FONT_B)
        st.configure("TLabelframe.Label", background=CARD, foreground=FG,
                     font=FONT_B)

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=CARD, padx=16, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text=APP_NAME, font=FONT_H, bg=CARD, fg=FG).pack(
            side="left")
        self.var_status = tk.StringVar(value="正在读取配置 ...")
        tk.Label(hdr, textvariable=self.var_status, font=FONT, bg=CARD,
                 fg=MUTED).pack(side="left", padx=(20, 0))
        tk.Frame(hdr, bg=CARD, width=1).pack(side="left", fill="y",
                                             padx=16, pady=2)
        ttk.Button(hdr, text="刷新状态", command=self.refresh_all).pack(
            side="left")
        ttk.Button(hdr, text="运行自检", command=self.run_doctor).pack(
            side="left", padx=(8, 0))
        tk.Frame(self, bg=CARD, height=1).pack(fill="x")

    def _build_banner(self) -> None:
        self.banner = tk.Label(
            self, text="", font=FONT_B, bg=BANNER_BG, fg=BANNER_FG,
            padx=16, pady=7, anchor="w", justify="left")
        # 默认不 pack，检测到 Codex 在运行时再显示

    def _build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self.tab_switch = ttk.Frame(self.nb, padding=14)
        self.tab_doctor = ttk.Frame(self.nb, padding=14)
        self.tab_backup = ttk.Frame(self.nb, padding=14)
        self.tab_setting = ttk.Frame(self.nb, padding=14)
        self.tab_about = ttk.Frame(self.nb, padding=14)
        self.nb.add(self.tab_switch, text="  切换模型  ")
        self.nb.add(self.tab_doctor, text="  配置自检  ")
        self.nb.add(self.tab_backup, text="  备份与还原  ")
        self.nb.add(self.tab_setting, text="  设置  ")
        self.nb.add(self.tab_about, text="  关于  ")

        self._build_switch_tab()
        self._build_doctor_tab()
        self._build_backup_tab()
        self._build_setting_tab()
        self._build_about_tab()

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text=" 操作日志 ", padding=8)
        box.pack(fill="x", padx=12, pady=(0, 8))
        self.log_text = ScrolledText(box, height=8, wrap="none",
                                     font=("Consolas", 9), state="disabled",
                                     bg="#111827", fg="#d1d5db",
                                     insertbackground="#d1d5db",
                                     relief="flat", padx=8, pady=6)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("ok", foreground="#4ade80")
        self.log_text.tag_configure("bad", foreground="#f87171")
        self.log_text.tag_configure("head", foreground="#93c5fd")

    def _build_statusbar(self) -> None:
        self.var_bar = tk.StringVar(value="就绪")
        tk.Label(self, textvariable=self.var_bar, font=FONT_S, bg=BG,
                 fg=MUTED, anchor="w", padx=14, pady=4).pack(fill="x")

    # -------------------------------------------------------------- 切换标签页
    def _build_switch_tab(self) -> None:
        left = ttk.LabelFrame(self.tab_switch, text=" 服务商 ", padding=10)
        left.pack(side="left", fill="both", expand=False)
        cols = ("id", "name", "key")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 height=12, selectmode="browse")
        self.tree.heading("id", text="id")
        self.tree.heading("name", text="服务商")
        self.tree.heading("key", text="API Key")
        self.tree.column("id", width=96, anchor="w", stretch=False)
        self.tree.column("name", width=190, anchor="w")
        self.tree.column("key", width=130, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_provider)

        right = ttk.LabelFrame(self.tab_switch, text=" 模型与密钥 ", padding=14)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        r0 = ttk.Frame(right, style="Card.TFrame")
        r0.pack(fill="x")
        self.var_pid = tk.StringVar(value="未选择服务商")
        tk.Label(r0, text="当前选择：", bg=CARD, fg=MUTED, font=FONT_S).pack(
            side="left")
        tk.Label(r0, textvariable=self.var_pid, bg=CARD, font=FONT_B,
                 fg=ACCENT).pack(side="left", padx=(4, 0))

        ttk.Label(right, text="模型").pack(anchor="w", pady=(14, 2))
        self.var_model = tk.StringVar()
        self.cmb_model = ttk.Combobox(right, textvariable=self.var_model,
                                      font=FONT, state="normal")
        self.cmb_model.pack(fill="x")

        ttk.Label(right, text="API Key（保存到 Windows 凭据管理器）").pack(
            anchor="w", pady=(14, 2))
        r1 = ttk.Frame(right, style="Card.TFrame")
        r1.pack(fill="x")
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(r1, textvariable=self.var_key, font=FONT,
                                 show="*")
        self.ent_key.pack(side="left", fill="x", expand=True)
        self.btn_eye = ttk.Button(r1, text="显示", width=6,
                                  command=self._toggle_eye)
        self.btn_eye.pack(side="left", padx=(6, 0))
        self.var_key_hint = tk.StringVar(value="Key 状态：未知")
        tk.Label(right, textvariable=self.var_key_hint, bg=CARD,
                 font=FONT_S, fg=MUTED).pack(anchor="w", pady=(4, 0))

        r2 = ttk.Frame(right, style="Card.TFrame")
        r2.pack(fill="x", pady=(12, 0))
        ttk.Button(r2, text="保存 Key", command=self.save_key).pack(
            side="left")
        ttk.Button(r2, text="清除 Key", command=self.clear_key).pack(
            side="left", padx=(8, 0))
        ttk.Button(r2, text="测试 Key", command=self.test_key).pack(
            side="left", padx=(8, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=16)

        r3 = ttk.Frame(right, style="Card.TFrame")
        r3.pack(fill="x")
        ttk.Button(r3, text="预览更改（不写入）",
                   command=self.preview).pack(side="left")
        ttk.Button(r3, text="应用切换", command=self.do_switch).pack(
            side="left", padx=(10, 0))

        ttk.Label(right, text="快捷回滚", background=CARD, foreground=MUTED,
                  font=FONT_S).pack(anchor="w", pady=(22, 4))
        r4 = ttk.Frame(right, style="Card.TFrame")
        r4.pack(fill="x")
        ttk.Button(r4, text="切回 Codex 原生内置模型",
                   command=self.switch_native).pack(side="left")
        ttk.Button(r4, text="还原原始快照",
                   command=self.restore_original).pack(side="left",
                                                       padx=(10, 0))

        ttk.Label(right, text="提示：切换后必须完全退出并重启 Codex 才生效。",
                  background=CARD, foreground=BANNER_FG, font=FONT_S
                  ).pack(anchor="w", pady=(24, 0))

    # -------------------------------------------------------------- 自检标签页
    def _build_doctor_tab(self) -> None:
        top = ttk.Frame(self.tab_doctor, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Button(top, text="运行自检", command=self.run_doctor).pack(
            side="left")
        self.var_doctor = tk.StringVar(value="尚未自检")
        tk.Label(top, textvariable=self.var_doctor, bg=CARD, font=FONT_B,
                 fg=MUTED).pack(side="left", padx=(16, 0))

        self.tree_doc = ttk.Treeview(
            self.tab_doctor, columns=("res", "label", "detail"),
            show="headings", height=14)
        self.tree_doc.heading("res", text="结果")
        self.tree_doc.heading("label", text="检查项")
        self.tree_doc.heading("detail", text="详情")
        self.tree_doc.column("res", width=70, anchor="center", stretch=False)
        self.tree_doc.column("label", width=380, anchor="w")
        self.tree_doc.column("detail", width=380, anchor="w")
        self.tree_doc.tag_configure("ok", foreground=OK_C)
        self.tree_doc.tag_configure("bad", foreground=BAD_C)
        self.tree_doc.tag_configure("info", foreground=MUTED)
        self.tree_doc.pack(fill="both", expand=True, pady=(12, 0))

    # -------------------------------------------------------------- 备份标签页
    def _build_backup_tab(self) -> None:
        top = ttk.Frame(self.tab_backup, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Button(top, text="刷新列表",
                   command=lambda: self.run_task("刷新备份", self._op_list_backups,
                                                 on_done=self._on_list_backups)
                   ).pack(side="left")
        ttk.Button(top, text="还原所选备份",
                   command=self.restore_backup).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="还原原始快照",
                   command=self.restore_original).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="切回原生内置",
                   command=self.switch_native).pack(side="left", padx=(8, 0))

        self.var_backup_info = tk.StringVar(
            value="「原始快照」= 本工具第一次运行那一刻的文件状态，"
                  "不等于 Codex 出厂配置；若此前用过第三方接入脚本，"
                  "还原回来的也是那份状态（要回出厂状态请用「切回原生内置」）。")
        tk.Label(self.tab_backup, textvariable=self.var_backup_info,
                 background=BG, foreground=MUTED, font=FONT_S,
                 anchor="w", justify="left").pack(fill="x", pady=(10, 4))

        self.lst_backup = tk.Listbox(self.tab_backup, font=FONT, height=12,
                                     activestyle="dotbox", exportselection=False)
        self.lst_backup.pack(fill="both", expand=True)
        self._backup_paths: list[Path] = []

    # -------------------------------------------------------------- 设置标签页
    def _build_setting_tab(self) -> None:
        box = ttk.LabelFrame(self.tab_setting, text=" 写入行为 ", padding=14)
        box.pack(fill="x")

        self.var_official = tk.BooleanVar(value=cc.OFFICIAL_EXTRAS)
        ttk.Checkbutton(
            box,
            text="额外写入 DeepSeek 官方的非模型字段"
                 "（preferred_auth_method / web_search / 推理档位等）"
                 "—— 会改变 Codex 原生行为，默认关闭",
            variable=self.var_official,
            command=self._toggle_official).pack(anchor="w")

        self.var_inline = tk.BooleanVar(value=cc.AUTH_MODE == "inline")
        ttk.Checkbutton(
            box,
            text="把 API Key 明文写进 config.toml"
                 "（桌面端读不到环境变量时才需要）",
            variable=self.var_inline,
            command=self._toggle_inline).pack(anchor="w", pady=(8, 0))
        self._inline_warned = False
        self.var_inline_hint = tk.StringVar()
        tk.Label(box, textvariable=self.var_inline_hint, background=CARD,
                 foreground=MUTED, font=FONT_S, anchor="w",
                 justify="left", wraplength=620).pack(anchor="w", pady=(4, 0))
        self._refresh_inline_hint()

        info = ttk.LabelFrame(self.tab_setting, text=" 路径与状态 ", padding=14)
        info.pack(fill="x", pady=(14, 0))
        rows = [
            ("配置目录", str(cc.CODEX_DIR)),
            ("config.toml", str(cc.CONFIG_PATH)),
            ("模型目录", str(cc.CATALOG_PATH)),
            ("备份目录", str(cc.BACKUP_DIR)),
            ("日志文件", str(self.log_path or "（不可用）")),
            ("凭据管理器",
             "可用" if cc.cred_available() else "不可用（仅用环境变量）"),
            ("原始快照",
             "已封存" if (cc.ORIGINAL_DIR / "manifest.json").exists()
             else "尚未封存"),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(info, text=k, background=CARD, foreground=MUTED,
                     font=FONT_S, width=12, anchor="w").grid(
                row=i, column=0, sticky="w", pady=2)
            tk.Label(info, text=v, background=CARD, foreground=FG, font=FONT_S,
                     anchor="w").grid(row=i, column=1, sticky="w", pady=2)
        info.columnconfigure(1, weight=1)

        btns = ttk.Frame(self.tab_setting, style="Card.TFrame")
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="打开配置目录",
                   command=lambda: open_path(cc.CODEX_DIR)).pack(side="left")
        ttk.Button(btns, text="打开备份目录",
                   command=lambda: open_path(cc.BACKUP_DIR)).pack(
            side="left", padx=(8, 0))
        ttk.Button(btns, text="打开日志目录",
                   command=lambda: open_path(
                       self.log_path.parent if self.log_path
                       else app_log_dir())).pack(
            side="left", padx=(8, 0))

    # -------------------------------------------------------------- 关于标签页
    def _build_about_tab(self) -> None:
        txt = ScrolledText(self.tab_about, font=FONT, wrap="word",
                           relief="flat", bg=CARD, fg=FG, padx=14, pady=14)
        txt.pack(fill="both", expand=True)
        lines = [
            (f"{APP_NAME}  v{APP_VERSION}", "h"),
            ("", ""),
            ("作用：把 Codex 切换到国内大模型，不动 Codex 的其他原生功能。",
             ""),
            ("", ""),
            ("【必须知道的三件事】", "h"),
            ("  1. 切换后必须完全退出 Codex（桌面端/终端）再重新打开，配置才生效。", ""),
            ("  2. 只接管 model / model_provider / model_catalog_json / "
             "[model_providers.*] / 推理档位，其余配置原样保留。", ""),
            ("  3. 首次运行会封存原始快照，可随时一键还原。", ""),
            ("", ""),
            ("【支持的服务商】", "h"),
        ]
        for pid, p in cc.PROVIDERS.items():
            models = "、".join(m["slug"] for m in p["models"])
            lines.append((f"  {pid}  {p['name']}", ""))
            lines.append((f"      base_url: {p['base_url']}", "s"))
            lines.append((f"      Key 环境变量: {p['env_key']}", "s"))
            lines.append((f"      模型: {models}", "s"))
            lines.append((f"      文档: {p['doc']}", "s"))
            lines.append(("", ""))
        lines.append(("【Codex 无法直连的服务商】", "h"))
        for name, why in cc.UNSUPPORTED.items():
            lines.append((f"  - {name}", ""))
            lines.append((f"      {why}", "s"))
        lines.append(("", ""))
        lines.append(("【API Key 存放说明】", "h"))
        lines.append((
            "  Key 存 Windows 凭据管理器（系统加密、界面掩码、可一键清除），"
            "同时写入用户环境变量供 Codex 读取。", ""))
        lines.append((
            "  因为 Codex 只认环境变量，这一步无法避免；凭据管理器的价值是"
            "不在界面回显、不落明文文件、集中可控。", "s"))

        for text, kind in lines:
            tag = {"h": "h", "s": "s"}.get(kind, "")
            txt.insert("end", text + "\n", tag)
        txt.tag_configure("h", font=FONT_B, foreground=ACCENT,
                          spacing1=6, spacing3=6)
        txt.tag_configure("s", foreground=MUTED, font=FONT_S)
        txt.configure(state="disabled")

    # ------------------------------------------------------------------ 事件
    def on_close(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        # 先取消挂起的定时器，否则销毁后 Tk 会报 invalid command name
        for job in (self._pump_job, self._boot_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass
        try:
            if self.logger:
                for h in list(self.logger.handlers):
                    h.close()
                    self.logger.removeHandler(h)
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def _toggle_eye(self) -> None:
        self.ent_key.configure(show="" if self.ent_key.cget("show") else "*")
        self.btn_eye.configure(
            text="隐藏" if self.ent_key.cget("show") == "" else "显示")

    def _toggle_official(self) -> None:
        cc.OFFICIAL_EXTRAS = bool(self.var_official.get())
        self._append_log(
            f"[设置] OFFICIAL_EXTRAS = {cc.OFFICIAL_EXTRAS}"
            f"{'（会改变 Codex 原生行为）' if cc.OFFICIAL_EXTRAS else ''}")

    def _refresh_inline_hint(self) -> None:
        inline = self.var_inline.get()
        self.var_inline_hint.set(
            "风险：Key 将以明文写进 config.toml，并被历史备份与原始快照各复制一份"
            " —— 能读该目录的人都能看到。"
            if inline else
            "安全：Key 只存环境变量 / 凭据管理器，config.toml 里只有变量名。")

    def _toggle_inline(self) -> None:
        on = self.var_inline.get()
        cc.AUTH_MODE = "inline" if on else "env"
        self._refresh_inline_hint()
        if on and not self._inline_warned:
            self._inline_warned = True
            messagebox.showwarning(
                APP_NAME,
                "已开启「明文写入 config.toml」。\n\n"
                "· API Key 会以明文保存在 config.toml；\n"
                "· 该文件还会被历史备份（最多 10 份）与原始快照各复制一份；\n"
                "· 凡是能读 ~/.codex 目录的人都能看到这个 Key。\n\n"
                "只有桌面端读不到环境变量时才需要它。")
        self._append_log(f"[设置] AUTH_MODE = {cc.AUTH_MODE}")

    def _on_select_provider(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        pid = sel[0]
        self.current_pid = pid
        self.var_pid.set(f"{pid}  ({cc.PROVIDERS[pid]['name']})")
        slugs = [m["slug"] for m in cc.PROVIDERS[pid]["models"]]
        if pid == "volcengine":
            slugs = list(slugs)
        self.cmb_model.configure(values=slugs)
        self.var_model.set(slugs[0] if slugs else "")
        self.var_key.set("")
        self.ent_key.configure(show="*")
        self.btn_eye.configure(text="显示")
        row = next((r for r in self.provider_rows if r["id"] == pid), None)
        if row:
            self.var_key_hint.set(f"已存 Key：{row['masked']}   "
                                  f"环境变量：{row['env_key']}")
        if pid == "volcengine":
            self.var_model.set("")
            self.cmb_model.set("")
            self._append_log("[提示] volcengine 需要填真实推理接入点 ID"
                             "（ep-开头），不是模型名。")

    # ------------------------------------------------------------------ 任务
    def run_task(self, tag, fn, *args, on_done=None) -> None:
        if self.busy:
            self._pending.append((tag, fn, args, on_done))
            self._set_bar(f"排队中：{tag}（前一个操作完成后自动执行）")
            return
        self.busy = True
        self._set_bar(f"正在执行：{tag} ...")

        def work() -> None:
            res = cc.run_op(fn, *args)
            for ln in res.lines:
                self.q.put(("log", ln))
            self.q.put(("done", tag, res, on_done))

        threading.Thread(target=work, daemon=True).start()

    def _pump(self) -> None:
        if self._destroyed:
            return
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "done":
                    self._on_done(item[1], item[2], item[3])
        except queue.Empty:
            pass
        if not self._destroyed:
            self._pump_job = self.after(80, self._pump)

    def _on_done(self, tag, res, on_done) -> None:
        self.busy = False
        if res.ok:
            self._set_bar(f"{tag}：完成")
        else:
            err = next((ln for ln in reversed(res.lines)
                        if "错误" in ln or "FAIL" in ln or "异常" in ln),
                       res.lines[-1] if res.lines else "未知错误")
            self._set_bar(f"{tag}：失败 — {err.strip()}")
        if on_done:
            try:
                on_done(res)
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"[界面异常] {type(exc).__name__}: {exc}")
        self.refresh_status_text()
        if res.ok and any(k in tag for k in ("切换", "还原")):
            self._maybe_remind_restart()
        self._drain_pending()

    def _drain_pending(self) -> None:
        if self.busy or not self._pending:
            return
        tag, fn, args, on_done = self._pending.pop(0)
        self.run_task(tag, fn, *args, on_done=on_done)

    def _maybe_remind_restart(self) -> None:
        running = codex_running()
        text = ("检测到 Codex 正在运行 —— 请完全退出后重新打开，"
                "新的模型配置才会生效。") if running else \
            ("配置已更新。下次打开 Codex 时生效。")
        self.banner.configure(text="  " + text)
        self.banner.pack(fill="x", padx=12, pady=(0, 6))
        self._append_log(f"[重启提醒] {text}")

    # ------------------------------------------------------------------ 动作
    def refresh_all(self) -> None:
        self.run_task("刷新状态", cc.cmd_list, None, on_done=self._on_list)

    def refresh_status_text(self) -> None:
        pid = self.active_provider or "Codex 内置默认"
        model = self.active_model or "Codex 默认模型"
        self.var_status.set(f"当前生效：{pid} / {model}    "
                            f"配置目录：{cc.CODEX_DIR}")

    def _on_list(self, res) -> None:
        rows = res.data.get("rows") or []
        self.provider_rows = rows
        kids = self.tree.get_children()
        if kids:
            self.tree.delete(*kids)
        for r in rows:
            self.tree.insert("", "end", iid=r["id"],
                             values=(r["id"], r["name"], r["masked"]))
        cur = res.data.get("current") or {}
        self.active_provider = cur.get("provider")
        self.active_model = cur.get("model")
        if cur.get("provider") in cc.PROVIDERS:
            self.tree.selection_set(cur["provider"])
            self.tree.see(cur["provider"])
            self._on_select_provider()
            if cur.get("model"):
                self.var_model.set(str(cur["model"]))
        elif not cur.get("provider"):
            self.var_pid.set("（Codex 内置默认 provider，尚未选择）")
        if not self.current_pid and rows:
            self.tree.selection_set(rows[0]["id"])
            self._on_select_provider()

    # --- Key -------------------------------------------------------------
    def _ask_key(self, pid: str) -> str | None:
        p = cc.PROVIDERS[pid]
        win = tk.Toplevel(self)
        win.title(f"输入 {pid} 的 API Key")
        win.geometry("560x190")
        win.configure(bg=CARD)
        win.transient(self)
        win.grab_set()
        tk.Label(win, text=f"还没有 {p['name']} 的 API Key",
                 font=FONT_B, bg=CARD, fg=FG).pack(anchor="w", padx=18,
                                                    pady=(16, 4))
        tk.Label(win, text=f"获取地址：{p['doc']}",
                 font=FONT_S, bg=CARD, fg=MUTED, wraplength=520,
                 justify="left").pack(anchor="w", padx=18)
        tk.Label(win, text=f"环境变量名：{p['env_key']}",
                 font=FONT_S, bg=CARD, fg=MUTED).pack(anchor="w", padx=18,
                                                       pady=(6, 0))
        var = tk.StringVar()
        ent = ttk.Entry(win, textvariable=var, font=FONT, show="*")
        ent.pack(fill="x", padx=18, pady=(10, 0))
        ent.focus_set()
        out: dict = {"key": None}

        def ok() -> None:
            val = var.get().strip()
            if not val:
                messagebox.showwarning(APP_NAME, "Key 不能为空", parent=win)
                return
            out["key"] = val
            win.destroy()

        def cancel() -> None:
            win.destroy()

        r = ttk.Frame(win, style="Card.TFrame")
        r.pack(fill="x", padx=18, pady=14)
        ttk.Button(r, text="保存并继续", command=ok).pack(side="right")
        ttk.Button(r, text="取消", command=cancel).pack(side="right",
                                                       padx=(0, 8))
        win.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: cancel())
        self.wait_window(win)
        return out["key"]

    def _ensure_key(self, pid: str) -> bool:
        if cc.resolve_key(pid):
            return True
        val = self._ask_key(pid)
        if not val:
            return False
        ns = SimpleNamespace(provider=pid, key=val, dry_run=False)
        res = cc.run_op(cc.cmd_setkey, ns)  # cmd_setkey 负责写凭据管理器+环境变量
        for ln in res.lines:
            self._append_log(ln)
        if not res.ok:
            messagebox.showerror(APP_NAME, "保存 Key 失败，原因见下方日志。")
        self.refresh_all()
        return res.ok

    def save_key(self) -> None:
        pid = self.current_pid
        if not pid:
            messagebox.showinfo(APP_NAME, "请先在左侧选择服务商")
            return
        val = self.var_key.get().strip()
        if not val:
            messagebox.showinfo(APP_NAME, "请输入 API Key")
            return
        ns = SimpleNamespace(provider=pid, key=val, dry_run=False)

        def done(r) -> None:
            if not r.ok:
                messagebox.showerror(APP_NAME, "保存 Key 失败，原因见下方日志。")
            self.refresh_all()

        self.run_task(f"保存 {pid} 的 Key", cc.cmd_setkey, ns, on_done=done)

    def clear_key(self) -> None:
        pid = self.current_pid
        if not pid:
            messagebox.showinfo(APP_NAME, "请先在左侧选择服务商")
            return
        if not messagebox.askyesno(
                APP_NAME, f"确定清除 {pid} 的 API Key？\n"
                          f"（凭据管理器与环境变量都会删除）"):
            return

        def op(p: str) -> int:
            env = cc.PROVIDERS[p]["env_key"]
            deleted = cc.cred_delete(env)
            if not cc.write_user_env(env, ""):
                print(f"[错误] 环境变量 {env} 清除失败。")
                return 1
            print(f"[OK] 已清除 {p} 的 Key"
                  f"（凭据管理器：{'已删除' if deleted else '无记录'}，"
                  f"环境变量 {env} 已清空）")
            return 0

        def done(r) -> None:
            if r.ok:
                self.var_key.set("")
                self.var_key_hint.set("已存 Key：未设置")
            self.refresh_all()

        self.run_task(f"清除 {pid} 的 Key", op, pid, on_done=done)

    def test_key(self) -> None:
        """发一个只读 GET /models 探测端点与 Key（不打印 Key）。"""
        pid = self.current_pid
        if not pid:
            messagebox.showinfo(APP_NAME, "请先在左侧选择服务商")
            return
        if not cc.resolve_key(pid):
            messagebox.showinfo(APP_NAME, f"{pid} 还没有 API Key，请先保存。")
            return
        ns = SimpleNamespace(provider=pid, timeout=8)
        self.run_task(f"测试 {pid} 的 Key", cc.cmd_testkey, ns,
                      on_done=self._on_testkey)

    def _on_testkey(self, res) -> None:
        if self._destroyed:
            return
        data = res.data
        if data.get("network_error"):
            messagebox.showwarning(
                APP_NAME,
                "连不上端点（网络 / 代理 / 防火墙）。\n"
                "这不能说明 Key 有问题，请联网后重试。")
        elif data.get("ok") is False:
            messagebox.showerror(
                APP_NAME,
                f"Key 被端点拒绝（HTTP {data.get('status')}）。\n"
                "Key 无效、已过期或权限不足，请重新获取后保存。")
        elif data.get("ok"):
            messagebox.showinfo(
                APP_NAME,
                f"Key 可用：HTTP {data.get('status')}，"
                f"{data.get('ms')}ms。")
        else:
            messagebox.showinfo(
                APP_NAME,
                f"端点返回 HTTP {data.get('status')}，无法据此判断 Key 是否有效。")

    # --- 切换 -------------------------------------------------------------
    def preview(self) -> None:
        pid = self.current_pid
        if not pid:
            messagebox.showinfo(APP_NAME, "请先在左侧选择服务商")
            return
        model = self.var_model.get().strip() or None
        if not self._ensure_key(pid):
            return
        self.run_task("预览更改", cc.apply, pid, model,
                      cc.AUTH_MODE == "inline", True, False,
                      on_done=self._show_preview)

    def _show_preview(self, res) -> None:
        if not res.ok:
            return
        preview = res.data.get("preview")
        if not preview:
            return
        diff = res.data.get("diff") or ""
        risk = res.data.get("risk") or ""
        win = tk.Toplevel(self)
        win.title("预览：将写入的 config.toml（尚未写入，Key 已脱敏）")
        win.geometry("900x600")
        win.transient(self)
        tk.Label(win, text="以下内容确认后才会写入 config.toml",
                 font=FONT_B, anchor="w").pack(fill="x", padx=12, pady=(10, 6))
        if risk:
            tk.Label(win, text=risk, font=FONT_S, fg=BAD_C, bg=BANNER_BG,
                     anchor="w", justify="left", wraplength=860,
                     padx=6, pady=4).pack(fill="x", padx=12)
        body = ScrolledText(win, font=("Consolas", 9), wrap="none",
                            relief="flat", bg="#111827", fg="#d1d5db",
                            padx=10, pady=8)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        body.tag_configure("add", foreground="#4ade80")
        body.tag_configure("del", foreground="#f87171")
        body.tag_configure("hunk", foreground="#93c5fd")
        body.tag_configure("head", foreground="#9ca3af")
        if diff:
            body.insert("end", "[与当前配置的差异]\n", "head")
            for ln in diff.splitlines():
                if ln.startswith(("+++", "---")):
                    tag = "head"
                elif ln.startswith("+"):
                    tag = "add"
                elif ln.startswith("-"):
                    tag = "del"
                elif ln.startswith("@@"):
                    tag = "hunk"
                else:
                    tag = ""
                body.insert("end", ln + "\n", tag)
            body.insert("end", "\n[完整 config.toml]\n", "head")
        body.insert("end", preview)
        body.configure(state="disabled")

        def go() -> None:
            win.destroy()
            self.do_switch()

        r = ttk.Frame(win)
        r.pack(fill="x", padx=12, pady=12)
        ttk.Button(r, text="确认并应用", command=go).pack(side="right")
        ttk.Button(r, text="返回修改",
                   command=win.destroy).pack(side="right", padx=(0, 8))

    def do_switch(self) -> None:
        pid = self.current_pid
        if not pid:
            messagebox.showinfo(APP_NAME, "请先在左侧选择服务商")
            return
        model = self.var_model.get().strip() or None
        if not self._ensure_key(pid):
            return
        inline = cc.AUTH_MODE == "inline"
        msg = (f"确认切换到 {pid}"
               f"{(' / ' + model) if model else ''}？\n\n"
               f"切换前会自动备份当前配置。\n"
               f"切换后必须完全退出并重启 Codex。")
        if inline:
            msg += ("\n\n【风险】inline 模式：Key 将以明文写进 config.toml，"
                    "并被备份与快照各复制一份。")
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self.run_task("切换模型", cc.apply, pid, model,
                      cc.AUTH_MODE == "inline", False, False,
                      on_done=self._after_switch)

    def _after_switch(self, res) -> None:
        if not res.ok:
            return
        d = res.data
        if d.get("provider") and d.get("provider") != "native":
            self.active_provider = d.get("provider")
        else:
            self.active_provider = None
        self.active_model = d.get("model")
        if self.current_pid and self.current_pid == self.active_provider:
            self.current_model = self.active_model
        self.refresh_status_text()
        self.run_task("刷新备份", self._op_list_backups,
                      on_done=self._on_list_backups)

    def switch_native(self) -> None:
        if not messagebox.askyesno(
                APP_NAME,
                "切回 Codex 原生内置 provider 与默认模型？\n\n"
                "只会撤销本工具接管的几个字段，其他配置不动。"):
            return
        self.run_task("切换模型", cc.apply, "native", None, False, False,
                      False, on_done=self._after_switch)

    # --- 还原 -------------------------------------------------------------
    def restore_original(self) -> None:
        if not (cc.ORIGINAL_DIR / "manifest.json").exists():
            messagebox.showinfo(APP_NAME,
                                "尚未封存原始快照，无法还原。\n"
                                "快照在第一次切换时自动生成。")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "还原到首次运行前的原始状态？\n\n"
                "· 会还原 config.toml 与 models.json（当前配置先自动备份）；\n"
                "· 快照 = 本工具第一次运行那一刻的文件状态，不等于 Codex 出厂配置；\n"
                "  若此前用过第三方接入脚本，还原回来的也是那份状态\n"
                "  （要回到出厂状态请用「切回原生内置」）。"):
            return
        self.run_task("还原原始快照", cc.cmd_original, None,
                      on_done=lambda r: self.refresh_all())

    def _op_list_backups(self) -> int:
        paths = [p for p in sorted(cc.BACKUP_DIR.glob("*/config.toml"),
                                   reverse=True)
                 if p.parent != cc.ORIGINAL_DIR] if cc.BACKUP_DIR.exists() \
            else []
        self._backup_paths = paths
        if not paths:
            print("没有备份。")
            return 0
        manifest = cc.ORIGINAL_DIR / "manifest.json"
        snap = ""
        if manifest.exists():
            import json
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
                snap = f"原始快照封存于 {data.get('saved_at', '?')}"
            except Exception:  # noqa: BLE001
                snap = "原始快照已封存"
        print(f"共 {len(paths)} 个备份（最多保留 10 个）。{snap}")
        for i, p in enumerate(paths):
            has_cat = (p.parent / "models.json").exists()
            print(f"  [{i}] {p.parent.name}"
                  f"{' + models.json' if has_cat else ''}")
        return 0

    def _on_list_backups(self, res) -> None:
        self.lst_backup.delete(0, "end")
        for p in self._backup_paths:
            tag = " + models.json" if (p.parent / "models.json").exists() else ""
            self.lst_backup.insert("end", f"{p.parent.name}{tag}")
        info = next((ln for ln in res.lines if ln.startswith("共 ") or
                     ln.startswith("没有备份")), "")
        manifest = cc.ORIGINAL_DIR / "manifest.json"
        extra = ""
        if manifest.exists():
            import json
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
                extra = f"\n原始快照封存于 {data.get('saved_at', '?')}，" \
                        f"config={'有' if data.get('config_existed') else '无'}，" \
                        f"models={'有' if data.get('models_existed') else '无'}"
            except Exception:  # noqa: BLE001
                pass
        self.var_backup_info.set(
            info + extra + "\n说明：原始快照 = 本工具第一次运行那一刻的文件状态，"
            "不等于 Codex 出厂配置；若此前用过第三方接入脚本，还原回来的也是那份"
            "状态（要回出厂状态请用「切回原生内置」）。")

    def restore_backup(self) -> None:
        sel = self.lst_backup.curselection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请先在列表中选择一个备份")
            return
        idx = int(sel[0])
        name = self._backup_paths[idx].parent.name
        if not messagebox.askyesno(
                APP_NAME, f"还原备份 {name}？\n当前配置会先自动备份。"):
            return
        ns = SimpleNamespace(index=idx)
        self.run_task("还原备份", cc.cmd_restore, ns,
                      on_done=lambda r: self.refresh_all())

    # --- 自检 -------------------------------------------------------------
    def run_doctor(self) -> None:
        self.run_task("配置自检", cc.cmd_doctor, None, on_done=self._on_doctor)

    def _on_doctor(self, res) -> None:
        checks = res.data.get("checks") or []
        kids = self.tree_doc.get_children()
        if kids:
            self.tree_doc.delete(*kids)
        for c in checks:
            if c.get("skip"):
                # 尚未接入：灰字中性提示，不算失败也不冒充"通过"
                val, tag = "尚未接入", "info"
            else:
                val = "通过" if c["ok"] else "失败"
                tag = "ok" if c["ok"] else "bad"
            self.tree_doc.insert(
                "", "end", values=(val, c["label"], c["detail"]), tags=(tag,))
        passed = res.data.get("passed", 0)
        total = res.data.get("total", 0)
        neutral = res.data.get("neutral", 0)
        text = f"{passed}/{total} 项通过"
        if not res.ok:
            text += "   —— 存在失败项，请按详情修复"
        elif neutral:
            text += f"   —— 尚未接入（{neutral} 项待切换后自动生成）"
        self.var_doctor.set(text)

    # ------------------------------------------------------------------ 杂项
    def _set_bar(self, text: str) -> None:
        self.var_bar.set(text)

    def _append_log(self, line: str) -> None:
        if self._destroyed:
            return
        # 界面与磁盘日志是两条独立的输出路径，这里统一脱敏，
        # 保证任何来源的行（含 dry-run 的整份配置）都不会明文落盘。
        line = cc.redact(line)
        tag = ""
        s = line.strip()
        if s.startswith("[OK") or "[OK]" in s:
            tag = "ok"
        elif s.startswith("[错误") or s.startswith("[FAIL") or "FAIL" in s:
            tag = "bad"
        elif s.startswith("["):
            tag = "head"
        try:
            self.log_text.configure(state="normal")
            if self.log_text.index("end-1c") != "1.0":
                self.log_text.insert("end", "\n")
            self.log_text.insert("end", line if line else "", tag)
            self.log_text.see("end")
            # 防止日志无限增长
            if int(self.log_text.index("end-1c").split(".")[0]) > 800:
                self.log_text.delete("1.0", "400.0")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        if self.logger and line.strip():
            try:
                self.logger.info(line.strip())
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
