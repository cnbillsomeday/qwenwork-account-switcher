#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
千问办公 · 账户切换器（图形界面）

独立窗口，不依赖 CDP 注入（千问办公屏蔽了 --remote-debugging-port）。
所有备份/切换逻辑直接复用同目录 switcher.py，本文件只做界面与线程调度。

纯标准库 + 系统自带 Tkinter（macOS 自带 Python 3.9 即可运行）。
"""

import io
import json
import queue
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import switcher as sw  # noqa: E402

# ── 配色 ─────────────────────────────────────────────────────────────
BG = "#F5F5F9"
CARD = "#FFFFFF"
CARD_HOVER = "#F3F0FF"
TEXT = "#1C1C28"
SUB = "#8B8B9E"
LINE = "#EAEAF2"
BRAND = "#6B4EFF"
BRAND_SOFT = "#EEE9FF"
GREEN = "#0FA968"
RED = "#E5484D"
AMBER = "#C77700"

GUICONF = sw.SWITCHER_DIR / "gui.json"


def load_guiconf():
    try:
        with GUICONF.open() as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"topmost": True, "window": [420, 620]}


def save_guiconf(cfg):
    try:
        GUICONF.parent.mkdir(parents=True, exist_ok=True)
        with GUICONF.open("w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def initial(name):
    for ch in (name or "?"):
        if ch.strip():
            return ch.upper()
    return "?"


def ellipsis(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fit_px(font, text, max_px):
    """按像素宽度裁剪并补省略号（Tk 的 Label 不会自己省略，会直接截断）"""
    text = text or ""
    if font.measure(text) <= max_px:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ell) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


def flat_btn(parent, text, command, bg, fg, font, padx=10, pady=4, hover=None):
    """tk.Button 在 macOS 上会被画成原生灰按钮，忽略 bg —— 用 Label 模拟扁平按钮"""
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                   padx=padx, pady=pady, cursor="pointinghand")
    lbl.bind("<Button-1>", lambda e: command())
    if hover:
        lbl.bind("<Enter>", lambda e: lbl.configure(bg=hover))
        lbl.bind("<Leave>", lambda e: lbl.configure(bg=bg))
    return lbl


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_guiconf()
        self.busy = False
        self.rows = []
        self._queue = queue.Queue()

        root.title("千问办公 · 账号切换")
        root.configure(bg=BG)
        w, h = self.cfg.get("window", [420, 620])
        ox, oy = self.cfg.get("pos", [80, 80])
        root.geometry(f"{w}x{h}+{ox}+{oy}")
        root.minsize(360, 520)
        root.resizable(True, True)
        try:
            root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        except tk.TclError:
            pass

        self._init_fonts()
        self._init_style()
        self._build()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ── 字体 / 样式 ──────────────────────────────────────────────────
    def _init_fonts(self):
        base = "PingFang SC"
        families = set(tkfont.families())
        if base not in families:
            base = "Helvetica Neue" if "Helvetica Neue" in families else "Helvetica"
        self.f_title = tkfont.Font(family=base, size=15, weight="bold")
        self.f_body = tkfont.Font(family=base, size=13)
        self.f_sub = tkfont.Font(family=base, size=11)
        self.f_small = tkfont.Font(family=base, size=10)
        self.f_mono = tkfont.Font(family="Menlo", size=10)
        self.f_avatar = tkfont.Font(family=base, size=15, weight="bold")

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Brand.TButton", font=self.f_body, foreground="#FFFFFF",
                     background=BRAND, borderwidth=0, focusthickness=0,
                     padding=(14, 9))
        st.map("Brand.TButton",
               background=[("active", "#5A3EE8"), ("disabled", "#C9C2F0")],
               foreground=[("disabled", "#FFFFFF")])
        st.configure("Ghost.TButton", font=self.f_body, foreground=TEXT,
                     background=CARD, borderwidth=0, padding=(12, 9))
        st.map("Ghost.TButton", background=[("active", "#EFEFF6")])

    # ── 布局 ────────────────────────────────────────────────────────
    def _build(self):
        # 顶栏
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="千问办公 · 账号切换", font=self.f_title,
                 bg=BG, fg=TEXT).pack(side="left")

        self.topmost_var = tk.BooleanVar(value=bool(self.cfg.get("topmost", True)))
        self.pin = tk.Label(head, text="", font=self.f_small, bg=BG, fg=SUB,
                            padx=6, cursor="pointinghand")
        self.pin.pack(side="right")
        self.pin.bind("<Button-1>", lambda e: self.toggle_topmost())
        self._sync_pin()

        # 当前账号卡
        self.cur_card = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                                 highlightthickness=1)
        self.cur_card.pack(fill="x", padx=16, pady=(4, 12))
        inner = tk.Frame(self.cur_card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)

        self.cur_avatar = tk.Canvas(inner, width=44, height=44, bg=CARD,
                                    highlightthickness=0)
        self.cur_avatar.pack(side="left")
        info = tk.Frame(inner, bg=CARD)
        info.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.cur_name = tk.Label(info, text="—", font=self.f_body, bg=CARD, fg=TEXT,
                                 anchor="w")
        self.cur_name.pack(fill="x")
        self.cur_meta = tk.Label(info, text="", font=self.f_sub, bg=CARD, fg=SUB,
                                 anchor="w")
        self.cur_meta.pack(fill="x", pady=(2, 0))

        # 列表标题
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=16)
        self.list_title = tk.Label(bar, text="已保存账号", font=self.f_sub,
                                   bg=BG, fg=SUB)
        self.list_title.pack(side="left")
        refresh_lbl = tk.Label(bar, text="刷新", font=self.f_small, bg=BG, fg=BRAND,
                               padx=6, cursor="pointinghand")
        refresh_lbl.pack(side="right")
        refresh_lbl.bind("<Button-1>", lambda e: self.refresh())

        # 滚动列表
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._vbar_shown = False

        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.win_id = self.canvas.create_window((0, 0), window=self.list_frame,
                                                anchor="nw")
        self.list_frame.bind("<Configure>", self._on_frame_conf)
        self.canvas.bind("<Configure>", self._on_canvas_conf)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 底部操作
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=16, pady=(10, 12))
        self.btn_save = ttk.Button(foot, text="＋ 保存当前登录态",
                                   style="Brand.TButton", command=self.save_current)
        self.btn_save.pack(fill="x")

        self.status = tk.Label(self.root, text="就绪", font=self.f_small,
                               bg=BG, fg=SUB, anchor="w")
        self.status.pack(fill="x", padx=18, pady=(0, 10))

    def _yscroll(self, first, last):
        """内容放得下就隐藏滚动条"""
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self._vbar_shown:
            self.vbar.pack(side="right", fill="y")
            self._vbar_shown = True
        elif not need and self._vbar_shown:
            self.vbar.pack_forget()
            self._vbar_shown = False
        self.vbar.set(first, last)

    def _on_frame_conf(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_conf(self, e):
        self.canvas.itemconfigure(self.win_id, width=e.width)

    def _on_wheel(self, e):
        if self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-1 * (e.delta / 3)), "units")

    # ── 顶部开关 ────────────────────────────────────────────────────
    def _sync_pin(self):
        on = bool(self.topmost_var.get())
        self.pin.configure(text="📌 已置顶" if on else "置顶",
                           fg=BRAND if on else SUB)

    def toggle_topmost(self):
        v = not bool(self.topmost_var.get())
        self.topmost_var.set(v)
        try:
            self.root.attributes("-topmost", v)
        except tk.TclError:
            pass
        self._sync_pin()
        self.cfg["topmost"] = v
        save_guiconf(self.cfg)

    # ── 数据渲染 ────────────────────────────────────────────────────
    def refresh(self):
        try:
            manifest = sw.load_manifest()
            ident = sw.detect_identity()
        except Exception:
            self.set_status("读取状态失败", RED)
            return

        profiles = manifest.get("profiles", {})
        active = manifest.get("active")

        # 应用里正在跑的账号，和 active profile 记录的账号是否同一个
        act_ident = (profiles.get(active) or {}).get("identity") or {}
        cur_key = sw.identity_key(ident)
        act_key = sw.identity_key(act_ident)
        self.mismatch = bool(cur_key and act_key and cur_key != act_key)

        # 当前账号卡
        self.cur_avatar.delete("all")
        logging_in = bool(ident.get("logged_in"))
        who = ident.get("name") or ident.get("username") or "?"
        color = BRAND if logging_in else "#B9B9C9"
        self.cur_avatar.create_oval(2, 2, 42, 42, fill=color, outline="")
        self.cur_avatar.create_text(22, 22, text=initial(who if logging_in else "?"),
                                    fill="#FFFFFF", font=self.f_avatar)
        if logging_in:
            uname = ident.get("username")
            label = who if (not uname or uname == who) else f"{who} · {uname}"
            self.cur_name.configure(text=ellipsis(label, 22), fg=TEXT)
            bits = [ident.get("plan") or "—"]
            if ident.get("email") and not str(ident["email"]).endswith("@phone.local"):
                bits.append(ellipsis(ident["email"], 20))
            bits.append(f"profile：{active or '未保存'}")
            if self.mismatch:
                bits.append(f"⚠ 与 profile 内账号（{act_key}）不一致")
            self.cur_meta.configure(text=" · ".join(bits),
                                    fg=AMBER if self.mismatch else SUB)
        else:
            self.cur_name.configure(text="未登录", fg=SUB)
            self.cur_meta.configure(text="在千问办公里登录后，点下方按钮保存")

        # 列表
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows = []

        self.list_title.configure(
            text=f"已保存账号（{len(profiles)}）" if profiles else "已保存账号")

        if not profiles:
            empty = tk.Frame(self.list_frame, bg=CARD, highlightbackground=LINE,
                             highlightthickness=1)
            empty.pack(fill="x", pady=(0, 6))
            tk.Label(empty, text="还没有保存任何账号", font=self.f_body,
                     bg=CARD, fg=TEXT).pack(pady=(18, 4))
            tk.Label(empty,
                     text="① 在千问办公登录账号 A → 点「保存当前登录态」\n"
                          "② 换账号 B 登录 → 再保存一次\n③ 以后点一下就能互相切换",
                     font=self.f_small, bg=CARD, fg=SUB, justify="left").pack(
                pady=(0, 18), padx=16)
            return

        for idx, (name, meta) in enumerate(profiles.items()):
            self._build_row(idx, name, meta, name == active)

    def _build_row(self, idx, name, meta, is_active):
        ident = meta.get("identity") or {}
        row = tk.Frame(self.list_frame, bg=CARD, highlightbackground=LINE,
                       highlightthickness=1, cursor="pointinghand")
        row.pack(fill="x", pady=(0, 6))

        body = tk.Frame(row, bg=CARD)
        body.pack(fill="x", padx=12, pady=10)

        av = tk.Canvas(body, width=36, height=36, bg=CARD, highlightthickness=0)
        av.pack(side="left")
        label = ident.get("name") or ident.get("username") or name
        av.create_oval(1, 1, 35, 35, fill=BRAND if is_active else "#C7C7D6", outline="")
        av.create_text(18, 18, text=initial(label), fill="#FFFFFF",
                       font=self.f_avatar)

        # 右侧按钮先 pack，才能保证不被左边的可扩展文本挤掉
        more = flat_btn(body, "⋯", lambda: None, CARD, SUB, self.f_title,
                        padx=6, pady=0, hover="#EFEFF6")
        more.bind("<Button-1>", lambda e: self.row_menu(name, more, is_active))
        more.pack(side="right")

        if not is_active:
            switcher = flat_btn(body, "切换",
                                lambda: self.switch_to(name, is_active),
                                BRAND_SOFT, BRAND, self.f_sub,
                                padx=12, pady=3, hover="#E2DAFF")
            switcher.pack(side="right", padx=(0, 6))

        txt = tk.Frame(body, bg=CARD)
        txt.pack(side="left", fill="x", expand=True, padx=(10, 6))

        top = tk.Frame(txt, bg=CARD)
        top.pack(fill="x")
        nm = tk.Label(top, text=fit_px(self.f_body, name, 150), font=self.f_body,
                      bg=CARD, fg=TEXT, anchor="w")
        nm.pack(side="left")
        if is_active:
            tk.Label(top, text="使用中", font=self.f_small, bg=BRAND_SOFT,
                     fg=BRAND, padx=6, pady=1).pack(side="left", padx=(6, 0))

        who = ident.get("name") or "—"
        if ident.get("username") and ident["username"] != who:
            who = f"{who} / {ident['username']}"
        plan = ident.get("plan") or ""
        sub_bits = [who] + ([plan] if plan else [])
        sub_full = " · ".join(sub_bits)
        sub = tk.Label(txt, text=sub_full, font=self.f_sub,
                       bg=CARD, fg=SUB, anchor="w")
        sub.pack(fill="x", pady=(1, 0))
        self._auto_trim(sub, sub_full, self.f_sub)

        upd = (meta.get("updated_at") or meta.get("created_at") or "")[:16].replace("T", " ")
        upd = upd[5:] if len(upd) >= 16 else upd
        size = sw.human(meta.get("size", 0))
        tail_bits = []
        if meta.get("note"):
            tail_bits.append(fit_px(self.f_small, meta["note"], 90))
        tail_bits += [upd, size]
        foot_full = " · ".join(tail_bits)
        foot = tk.Label(txt, text=foot_full, font=self.f_small,
                        bg=CARD, fg="#B4B4C4", anchor="w")
        foot.pack(fill="x")
        self._auto_trim(foot, foot_full, self.f_small)

        for w in (row, body, txt, top, nm, sub, foot, av):
            w.bind("<Enter>", lambda e, ws=(row, body, txt, top): self._hover(ws, True))
            w.bind("<Leave>", lambda e, ws=(row, body, txt, top): self._hover(ws, False))
        body.bind("<Double-Button-1>", lambda e: self.switch_to(name, is_active))
        nm.bind("<Double-Button-1>", lambda e: self.switch_to(name, is_active))

        self.rows.append(row)

    def _auto_trim(self, lbl, full, font):
        """按控件真实宽度裁剪文本 —— 固定像素预算在不同字号下都不靠谱"""
        state = {"text": full}

        def on_conf(e):
            want = fit_px(font, full, max(24, e.width - 2))
            if want != state["text"]:
                state["text"] = want
                lbl.configure(text=want)

        lbl.bind("<Configure>", on_conf)

    def _hover(self, frames, on):
        color = CARD_HOVER if on else CARD
        for w in frames:
            try:
                if w.cget("bg") in (CARD, CARD_HOVER):
                    w.configure(bg=color)
            except tk.TclError:
                pass

    def row_menu(self, name, anchor, is_active):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="切换到该账号",
                      command=lambda: self.switch_to(name, is_active))
        m.add_separator()
        m.add_command(label="重命名…", command=lambda: self.rename(name))
        m.add_command(label="改备注…", command=lambda: self.set_note(name))
        m.add_separator()
        m.add_command(label="删除", command=lambda: self.delete(name))
        try:
            m.tk_popup(anchor.winfo_rootx(), anchor.winfo_rooty() + 24)
        finally:
            m.grab_release()

    # ── 任务调度 ────────────────────────────────────────────────────
    def set_status(self, text, color=SUB):
        self.status.configure(text=text, fg=color)

    def _set_busy(self, busy, msg=None):
        self.busy = busy
        st = ["disabled"] if busy else ["!disabled"]
        self.btn_save.state(st)
        if msg:
            self.set_status(msg, BRAND)

    def run_task(self, fn, label, done=None):
        """在后台线程跑 switcher 的命令，把 print 输出收进状态栏

        Tkinter 不是线程安全的：工作线程只把结果丢进队列，
        由主线程用 after() 轮询取结果，绝不在子线程里碰控件。
        """
        if self.busy:
            return
        self._set_busy(True, label)

        def worker():
            buf_out, buf_err = io.StringIO(), io.StringIO()
            err = None
            try:
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    fn()
            except SystemExit:
                pass
            except Exception:
                err = traceback.format_exc(limit=2)
            out = (buf_out.getvalue() + buf_err.getvalue()).strip()
            self._queue.put((err, out, done))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self):
        try:
            err, out, done = self._queue.get_nowait()
        except queue.Empty:
            if self.busy:
                self.root.after(80, self._poll)
            return
        self._finish(err, out, done)

    def _finish(self, err, out, done):
        self._set_busy(False)
        self.refresh()
        if err:
            self.set_status("操作失败，详见日志", RED)
            messagebox.showerror("出错了", err.splitlines()[-1] if err else "未知错误")
            return
        lines = [l for l in out.splitlines() if l.strip()]
        tail = lines[-1].lstrip("✓ℹ⚠ ").strip() if lines else "完成"
        self.set_status(tail, GREEN)
        if done:
            done()

    # ── 操作 ────────────────────────────────────────────────────────
    def save_current(self):
        manifest = sw.load_manifest()
        default = ""
        ident = sw.detect_identity()
        if ident.get("logged_in"):
            default = ident.get("name") or ident.get("username") or ""

        name = simpledialog.askstring(
            "保存当前登录态",
            "给这个账号起个名字（例如 work / home）：",
            initialvalue=default if default not in manifest["profiles"] else "",
            parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return

        force = False
        if name in manifest["profiles"]:
            if not messagebox.askyesno("已存在",
                                       f"'{name}' 已存在，覆盖它的登录态快照？",
                                       parent=self.root):
                return
            force = True

        note = simpledialog.askstring("备注（可留空）", "备注：", parent=self.root)
        args = type("A", (), {"name": name, "force": force,
                              "note": (note or "").strip(),
                              "no_restart": True})
        self.run_task(lambda: sw.cmd_save(args), "正在保存登录态 …",
                      done=lambda: self.set_status(f"已保存『{name}』", GREEN))

    def switch_to(self, name, is_active):
        if is_active:
            messagebox.showinfo("提示", f"'{name}' 就是当前使用的账号。", parent=self.root)
            return
        msg = f"切换到『{name}』？\n\n会先退出千问办公，替换登录态后再自动启动。"
        if getattr(self, "mismatch", False):
            msg += ("\n\n注意：当前登录的账号与 active profile 不一致，"
                    "切换时不会用当前登录态去覆盖任何 profile。")
        if not messagebox.askyesno("切换账号", msg, parent=self.root):
            return
        args = type("A", (), {"name": name, "no_restart": False})
        self.run_task(lambda: sw.cmd_switch(args), f"正在切换到 {name} …",
                      done=lambda: self.set_status(f"已切换到『{name}』", GREEN))

    def rename(self, name):
        new = simpledialog.askstring("重命名", "新名称：", initialvalue=name,
                                     parent=self.root)
        if not new or new.strip() == name:
            return
        args = type("A", (), {"name": name, "rename": new.strip(), "note": None})
        self.run_task(lambda: sw.cmd_edit(args), "正在重命名 …")

    def set_note(self, name):
        cur = (sw.load_manifest()["profiles"].get(name, {}) or {}).get("note", "")
        note = simpledialog.askstring("改备注", "备注：", initialvalue=cur,
                                      parent=self.root)
        if note is None:
            return
        args = type("A", (), {"name": name, "rename": None, "note": note.strip()})
        self.run_task(lambda: sw.cmd_edit(args), "正在更新备注 …")

    def delete(self, name):
        if not messagebox.askyesno("删除", f"删除 profile『{name}』？\n"
                                          "（只删本地快照，不影响线上账号）",
                                   parent=self.root):
            return
        args = type("A", (), {"name": name, "yes": True})
        self.run_task(lambda: sw.cmd_delete(args), "正在删除 …")

    def on_close(self):
        try:
            self.cfg["window"] = [self.root.winfo_width(), self.root.winfo_height()]
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_guiconf(self.cfg)
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
