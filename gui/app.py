#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""bilimusic —— B站歌下载工具箱 图形界面。

页面结构（4 个页签）
--------------------
① 目标   配置目标、账号、黑名单白名单、判定阈值
② 流程   五个步骤按钮：抓取 → 判定 → 去重 → 下载 → 校验
③ 曲库   查看去重后的基底清单（每首歌一条），可勾选下载
④ 日志   运行日志 / 环境自检 / 文档入口

日志：主窗口底部可折叠面板（折叠时几乎不占空间）。
窗口：启动即最大化（不是全屏）。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "B站歌下载工具箱"
APP_SUBTITLE = "bilimusic"

GUI_DIR = Path(__file__).resolve().parent
APP_ROOT = GUI_DIR.parent
sys.path.insert(0, str(APP_ROOT))

try:
    import deps as deps_mod
except ImportError:
    deps_mod = None
try:
    import theme
except ImportError:
    theme = None

from core import config as cfg_mod          # noqa: E402
from core import dedup as dedup_mod        # noqa: E402
from core import downloader as dl_mod      # noqa: E402
from core import naming                     # noqa: E402
from core import pipeline as pipe_mod      # noqa: E402
from core import rules                      # noqa: E402

IS_WINDOWS = os.name == "nt"

# 音质档位
AUDIO_CHOICES = [
    ("highest", "最高音质（自动挑最好的）"),
    ("hires", "Hi-Res 无损（大会员）"),
    ("dolby", "杜比全景声"),
    ("192k", "192K"),
    ("132k", "132K"),
    ("64k", "64K"),
]

FORMAT_CHOICES = [
    ("m4a", "m4a（无损封装，推荐）"),
    ("mp3", "mp3（重编码，通用性最好）"),
]


class ScrollableFrame(ttk.Frame):
    """可垂直滚动的容器：内容超出窗口时出现滚动条（鼠标滚轮可用）。"""

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                background=theme.BG if theme else "#f4f5f7")
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_config)
        self.canvas.bind("<Configure>", self._on_canvas_config)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_config(self, _e) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_config(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)
        self._toggle_scrollbar()

    def _toggle_scrollbar(self) -> None:
        need = self.body.winfo_reqheight() > self.canvas.winfo_height()
        if need:
            self.vsb.grid()
        else:
            self.vsb.grid_remove()

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, e) -> None:
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(-e.delta / 120), "units")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} · {APP_SUBTITLE}")
        self.minsize(1060, 700)
        self._set_window_icon()

        self.log_queue: queue.Queue = queue.Queue()
        self.running = False
        self.cfg = cfg_mod.load()
        self.manifest: list = []
        self._checked: set[int] = set()

        if theme:
            theme.apply(self)
        self._maximize()
        self._build_ui()
        self._poll_log()
        self.after(200, self._load_state)
        self._log("info", f"程序目录：{APP_ROOT}")

    # ================= 基础 =================
    def _set_window_icon(self) -> None:
        ico = GUI_DIR / "bili.ico"
        try:
            if ico.is_file():
                self.iconbitmap(default=str(ico))
        except tk.TclError:
            pass

    def _maximize(self) -> None:
        try:
            if IS_WINDOWS:
                self.state("zoomed")
            else:
                self.attributes("-zoomed", True)
        except tk.TclError:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{int(sw*0.92)}x{int(sh*0.88)}+{int(sw*0.04)}+{int(sw*0.05)}")

    # ================= UI =================
    def _build_ui(self) -> None:
        self._build_topbar()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 0))

        self.nb = ttk.Notebook(body)
        self.nb.pack(fill="both", expand=True)

        self.tab_target = ttk.Frame(self.nb)
        self.tab_flow = ttk.Frame(self.nb)
        self.tab_lib = ttk.Frame(self.nb)
        self.nb.add(self.tab_target, text="配置")
        self.nb.add(self.tab_flow, text="流程")
        self.nb.add(self.tab_lib, text="曲库")

        self.tab_target_body = ttk.Frame(self.tab_target, padding=(14, 12))
        self.tab_target_body.pack(fill="both", expand=True)

        self.tab_flow_body = ttk.Frame(self.tab_flow, padding=(14, 12))
        self.tab_flow_body.pack(fill="both", expand=True)

        self.tab_lib_body = ttk.Frame(self.tab_lib, padding=(14, 12))
        self.tab_lib_body.pack(fill="both", expand=True)

        self._build_target(self.tab_target_body)
        self._build_flow(self.tab_flow_body)
        self._build_lib(self.tab_lib_body)
        self._build_log_panel()
        self.after(400, self.refresh_login_status)

    def _build_topbar(self) -> None:
        bar = ttk.Frame(self, style="Header.TFrame")
        bar.pack(fill="x", padx=0, pady=0)

        inner = ttk.Frame(bar, style="Header.TFrame")
        inner.pack(fill="x", padx=16, pady=(14, 10))

        left = ttk.Frame(inner, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)

        if theme:
            title = ttk.Frame(left, style="Header.TFrame")
            title.pack(side="left", padx=(0, 22))
            dot = tk.Frame(title, background=theme.PINK, width=4, height=22)
            dot.pack(side="left", padx=(0, 8))
            dot.pack_propagate(False)
            ttk.Label(title, text="B站歌下载", style="AccentTitle.TLabel").pack(side="left")

        self.var_target = tk.StringVar(value="未配置目标")
        lbl = ttk.Label(left, textvariable=self.var_target, style="TLabel",
                        font=("Microsoft YaHei UI", 10, "bold"))
        lbl.pack(side="left", padx=(14, 0))
        if theme:
            lbl.configure(foreground=theme.MUTED)

        right = ttk.Frame(inner, style="Header.TFrame")
        right.pack(side="right")
        ttk.Button(right, text="保存配置", style="Small.TButton",
                   command=self.save_config).pack(side="right")
        ttk.Button(right, text="环境自检", style="Small.TButton",
                   command=self.show_deps).pack(side="right", padx=6)
        ttk.Button(right, text="打开数据目录", style="Small.TButton",
                   command=self.open_base).pack(side="right")

        if theme:
            tk.Frame(self, background=theme.BORDER, height=1).pack(fill="x")

    # ---------------- ① 配置页 ----------------
    def _build_target(self, f) -> None:
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        # ===== 顶部：B站账号 =====
        self._build_account_card(f)

        # ===== 子页签 =====
        self.sub_nb = ttk.Notebook(f)
        self.sub_nb.grid(row=1, column=0, sticky="nsew")

        self.sub_basic = ttk.Frame(self.sub_nb)
        self.sub_rules = ttk.Frame(self.sub_nb)
        self.sub_sanitize = ttk.Frame(self.sub_nb)
        self.sub_adv = ttk.Frame(self.sub_nb)
        self.sub_nb.add(self.sub_basic, text="基本")
        self.sub_nb.add(self.sub_rules, text="黑白名单")
        self.sub_nb.add(self.sub_sanitize, text="标题净化")
        self.sub_nb.add(self.sub_adv, text="高级规则")

        self.scroll_basic = ScrollableFrame(self.sub_basic)
        self.scroll_basic.pack(fill="both", expand=True, padx=12, pady=10)
        self.scroll_rules = ScrollableFrame(self.sub_rules)
        self.scroll_rules.pack(fill="both", expand=True, padx=12, pady=10)
        self.scroll_sanitize = ScrollableFrame(self.sub_sanitize)
        self.scroll_sanitize.pack(fill="both", expand=True, padx=12, pady=10)
        self.scroll_adv = ScrollableFrame(self.sub_adv)
        self.scroll_adv.pack(fill="both", expand=True, padx=12, pady=10)

        self._build_basic(self.scroll_basic.body)
        self._build_rules(self.scroll_rules.body)
        self._build_sanitize(self.scroll_sanitize.body)
        self._build_advanced(self.scroll_adv.body)

        # 底部：预设 + 保存
        foot = ttk.Frame(f)
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(foot, text="载入规则预设…", style="Ghost.TButton",
                   command=self.load_preset).pack(side="left")
        ttk.Button(foot, text="载入空白模板", style="Small.TButton",
                   command=self.load_blank_preset).pack(side="left", padx=6)
        ttk.Button(foot, text="保存配置", style="Accent.TButton",
                   command=self.save_config).pack(side="right")

    # ---------- 顶部：B站账号卡片 ----------
    def _build_account_card(self, parent) -> None:
        card = ttk.LabelFrame(parent, text="  B站账号  ", padding=12)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        # 状态行
        row1 = ttk.Frame(card)
        row1.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.var_login = tk.StringVar(value="正在检测登录状态…")
        self.lbl_login = ttk.Label(row1, textvariable=self.var_login,
                                   style="TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_login.pack(side="left")
        ttk.Button(row1, text="刷新状态", style="Small.TButton",
                   command=self.refresh_login_status).pack(side="right")

        # 粘贴行
        row2 = ttk.Frame(card)
        row2.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(row2, text="粘贴新 SESSDATA：", style="CardMuted.TLabel").pack(side="left")
        self.ent_sessdata = ttk.Entry(row2)
        self.ent_sessdata.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(row2, text="保存并验证", style="SmallAccent.TButton",
                   command=self.update_sessdata).pack(side="left")
        ttk.Button(row2, text="怎么获取？", style="Small.TButton",
                   command=self.show_cookie_help).pack(side="left", padx=(6, 0))

        self.var_cookie_path = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.var_cookie_path,
                  style="CardMuted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def refresh_login_status(self) -> None:
        """后台查 nav，更新「已登录/未登录」显示。"""
        self.var_login.set("正在检测登录状态…")
        if theme:
            self.lbl_login.configure(foreground=theme.MUTED)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg)
                info = pl.account_status()
                self.log_queue.put(("login_status", json.dumps(info, ensure_ascii=False)))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("login_status", json.dumps(
                    {"logged_in": False, "message": str(e)})))
        threading.Thread(target=work, daemon=True).start()

    def _apply_login_status(self, info: dict) -> None:
        vip = {0: "普通", 1: "大会员", 2: "年费大会员"}.get(info.get("vip_type", 0), "")
        if info.get("logged_in"):
            name = info.get("uname") or "?"
            mid = info.get("mid") or ""
            self.var_login.set(f"● 已登录：{name}（mid {mid}｜{vip}）")
            color = theme.SUCCESS if theme else "green"
        elif info.get("cookie_expired"):
            self.var_login.set("● 未登录：cookies.txt 里的 SESSDATA 已过期，"
                               "下载可用但最高 192K，抓取容易 412。粘贴新的即可恢复")
            color = theme.WARN if theme else "#b87400"
        elif info.get("sessdata_len"):
            self.var_login.set("● 未登录：有 SESSDATA 但验证失败（"
                               + str(info.get("message") or "")[:60] + "）")
            color = theme.WARN if theme else "#b87400"
        else:
            self.var_login.set("● 未登录：没有 cookies.txt（可用，但最高 192K、抓取容易 412）")
            color = theme.MUTED if theme else "gray"
        if theme:
            self.lbl_login.configure(foreground=color)
        try:
            pl = pipe_mod.Pipeline(self.cfg)
            p = pl._find_sessdata_path()
            self.var_cookie_path.set(
                f"cookies 文件：{p if p else '（未找到）'}"
                f"　（放程序目录或数据目录都会被自动识别）")
        except Exception:  # noqa: BLE001
            pass

    def update_sessdata(self) -> None:
        pasted = self.ent_sessdata.get().strip()
        if not pasted:
            messagebox.showinfo("粘贴 SESSDATA",
                                "先把 SESSDATA 粘进来。\n"
                                "支持三种格式：整行 Netscape / SESSDATA=xxx / 直接粘值。")
            return
        try:
            self.save_config(silent=True)
            pl = pipe_mod.Pipeline(self.cfg)
            sess = pl.save_sessdata(pasted)
        except ValueError as e:
            messagebox.showerror("没解析出 SESSDATA", str(e))
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", f"{type(e).__name__}: {e}")
            return
        self.ent_sessdata.delete(0, "end")
        self._log("ok", f"SESSDATA 已保存（长度 {len(sess)}），正在验证…")
        self.refresh_login_status()

    def show_cookie_help(self) -> None:
        msg = (
            "怎么获取 SESSDATA（约 1 分钟）：\n\n"
            "① 浏览器打开 bilibili.com 并确认已登录\n"
            "② 按 F12 → 「应用/Application」→ Cookie → https://www.bilibili.com\n"
            "③ 找到名为 SESSDATA 的行，双击它的「值」，整段复制\n"
            "   （带 %2C 等符号原样复制，别改动）\n"
            "④ 粘贴到左边的框 → 点「保存并验证」\n\n"
            "也可以直接粘整行（.bilibili.com…SESSDATA…值）\n"
            "或整串 cookie（SESSDATA=xxx; 别的=yyy），程序都认。\n\n"
            "保存位置：数据目录或程序目录的 cookies.txt（自动写入）。\n"
            "有效期约 360 天；它等同登录凭证，不要发给别人。")
        messagebox.showinfo("获取 SESSDATA", msg)

    # ---------- 子页：黑白名单 ----------
    def _build_rules(self, body) -> None:
        # 黑名单
        card = self._card(body, "黑名单（命中即排除）")
        grid = ttk.Frame(card)
        grid.pack(fill="x")
        self.txt_black_kw = self._mk_text(grid, "通用关键词\n（换目标也成立）")
        self.txt_black_kw.insert("1.0", "\n".join(self.cfg.exclude_kws_generic))
        self.txt_black_pre = self._mk_text(grid, "通用前缀\n（只判开头，不带【】）")
        self.txt_black_pre.insert("1.0", "\n".join(self.cfg.exclude_prefix_generic))
        self.txt_black_re = self._mk_text(grid, "通用正则\n（如 SEE 反应类）")
        self.txt_black_re.insert("1.0", "\n".join(self.cfg.exclude_regex_generic))
        self.txt_black_pkw = self._mk_text(grid, "专属关键词\n（人名/粉丝团/梗/综艺）")
        self.txt_black_pkw.insert("1.0", "\n".join(self.cfg.exclude_kws_project))
        self.txt_black_ppre = self._mk_text(grid, "专属前缀")
        self.txt_black_ppre.insert("1.0", "\n".join(self.cfg.exclude_prefix_project))
        self.txt_black_chorus = self._mk_text(grid, "除非合唱\n（命中排除，含「合唱」豁免）")
        self.txt_black_chorus.insert("1.0", "\n".join(self.cfg.exclude_unless_chorus_kws))

        ttk.Label(card,
                  text="铁律：多P 歌回只判分P名，单P 才判标题；合集总标题永远不参与。"
                       "关键词忽略空格；前缀先剥【】等符号，所以不要带【】写。",
                  style="RaisedMuted.TLabel").pack(anchor="w", pady=(8, 0))

        # 白名单
        card2 = self._card(body, "白名单（命中强制收录，优先级高于黑名单）")
        g2 = ttk.Frame(card2)
        g2.pack(fill="x")
        self.txt_white_kw = self._mk_text(g2, "白名单关键词")
        self.txt_white_kw.insert("1.0", "\n".join(self.cfg.include_kws))
        self.txt_white_pre = self._mk_text(g2, "白名单前缀（不带【】）")
        self.txt_white_pre.insert("1.0", "\n".join(self.cfg.include_prefix))

        # 人工 BV
        card3 = self._card(body, "人工强制判定（BV 号）")
        g3 = ttk.Frame(card3)
        g3.pack(fill="x")
        self.txt_force1 = self._mk_text(g3, "force_cat1\n强制判①（确认在唱）")
        self.txt_force1.insert("1.0", "\n".join(self.cfg.force_cat1))
        self.txt_force3 = self._mk_text(g3, "force_cat3\n强制判③（确认不在唱）")
        self.txt_force3.insert("1.0", "\n".join(self.cfg.force_cat3))
        self.txt_nonsong = self._mk_text(g3, "nonsong_bv\n核实「不是唱歌」整支排除")
        self.txt_nonsong.insert("1.0", "\n".join(self.cfg.nonsong_bv))
        ttk.Label(card3,
                  text="分P级排除/补录（force_non_song_units、manual_key、manual_disp）"
                       "在 manual_labels.json 里编辑，去重时生效。",
                  style="RaisedMuted.TLabel").pack(anchor="w", pady=(6, 0))

    def _mk_text(self, parent, title: str, height: int = 6, width: int = 26) -> tk.Text:
        col = ttk.Frame(parent)
        col.pack(side="left", padx=(0, 8))
        ttk.Label(col, text=title, style="RaisedMuted.TLabel").pack(anchor="w")
        t = tk.Text(col, height=height, width=width, wrap="none",
                    font=("Consolas", 9), relief="sunken", borderwidth=1, bg="#ffffff")
        t.pack()
        return t

    # ---------- 子页：标题净化 ----------
    def _build_sanitize(self, body) -> None:
        card = self._card(body, "标题净化规则（只影响文件名/显示名，不影响判定）")
        san = self.cfg.sanitize
        self.san_vars = {}
        items = [
            ("strip_emoji", "去 emoji 表情（💃🎄🔥⚡…）"),
            ("normalize_fancy", "花体字母还原（𝐍𝐈𝐆𝐇𝐓 → NIGHT）"),
            ("strip_special", "去装饰符号（箭头/几何/颜文字/全角＾…）"),
            ("strip_bracket_tags", "去【…】标签"),
            ("strip_series_no", "去 #N 系列号（保护「心拍数#0822」）"),
            ("strip_lead_punct", "去开头标点（【】剥完剩「，xxx」时保留标签文字）"),
            ("keep_trailing_tone", "保留结尾语气符（~～ 等，关掉则一并删）"),
        ]
        for key, label in items:
            v = tk.BooleanVar(value=bool(san.get(key, True)))
            self.san_vars[key] = v
            ttk.Checkbutton(card, text=label, variable=v,
                            command=self._preview_sanitize).pack(anchor="w", pady=1)

        prow = ttk.Frame(card)
        prow.pack(fill="x", pady=(8, 0))
        ttk.Label(prow, text="额外要删的字符：", style="Raised.TLabel").pack(side="left")
        self.var_extra_chars = tk.StringVar(value=san.get("extra_strip_chars", ""))
        e = ttk.Entry(prow, textvariable=self.var_extra_chars, width=40)
        e.pack(side="left", padx=(6, 6))
        e.bind("<KeyRelease>", lambda _ev: self._preview_sanitize())

        # 实时预览
        pv = ttk.LabelFrame(card, text="  实时预览  ", padding=10)
        pv.pack(fill="x", pady=(10, 0))
        self.var_preview_src = tk.StringVar(
            value="【某某】26.11.14迷人歌回 坏女孩🔥 心拍数#0822 ~𝐁𝐀𝐃")
        ttk.Entry(pv, textvariable=self.var_preview_src).pack(fill="x")
        self.var_preview_out = tk.StringVar(value="")
        out = ttk.Label(pv, textvariable=self.var_preview_out, style="Big.TLabel")
        out.pack(anchor="w", pady=(6, 0))
        self._preview_sanitize()

        ttk.Label(card,
                  text="⚠️ 改净化规则会影响去重键（歌名归一化）——保存后要重跑「③ 去重」才生效；"
                       "已下载文件不会自动改名。",
                  style="RaisedMuted.TLabel").pack(anchor="w", pady=(8, 0))

    def _collect_sanitize(self) -> dict:
        return {
            "strip_emoji": self.san_vars["strip_emoji"].get(),
            "normalize_fancy": self.san_vars["normalize_fancy"].get(),
            "strip_special": self.san_vars["strip_special"].get(),
            "strip_bracket_tags": self.san_vars["strip_bracket_tags"].get(),
            "strip_series_no": self.san_vars["strip_series_no"].get(),
            "strip_lead_punct": self.san_vars["strip_lead_punct"].get(),
            "keep_trailing_tone": self.san_vars["keep_trailing_tone"].get(),
            "extra_strip_chars": self.var_extra_chars.get(),
        }

    def _preview_sanitize(self) -> None:
        try:
            old = naming.get_sanitize()
            naming.set_sanitize(self._collect_sanitize())
            from core import rules as _r
            s = self.var_preview_src.get()
            # 与文件名同一条路径：strip_emoji → base_name
            out = naming.base_name(_r.strip_emoji(s))
            self.var_preview_out.set(f"净化结果：{out}")
            naming.set_sanitize(old)
        except Exception as e:  # noqa: BLE001
            self.var_preview_out.set(f"（预览失败：{e}）")

    # ---------- 子页：高级规则 ----------
    def _build_advanced(self, body) -> None:
        card = self._card(body, "按 UP 的清洗规则（正则表达式.txt 那套）")
        self.txt_uprules = tk.Text(card, height=10, wrap="none",
                                   font=("Consolas", 9), relief="sunken",
                                   borderwidth=1, bg="#ffffff")
        self.txt_uprules.pack(fill="x")
        self.txt_uprules.insert("1.0", self._uprules_to_text(self.cfg.up_clean_rules))
        ttk.Label(card,
                  text="每行一条：UP名 | 删除串(用;分隔) | 提取正则(取第1捕获组) | 跳过词(用;分隔)\n"
                       "例：某UP名 | 😭;【某某】 | 歌回(.*?)- | 二次元专场;loser",
                  style="RaisedMuted.TLabel").pack(anchor="w", pady=(6, 0))

        card2 = self._card(body, "弹幕/评论校验正则（「校验」功能用；留空=按目标名自动拼）")
        rows = [
            ("verify_mark_regex", "显式署名（为主唱）",
             "【某某】|某某翻唱|feat.某某|某某×…"),
            ("verify_collab_regex", "合作/feat 署名", ""),
            ("verify_sing_regex", "目标+唱歌语境（评论/弹幕）", ""),
            ("verify_group_regex", "群体企划（命中→他人）", "这群人|十人|UP合唱|拜年祭|出征曲"),
            ("verify_role_regex", "角色向演唱（命中→他人）", "(初音未来|博丽灵梦…).{0,12}?(唱|献唱)"),
            ("verify_named_others", "他人唱见名单（命中→他人）", "kradness|鹿乃|まふまふ|そらる"),
        ]
        self.adv_vars = {}
        for key, label, ph in rows:
            r = ttk.Frame(card2)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, style="Raised.TLabel", width=26).pack(side="left")
            v = tk.StringVar(value=getattr(self.cfg, key, ""))
            self.adv_vars[key] = v
            ttk.Entry(r, textvariable=v).pack(side="left", fill="x", expand=True)

        card3 = self._card(body, "其他判定参数")
        self.var_verified_label = tk.StringVar(value=self.cfg.verified_label)
        r = ttk.Frame(card3)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="已核实标签名（_verified 里的）", style="Raised.TLabel").pack(side="left")
        ttk.Entry(r, textvariable=self.var_verified_label, width=16).pack(side="left", padx=8)
        self.var_other_names = tk.StringVar(value=self.cfg.other_names)
        r2 = ttk.Frame(card3)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="纯他人名单（正则）", style="Raised.TLabel").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_other_names).pack(side="left", fill="x",
                                                              expand=True, padx=8)

    @staticmethod
    def _uprules_to_text(rules: list) -> str:
        lines = []
        for r in rules or []:
            up = r.get("up") or ""
            rem = ";".join(r.get("remove") or [])
            ex = r.get("extract_regex") or ""
            sk = ";".join(r.get("skip_parts") or [])
            lines.append(f"{up} | {rem} | {ex} | {sk}")
        return "\n".join(lines)

    @staticmethod
    def _text_to_uprules(text: str) -> list:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if not parts or not parts[0]:
                continue
            rule = {"up": parts[0]}
            if len(parts) > 1 and parts[1]:
                rule["remove"] = [x for x in parts[1].split(";") if x]
            if len(parts) > 2 and parts[2]:
                rule["extract_regex"] = parts[2]
            if len(parts) > 3 and parts[3]:
                rule["skip_parts"] = [x for x in parts[3].split(";") if x]
            out.append(rule)
        return out
    def _build_basic(self, body) -> None:
        card = self._card(body, "目标与路径")
        self.var_name = tk.StringVar(value=self.cfg.target_name)
        self.var_alias = tk.StringVar(value=self.cfg.target_aliases)
        self.var_base = tk.StringVar(value=str(self.cfg.base_dir))
        self.var_self = tk.StringVar(value=str(self.cfg.self_mid or ""))
        self.var_priority = tk.StringVar(value=self.cfg.priority_up)

        ttk.Label(card, text="目标名", style="Raised.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(card, textvariable=self.var_name, width=24).grid(
            row=0, column=1, sticky="ew", padx=(0, 16), pady=3)
        ttk.Label(card, text="本人 mid", style="Raised.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(card, textvariable=self.var_self, width=14).grid(
            row=0, column=3, sticky="ew", padx=(0, 16), pady=3)
        ttk.Label(card, text="优先号", style="Raised.TLabel").grid(
            row=0, column=4, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(card, textvariable=self.var_priority, width=18).grid(
            row=0, column=5, sticky="ew", pady=3)

        ttk.Label(card, text="别名正则", style="Raised.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(card, textvariable=self.var_alias, width=24).grid(
            row=1, column=1, sticky="ew", padx=(0, 16), pady=3)
        ttk.Label(card, text="（判断标题是否提到目标用；多个用 | 分隔）",
                  style="RaisedMuted.TLabel").grid(
            row=1, column=2, columnspan=4, sticky="w", pady=3)

        ttk.Label(card, text="数据目录", style="Raised.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(card, textvariable=self.var_base, width=44).grid(
            row=2, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Button(card, text="浏览…", style="Small.TButton",
                   command=self.pick_base).grid(row=2, column=5, sticky="w", padx=(8, 0))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)
        card.columnconfigure(5, weight=1)

        # 账号
        card2 = self._card(body, "要跟踪的 UP 主")
        self.txt_accounts = tk.Text(card2, height=7, wrap="none", font=("Consolas", 10),
                                    relief="sunken", borderwidth=1, bg="#ffffff")
        self.txt_accounts.insert("1.0", self._accounts_to_text(self.cfg.accounts))
        self.txt_accounts.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(card2, text="一行一个，格式：mid 空格 名字   （例：12345678 某UP名）",
                  style="RaisedMuted.TLabel").grid(row=1, column=0, columnspan=2,
                                                   sticky="w", pady=(6, 0))
        ttk.Label(card2, text="「裸歌名=在唱」频道 mid", style="Raised.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.var_zl_only = tk.StringVar(value=", ".join(str(x) for x in self.cfg.zl_only))
        ttk.Entry(card2, textvariable=self.var_zl_only).grid(
            row=2, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(card2, text="（综合搬运号绝对不能放进来）",
                  style="RaisedMuted.TLabel").grid(row=3, column=0, columnspan=2,
                                                   sticky="w", pady=(2, 0))
        ttk.Label(card2, text="纯搬运合集号 mid（整号排除）", style="Raised.TLabel").grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        self.var_comp_mids = tk.StringVar(
            value=", ".join(str(x) for x in self.cfg.compilation_mids))
        ttk.Entry(card2, textvariable=self.var_comp_mids).grid(
            row=4, column=1, sticky="ew", padx=(8, 0))
        card2.columnconfigure(1, weight=1)

        # 下载参数
        card3 = self._card(body, "下载参数")
        self.var_audio = tk.StringVar(value=self._audio_text(self.cfg.audio_priority))
        self.var_format = tk.StringVar(value=self._format_text(self.cfg.audio_format))
        self.var_jobs = tk.IntVar(value=self.cfg.download_jobs)
        self.var_interval = tk.DoubleVar(value=self.cfg.api_interval)

        ttk.Label(card3, text="音质", style="Raised.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Combobox(card3, textvariable=self.var_audio, state="readonly",
                     values=[c[1] for c in AUDIO_CHOICES], width=24).grid(
            row=0, column=1, sticky="ew", padx=(0, 16))
        ttk.Label(card3, text="格式", style="Raised.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Combobox(card3, textvariable=self.var_format, state="readonly",
                     values=[c[1] for c in FORMAT_CHOICES], width=22).grid(
            row=0, column=3, sticky="ew", padx=(0, 16))
        ttk.Label(card3, text="并发", style="Raised.TLabel").grid(
            row=0, column=4, sticky="w", padx=(0, 8))
        ttk.Spinbox(card3, from_=1, to=8, textvariable=self.var_jobs, width=6).grid(
            row=0, column=5, sticky="w", padx=(0, 16))
        ttk.Label(card3, text="API 间隔(s)", style="Raised.TLabel").grid(
            row=0, column=6, sticky="w", padx=(0, 8))
        ttk.Spinbox(card3, from_=0.5, to=10.0, increment=0.1,
                    textvariable=self.var_interval, width=6).grid(
            row=0, column=7, sticky="w")
        ttk.Label(card3, text="并发别贪：4 是实测稳定上限；间隔别低于 1.1",
                  style="RaisedMuted.TLabel").grid(
            row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))
        card3.columnconfigure(1, weight=1)
        card3.columnconfigure(3, weight=1)

    def _card(self, parent, title):
        """建一张卡片（统一用 pack 放入父容器，内部用 grid）。"""
        if theme:
            return theme.make_card(parent, title)
        lf = ttk.LabelFrame(parent, text=title, padding=14)
        lf.pack(fill="x", pady=(0, 12))
        return lf

    @staticmethod
    def _audio_text(value: str) -> str:
        for v, t in AUDIO_CHOICES:
            if v == value:
                return t
        return AUDIO_CHOICES[0][1]

    @staticmethod
    def _format_text(value: str) -> str:
        for v, t in FORMAT_CHOICES:
            if v == value:
                return t
        return FORMAT_CHOICES[0][1]

    def _accounts_to_text(self, accounts: list) -> str:
        return "\n".join(f"{a.get('mid')} {a.get('name')}" for a in accounts)

    def _text_to_accounts(self) -> list:
        out = []
        for line in self.txt_accounts.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            mid = parts[0].strip()
            if not mid.isdigit():
                continue
            name = parts[1].strip() if len(parts) > 1 else mid
            out.append({"mid": int(mid), "name": name})
        return out

    def _text_lines(self, widget) -> list:
        return [x.strip() for x in widget.get("1.0", "end").splitlines() if x.strip()]

    def pick_base(self) -> None:
        d = filedialog.askdirectory(title="选择数据目录")
        if d:
            self.var_base.set(d)

    # ---------------- ② 流程页 ----------------
    def _build_flow(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        # 步骤卡片
        steps = ttk.LabelFrame(parent, text="  五步流程  ", padding=14)
        steps.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        steps.columnconfigure(0, weight=1)

        btns = [
            ("① 抓取", "fetch", "拉取各 UP 的投稿列表", "Accent.TButton"),
            ("② 判定", "classify", "判 ①本人在唱 / ②疑似 / ③无关", "TButton"),
            ("③ 去重", "dedup", "两层去重 → 基底 → 下载清单", "TButton"),
            ("④ 下载", "download", "按基底清单下载音频", "Accent.TButton"),
            ("⑤ 校验", "verify", "检查缺失 / 损坏 / 时长", "TButton"),
        ]
        self.btn_map = {}
        # 五个步骤排成两行网格，避免窗口窄时按钮被挤出可视区
        grid = ttk.Frame(steps)
        grid.pack(fill="x")
        for i, (text, key, tip, style) in enumerate(btns):
            r, c = divmod(i, 3)
            cell = ttk.Frame(grid)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 24), pady=(0, 6))
            b = ttk.Button(cell, text=text, style=style,
                           command=lambda k=key: self.run_step(k))
            b.pack(side="left")
            self.btn_map[key] = b
            ttk.Label(cell, text=tip, style="Muted.TLabel").pack(side="left", padx=(6, 0))

        opts = ttk.Frame(steps)
        opts.pack(fill="x", pady=(10, 0))
        self.var_incremental = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="抓取时用增量（只拉新投稿）",
                        variable=self.var_incremental).pack(side="left")
        ttk.Label(opts, text="首次抓取请取消勾选", style="Muted.TLabel").pack(
            side="left", padx=(10, 0))
        self.var_only_self = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="只下载本人投稿",
                        variable=self.var_only_self).pack(side="left", padx=(20, 0))
        ttk.Button(opts, text="扫描：还有谁在切目标", style="Small.TButton",
                   command=self.run_scan).pack(side="left", padx=(20, 0))
        ttk.Button(opts, text="过期文件归位", style="Small.TButton",
                   command=self.run_pending).pack(side="left", padx=6)

        # 第二行：辅助步骤
        opts2 = ttk.Frame(steps)
        opts2.pack(fill="x", pady=(8, 0))
        ttk.Button(opts2, text="⑥ 弹幕/评论校验", style="Small.TButton",
                   command=self.run_comment_verify).pack(side="left")
        ttk.Label(opts2, text="抓评论+弹幕，标出「确认本唱/他人唱/待核对」",
                  style="Muted.TLabel").pack(side="left", padx=(6, 18))
        ttk.Button(opts2, text="⑦ 构建歌名词典", style="Small.TButton",
                   command=self.run_build_songs).pack(side="left")
        ttk.Label(opts2, text="从分P名抽真实歌名，补进 _songs.txt（① 偏少时跑）",
                  style="Muted.TLabel").pack(side="left", padx=(6, 0))

        # 状态 + 统计
        stat = ttk.LabelFrame(parent, text="  状态  ", padding=12)
        stat.grid(row=1, column=0, sticky="nsew")
        stat.columnconfigure(1, weight=1)

        self.var_status = tk.StringVar(value="空闲")
        ttk.Label(stat, textvariable=self.var_status, style="Card.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.tree_stat = ttk.Treeview(stat, columns=("k", "v"), show="headings",
                                       selectmode="none", height=8)
        self.tree_stat.heading("k", text="项目")
        self.tree_stat.heading("v", text="值")
        self.tree_stat.column("k", width=200, anchor="w")
        self.tree_stat.column("v", width=420, anchor="w")
        self.tree_stat.grid(row=1, column=0, columnspan=2, sticky="nsew")
        stat.rowconfigure(1, weight=1)

        bt = ttk.Frame(stat)
        bt.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bt, text="打开文档目录", style="Small.TButton",
                   command=self.open_docs).pack(side="left")
        ttk.Button(bt, text="打开音频目录", style="Small.TButton",
                   command=self.open_audio).pack(side="left", padx=6)
        ttk.Button(bt, text="刷新曲库", style="Small.TButton",
                   command=self.load_manifest).pack(side="left", padx=6)

    # ---------------- ③ 曲库页 ----------------
    def _build_lib(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        head = ttk.LabelFrame(parent, text="  曲库（基底：每首歌一条）  ", padding=10)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.var_lib = tk.StringVar(value="尚未生成 —— 请先执行「③ 去重」")
        ttk.Label(head, textvariable=self.var_lib, style="CardMuted.TLabel").pack(side="left")
        ttk.Button(head, text="全选", style="Small.TButton",
                   command=lambda: self._check_all(True)).pack(side="right")
        ttk.Button(head, text="全不选", style="Small.TButton",
                   command=lambda: self._check_all(False)).pack(side="right", padx=6)
        ttk.Button(head, text="下载选中项", style="SmallAccent.TButton",
                   command=self.download_selected).pack(side="right", padx=6)

        body = ttk.Frame(parent)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        cols = ("sel", "date", "up", "title", "dur", "bv")
        heads = {"sel": "选", "date": "日期", "up": "UP",
                 "title": "曲目", "dur": "时长", "bv": "BV"}
        widths = {"sel": 44, "date": 92, "up": 120, "title": 320, "dur": 62, "bv": 130}
        self.tree_lib = ttk.Treeview(body, columns=cols, show="headings",
                                     selectmode="none", height=8)
        for c in cols:
            self.tree_lib.heading(c, text=heads[c])
            self.tree_lib.column(c, width=widths[c], minwidth=40,
                                 stretch=(c == "title"),
                                 anchor="w" if c in ("title", "date", "up") else "center")
        if theme:
            theme.stripe_tree(self.tree_lib)
        vs = ttk.Scrollbar(body, orient="vertical", command=self.tree_lib.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=self.tree_lib.xview)
        self.tree_lib.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree_lib.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.tree_lib.bind("<Button-1>", self._on_lib_click)
        self.tree_lib.bind("<Button-3>", self._on_lib_right)
        self.tree_lib.bind("<Double-1>", self._on_lib_double)

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="复制 BV 号", command=self._copy_bv)
        self._menu.add_command(label="复制标题", command=self._copy_title)
        self._menu.add_command(label="在 B 站打开", command=self._open_bilibili)

        # 详情
        self.var_detail = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.var_detail,
                  style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))

    # ---------------- 日志面板 ----------------
    def _build_log_panel(self) -> None:
        holder = ttk.Frame(self)
        holder.pack(fill="x", padx=14, pady=(8, 10))

        head = ttk.Frame(holder)
        head.pack(fill="x")
        self.var_log_open = tk.BooleanVar(value=False)
        ttk.Button(head, text="▸ 日志", style="Link.TButton",
                   command=self.toggle_log).pack(side="left")
        ttk.Button(head, text="清空", style="Small.TButton",
                   command=self.clear_log).pack(side="right")
        ttk.Button(head, text="打开日志文件", style="Small.TButton",
                   command=self.open_logfile).pack(side="right", padx=6)

        self.log_holder = ttk.Frame(holder)
        self.log_holder.pack(fill="x", pady=(6, 0))
        if theme:
            self.log_text = theme.make_log_widget(self.log_holder)
        else:
            self.log_text = tk.Text(self.log_holder, height=10, wrap="word")
        self.log_text.pack(fill="x")
        self.log_holder.pack_forget()

    def toggle_log(self) -> None:
        if self.log_holder.winfo_manager():
            self.log_holder.pack_forget()
            self.var_log_open.set(False)
        else:
            self.log_holder.pack(fill="x", pady=(6, 0))
            self.var_log_open.set(True)

    # ================= 状态 =================
    def _load_state(self) -> None:
        self._update_target_label()
        self.load_manifest()
        self._refresh_stats()

    def _update_target_label(self) -> None:
        n = len(self.cfg.accounts)
        if self.cfg.is_configured():
            self.var_target.set(f"目标：{self.cfg.target_name}　｜　跟踪 {n} 个号")
        else:
            self.var_target.set("未配置目标 —— 请到「配置」页填写")

    def _refresh_stats(self) -> None:
        cfg = self.cfg
        rows = []

        def add(k, v):
            rows.append((k, v))

        add("目标", cfg.target_name)
        add("数据目录", str(cfg.base_dir))
        add("跟踪账号", "、".join(f"{a.get('name')}({a.get('mid')})"
                                 for a in cfg.accounts) or "（未配置）")
        add("投稿缓存", f"{len(list(cfg.data_dir.glob('_subs_*.json')))} 个号")
        n_base = 0
        if cfg.base_index.is_file():
            try:
                n_base = len(json.loads(cfg.base_index.read_text(encoding="utf-8"))
                             .get("songs") or {})
            except Exception:  # noqa: BLE001
                n_base = 0
        add("基底歌数", f"{n_base} 首")
        add("下载清单", f"{len(self.manifest)} 条")
        add("已下载音频", f"{self._count_audio()} 个")
        add("cookies", "已加载" if self._cookies_ok() else "缺失（最高只能到 192K）")
        for status, label, text in (deps_mod.summary_lines() if deps_mod else []):
            add(f"环境 {label}", text)

        self.tree_stat.delete(*self.tree_stat.get_children())
        for i, (k, v) in enumerate(rows):
            self.tree_stat.insert("", "end", values=(k, v),
                                  tags=("odd" if i % 2 else "even",))

    @staticmethod
    def _count_audio() -> int:
        cfg = cfg_mod.load()
        n = 0
        for d in (cfg.dir_self, cfg.dir_other):
            try:
                n += len([f for f in os.listdir(d)
                          if f.endswith((".m4a", ".flac", ".mp3"))])
            except OSError:
                pass
        return n

    @staticmethod
    def _cookies_ok() -> bool:
        cfg = cfg_mod.load()
        for p in (cfg.base_dir / cfg.cookies_file, APP_ROOT / "cookies.txt"):
            if p.is_file():
                try:
                    if "SESSDATA" in p.read_text(encoding="utf-8", errors="ignore"):
                        return True
                except OSError:
                    pass
        return False

    # ================= 曲库 =================
    def load_manifest(self) -> None:
        cfg = self.cfg
        self.manifest = []
        if cfg.manifest.is_file():
            try:
                self.manifest = json.loads(cfg.manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.manifest = []
        self.tree_lib.delete(*self.tree_lib.get_children())
        self._checked.clear()
        for i, it in enumerate(self.manifest):
            self.tree_lib.insert("", "end", iid=str(i), values=(
                "☐", it.get("date") or "", it.get("up") or "",
                it.get("name") or "", rules.fmt_dur(it.get("dur")), it.get("bv") or "",
            ), tags=("odd" if i % 2 else "even",))
        if self.manifest:
            self.var_lib.set(f"共 {len(self.manifest)} 条（每首歌一条）"
                             f"　已选 {len(self._checked)} 条")
        else:
            self.var_lib.set("尚未生成 —— 请先执行「③ 去重」")
        self._refresh_stats()

    def _check_all(self, on: bool) -> None:
        self._checked.clear() if not on else self._checked.update(
            range(len(self.manifest)))
        for i in range(len(self.manifest)):
            if self.tree_lib.exists(str(i)):
                self.tree_lib.item(str(i), values=(
                    "☑" if on else "☐",
                    *self.tree_lib.item(str(i), "values")[1:]))
        self.var_lib.set(f"共 {len(self.manifest)} 条（每首歌一条）"
                         f"　已选 {len(self._checked)} 条")

    def _on_lib_click(self, event) -> None:
        iid = self.tree_lib.identify_row(event.y)
        if not iid:
            return
        if self.tree_lib.identify_column(event.x) != "#1":
            return
        idx = int(iid)
        if idx in self._checked:
            self._checked.discard(idx)
            mark = "☐"
        else:
            self._checked.add(idx)
            mark = "☑"
        vals = list(self.tree_lib.item(iid, "values"))
        vals[0] = mark
        self.tree_lib.item(iid, values=vals)
        self.var_lib.set(f"共 {len(self.manifest)} 条（每首歌一条）"
                         f"　已选 {len(self._checked)} 条")

    def _on_lib_right(self, event) -> None:
        iid = self.tree_lib.identify_row(event.y)
        if iid:
            self.tree_lib.selection_set(iid)
            try:
                self._menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._menu.grab_release()

    def _on_lib_double(self, _event) -> None:
        sel = self.tree_lib.selection()
        if sel:
            it = self.manifest[int(sel[0])]
            webbrowser.open(f"https://www.bilibili.com/video/{it.get('bv')}")

    def _copy_bv(self) -> None:
        sel = self.tree_lib.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(str(self.manifest[int(sel[0])].get("bv")))
            self._log("info", "已复制 BV 号")

    def _copy_title(self) -> None:
        sel = self.tree_lib.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(str(self.manifest[int(sel[0])].get("name")))
            self._log("info", "已复制标题")

    def _open_bilibili(self) -> None:
        sel = self.tree_lib.selection()
        if sel:
            it = self.manifest[int(sel[0])]
            webbrowser.open(f"https://www.bilibili.com/video/{it.get('bv')}")

    # ================= 配置 =================
    def save_config(self, silent: bool = False) -> None:
        cfg = self.cfg
        cfg.target_name = self.var_name.get().strip() or "示例目标"
        cfg.target_aliases = self.var_alias.get().strip() or cfg.target_name
        cfg.base_dir = Path(self.var_base.get().strip() or "./workspace")
        if not cfg.base_dir.is_absolute():
            cfg.base_dir = (APP_ROOT / cfg.base_dir).resolve()
        try:
            cfg.self_mid = int(self.var_self.get().strip() or 0)
        except ValueError:
            cfg.self_mid = 0
        cfg.priority_up = self.var_priority.get().strip()
        cfg.accounts = self._text_to_accounts()
        cfg.zl_only = [int(x) for x in self.var_zl_only.get().replace(",", " ").split()
                       if x.strip().isdigit()]
        cfg.compilation_mids = [int(x) for x in
                                self.var_comp_mids.get().replace(",", " ").split()
                                if x.strip().isdigit()]

        # 黑名单六层
        cfg.exclude_kws_generic = self._text_lines(self.txt_black_kw)
        cfg.exclude_prefix_generic = self._text_lines(self.txt_black_pre)
        cfg.exclude_regex_generic = self._text_lines(self.txt_black_re)
        cfg.exclude_kws_project = self._text_lines(self.txt_black_pkw)
        cfg.exclude_prefix_project = self._text_lines(self.txt_black_ppre)
        cfg.exclude_unless_chorus_kws = self._text_lines(self.txt_black_chorus)
        # 白名单
        cfg.include_kws = self._text_lines(self.txt_white_kw)
        cfg.include_prefix = self._text_lines(self.txt_white_pre)
        # 人工 BV
        cfg.force_cat1 = self._text_lines(self.txt_force1)
        cfg.force_cat3 = self._text_lines(self.txt_force3)
        cfg.nonsong_bv = self._text_lines(self.txt_nonsong)
        # 标题净化 + 按 UP 清洗
        cfg.sanitize = self._collect_sanitize()
        naming.set_sanitize(cfg.sanitize)
        cfg.up_clean_rules = self._text_to_uprules(
            self.txt_uprules.get("1.0", "end"))
        # 高级校验正则
        for key, var in self.adv_vars.items():
            setattr(cfg, key, var.get().strip())
        cfg.verified_label = self.var_verified_label.get().strip() or "verified"
        cfg.other_names = self.var_other_names.get().strip()

        # 音质 / 格式下拉框
        for val, text in AUDIO_CHOICES:
            if text == self.var_audio.get():
                cfg.audio_priority = val
        for val, text in FORMAT_CHOICES:
            if text == self.var_format.get():
                cfg.audio_format = val
        cfg.download_jobs = max(1, min(8, int(self.var_jobs.get() or 3)))
        cfg.api_interval = max(0.5, float(self.var_interval.get() or 1.1))
        # ffmpeg 路径用探测结果覆盖
        if deps_mod:
            ff = deps_mod.resolve("ffmpeg")
            if ff.ok and cfg.ffmpeg in ("", "ffmpeg", "ffprobe"):
                cfg.ffmpeg = ff.path
            fp = deps_mod.resolve("ffprobe")
            if fp.ok and cfg.ffprobe in ("", "ffprobe"):
                cfg.ffprobe = fp.path

        try:
            p = cfg_mod.save_user(cfg, APP_ROOT)
        except OSError as e:
            if not silent:
                messagebox.showerror("保存失败", str(e))
            return
        self.cfg = cfg
        self._update_target_label()
        self._refresh_stats()
        if not silent:
            self._log("ok", f"配置已保存 → {p}")

    # ---------- 预设载入 ----------
    def load_preset(self) -> None:
        """从 JSON 文件载入一套规则（填进表单，不立即保存，可再编辑）。

        预设文件里可能有账号表、人工标注的 BV 号等**私有资料**，
        所以不存在程序目录里 —— 由用户自己选文件。
        """
        init_dir = APP_ROOT.parent / "presets"
        if not init_dir.is_dir():
            init_dir = APP_ROOT
        chosen = filedialog.askopenfilename(
            title="选择规则预设 JSON（含账号 / 黑白名单 / 校验正则等）",
            initialdir=str(init_dir),
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if not chosen:
            return
        p = Path(chosen)
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("预设读取失败", f"{p}\n{e}")
            return
        if not isinstance(data, dict):
            messagebox.showerror("预设格式不对", "顶层应该是一个 JSON 对象。")
            return
        self._fill_form(data)
        self._log("info", f"已载入预设：{p}")
        messagebox.showinfo(
            "已载入规则预设",
            f"规则已从\n{p}\n填进表单（账号、黑白名单、BV、校验正则、按 UP 清洗）。\n"
            "确认无误后点右下角「保存配置」。")

    def load_blank_preset(self) -> None:
        self._fill_form({
            "target_name": "示例目标", "target_aliases": "示例目标",
            "accounts": [], "zl_only": [], "compilation_mids": [],
            "exclude_kws_generic": ["整活"],
            "exclude_prefix_generic": ["手描"],
            "exclude_kws_project": [], "exclude_prefix_project": [],
            "exclude_unless_chorus_kws": [], "exclude_regex_generic": [],
            "include_kws": [], "include_prefix": [],
            "force_cat1": [], "force_cat3": [], "nonsong_bv": [],
            "up_clean_rules": [],
        })

    def _fill_form(self, d: dict) -> None:
        def lines(key):
            return "\n".join(str(x) for x in d.get(key) or [])
        if "target_name" in d:
            self.var_name.set(d["target_name"])
        if "target_aliases" in d:
            self.var_alias.set(d["target_aliases"])
        if "self_mid" in d:
            self.var_self.set(str(d["self_mid"] or ""))
        if "priority_up" in d:
            self.var_priority.set(d["priority_up"])
        if "accounts" in d:
            self.txt_accounts.delete("1.0", "end")
            self.txt_accounts.insert("1.0", self._accounts_to_text(d["accounts"]))
        if "zl_only" in d:
            self.var_zl_only.set(", ".join(str(x) for x in d["zl_only"]))
        if "compilation_mids" in d:
            self.var_comp_mids.set(", ".join(str(x) for x in d["compilation_mids"]))
        pairs = [
            (self.txt_black_kw, "exclude_kws_generic"),
            (self.txt_black_pre, "exclude_prefix_generic"),
            (self.txt_black_re, "exclude_regex_generic"),
            (self.txt_black_pkw, "exclude_kws_project"),
            (self.txt_black_ppre, "exclude_prefix_project"),
            (self.txt_black_chorus, "exclude_unless_chorus_kws"),
            (self.txt_white_kw, "include_kws"),
            (self.txt_white_pre, "include_prefix"),
            (self.txt_force1, "force_cat1"),
            (self.txt_force3, "force_cat3"),
            (self.txt_nonsong, "nonsong_bv"),
        ]
        for widget, key in pairs:
            if key in d:
                widget.delete("1.0", "end")
                widget.insert("1.0", lines(key))
        if "up_clean_rules" in d:
            self.txt_uprules.delete("1.0", "end")
            self.txt_uprules.insert("1.0", self._uprules_to_text(d["up_clean_rules"]))
        for key, var in self.adv_vars.items():
            if key in d:
                var.set(d[key])
        if "verified_label" in d:
            self.var_verified_label.set(d["verified_label"])
        if "other_names" in d:
            self.var_other_names.set(d["other_names"])
        if "sanitize" in d:
            for k, v in d["sanitize"].items():
                if k in self.san_vars and isinstance(v, bool):
                    self.san_vars[k].set(v)
                elif k == "extra_strip_chars":
                    self.var_extra_chars.set(str(v))
            self._preview_sanitize()

    # ================= 执行 =================
    def run_step(self, key: str) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self.save_config()
        self._log("cmd", f"\n▶ {key}")
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg, log=lambda m: self.log_queue.put(("out", str(m))))
                if key == "fetch":
                    pl.step_fetch(incremental=self.var_incremental.get())
                    self.log_queue.put(("refresh", ""))
                elif key == "classify":
                    pl.step_classify()
                    self.log_queue.put(("refresh", ""))
                elif key == "dedup":
                    pl.step_dedup()
                    self.log_queue.put(("refresh", ""))
                elif key == "download":
                    only = "self" if self.var_only_self.get() else "all"
                    pl.step_download(only=only)
                elif key == "verify":
                    pl.step_verify()
                self.log_queue.put(("ok", "✔ 完成"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def run_comment_verify(self) -> None:
        """⑥ 弹幕/评论校验。"""
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self.save_config()
        self._log("cmd", "\n▶ 弹幕/评论校验")
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg,
                                       log=lambda m: self.log_queue.put(("out", str(m))))
                pl.step_verify_comments()
                self.log_queue.put(("ok", "✔ 校验完成（重跑「② 判定」才会用上）"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def run_build_songs(self) -> None:
        """⑦ 构建歌名词典。"""
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self.save_config()
        self._log("cmd", "\n▶ 构建歌名词典")
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg,
                                       log=lambda m: self.log_queue.put(("out", str(m))))
                pl.step_build_songs()
                self.log_queue.put(("ok", "✔ 词典已更新（重跑「② 判定」看效果）"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def run_scan(self) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self.save_config()
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg, log=lambda m: self.log_queue.put(("out", str(m))))
                pl.step_scan()
                self.log_queue.put(("ok", "✔ 扫描完成"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def run_pending(self) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg, log=lambda m: self.log_queue.put(("out", str(m))))
                pl.step_pending()
                self.log_queue.put(("ok", "✔ 归位完成"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def download_selected(self) -> None:
        """只下载曲库里勾选的条目。"""
        if not self._checked:
            messagebox.showinfo("未选择", "请先在「曲库」页勾选要下载的条目。")
            return
        if self.running:
            messagebox.showinfo("正在运行", "已有任务在执行，请等待完成。")
            return
        self.save_config()
        items = [self.manifest[i] for i in sorted(self._checked)]
        tmp = self.cfg.data_dir / "_选中下载.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        self._set_running(True)

        def work():
            try:
                pl = pipe_mod.Pipeline(self.cfg, log=lambda m: self.log_queue.put(("out", str(m))))
                pl.step_download(manifest_path=tmp)
                self.log_queue.put(("ok", f"✔ 下载完成（{len(items)} 条）"))
            except Exception as e:  # noqa: BLE001
                self.log_queue.put(("err", f"✘ {type(e).__name__}: {e}"))
            finally:
                self.log_queue.put(("done", "1"))

        threading.Thread(target=work, daemon=True).start()

    def _set_running(self, on: bool) -> None:
        self.running = on
        self.var_status.set("运行中…" if on else "空闲")
        state = "disabled" if on else "normal"
        for b in self.btn_map.values():
            try:
                b.configure(state=state)
            except tk.TclError:
                pass

    # ================= 日志 =================
    def _log(self, tag: str, msg: str) -> None:
        self.log_queue.put((tag, msg))

    def _poll_log(self) -> None:
        try:
            while True:
                tag, msg = self.log_queue.get_nowait()
                if tag == "refresh":
                    self.load_manifest()
                    continue
                if tag == "login_status":
                    try:
                        self._apply_login_status(json.loads(msg))
                    except json.JSONDecodeError:
                        pass
                    continue
                if tag == "done":
                    self._set_running(False)
                    self._refresh_stats()
                    continue
                t = self.log_text
                t.configure(state="normal")
                t.insert("end", msg + "\n",
                         tag if tag in ("info", "cmd", "err", "ok") else "out")
                t.see("end")
                t.configure(state="disabled")
                self._append_log_file(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll_log)

    def _append_log_file(self, line: str) -> None:
        try:
            d = self.cfg.base_dir / "_脚本与数据"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "bilimusic.log").open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
        except OSError:
            pass

    def clear_log(self) -> None:
        t = self.log_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")

    def open_logfile(self) -> None:
        p = self.cfg.base_dir / "_脚本与数据" / "bilimusic.log"
        self._open_path(p)

    # ================= 打开目录 =================
    def _open_path(self, p: Path) -> None:
        try:
            if IS_WINDOWS:
                os.startfile(str(p))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:  # noqa: BLE001
            self._log("err", f"无法打开 {p}: {e}")

    def open_base(self) -> None:
        self.cfg.base_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.cfg.base_dir)

    def open_docs(self) -> None:
        self.cfg.doc_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.cfg.doc_dir)

    def open_audio(self) -> None:
        self.cfg.dir_other.mkdir(parents=True, exist_ok=True)
        self._open_path(self.cfg.dir_other)

    def show_deps(self) -> None:
        if not deps_mod:
            messagebox.showinfo("环境自检", "依赖模块未加载")
            return
        lines = []
        for status, label, text in deps_mod.summary_lines():
            mark = "✔" if status == "ok" else "✘"
            lines.append(f"{mark} {label:8s} {text}")
        miss = deps_mod.missing()
        if miss:
            lines.append("")
            for n in miss:
                info = deps_mod.DEPENDENCY_INFO[n]
                lines.append(f"缺少 {info['label']}：{info['why']}")
                lines.append(f"  下载：{info['url']}")
                lines.append(f"  做法：{info['hint']}")
            lines.append("")
            lines.append(f"也可以直接放到：{APP_ROOT / 'bin'}")
        messagebox.showinfo("环境自检", "\n".join(lines))


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
