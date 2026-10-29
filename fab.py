#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
千问办公 · 悬浮切换按钮（FAB）

一个常驻千问办公窗口右下角的圆形小按钮，点一下弹出账号列表，点账号直接切换。

为什么不是"注入到页面里"：
  千问办公屏蔽了 --remote-debugging-port（传参直接拒绝启动），而运行时调用
  app.commandLine.appendSwitch 对调试端口无效 —— CDP 注入这条路走不通。
  所以按钮是独立窗口，靠 CGWindowList 读取千问办公窗口位置，实时贴在它右下角。
  效果接近，但不需要改动应用本体。

依赖：Python 3.9+ / Tk 8.6+（与 run-gui.sh 同一套挑选逻辑）
"""

import ctypes
import io
import json
import queue
import subprocess
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont, messagebox, simpledialog

sys.path.insert(0, str(Path(__file__).resolve().parent))
import switcher as sw  # noqa: E402

# ── 外观 ─────────────────────────────────────────────────────────────
APP_OWNER = "千问办公"        # CGWindow 里的 owner 名（中文本地化名，不是进程名）
W, H = 118, 34
MARGIN = 18
BRAND = "#6B4EFF"
BRAND_DIM = "#9AA3C7"
IDLE = "#B9B9C9"
CARD = "#FFFFFF"
TEXT = "#1C1C28"
SUB = "#8B8B9E"
LINE = "#EAEAF2"
BRAND_SOFT = "#EEE9FF"
AMBER = "#C77700"
GREEN = "#0FA968"

GUICONF = sw.SWITCHER_DIR / "fab.json"


def load_conf():
    try:
        with GUICONF.open() as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"follow": True, "front_only": True, "offset": None}


def save_conf(cfg):
    try:
        GUICONF.parent.mkdir(parents=True, exist_ok=True)
        with GUICONF.open("w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def hide_dock_icon():
    """把 App 切成 Accessory 策略：不占 Dock、不出现在 ⌘⇥ 里，但窗口照常显示。

    比 Info.plist 里写 LSUIElement 更稳 —— 后者会让 Tk 窗口干脆不渲染。
    """
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        ns_app = objc.objc_msgSend(objc.objc_getClass(b"NSApplication"),
                                   objc.sel_registerName(b"sharedApplication"))
        if not ns_app:
            return False
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        objc.objc_msgSend(ns_app, objc.sel_registerName(b"setActivationPolicy:"), 1)
        return True
    except Exception:
        return False


def frontmost_app_name():
    """当前前台应用的名字（NSWorkspace.frontmostApplication.localizedName）"""
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        ws = objc.objc_msgSend(objc.objc_getClass(b"NSWorkspace"),
                               objc.sel_registerName(b"sharedWorkspace"))
        front = objc.objc_msgSend(ws, objc.sel_registerName(b"frontmostApplication"))
        if not front:
            return ""
        name = objc.objc_msgSend(front, objc.sel_registerName(b"localizedName"))
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        if not name:
            return ""
        n = cf.CFStringGetLength(name) * 4 + 1
        buf = ctypes.create_string_buffer(n)
        if cf.CFStringGetCString(name, buf, n, 0x08000100):
            return buf.value.decode("utf-8", "replace")
        return ""
    except Exception:
        return ""


# ── 读取千问办公窗口几何（CoreGraphics，纯 ctypes，无需权限）──────────
class CG:
    _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    _cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _cf.CFArrayGetCount.restype = ctypes.c_long
    _cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    _cf.CFStringGetLength.restype = ctypes.c_long
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    _cf.CFNumberGetValue.restype = ctypes.c_bool

    @classmethod
    def _str(cls, v):
        if not v:
            return ""
        n = cls._cf.CFStringGetLength(v) * 4 + 1
        buf = ctypes.create_string_buffer(n)
        if cls._cf.CFStringGetCString(v, buf, n, 0x08000100):
            return buf.value.decode("utf-8", "replace")
        return ""

    @classmethod
    def _num(cls, v):
        if not v:
            return None
        out = ctypes.c_double()
        for t in (13, 6, 3, 9):
            if cls._cf.CFNumberGetValue(v, t, ctypes.byref(out)):
                return out.value
        return None

    @classmethod
    def _cfs(cls, s):
        return cls._cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

    @classmethod
    def app_rect(cls, owner=APP_OWNER):
        """返回 (x, y, w, h)；找不到窗口返回 None"""
        arr = cls._cg.CGWindowListCopyWindowInfo(1, 0)   # 仅屏上窗口
        if not arr:
            return None
        best = None
        for i in range(cls._cf.CFArrayGetCount(arr)):
            d = cls._cf.CFArrayGetValueAtIndex(arr, i)
            if cls._str(cls._cf.CFDictionaryGetValue(d, cls._cfs("kCGWindowOwnerName"))) != owner:
                continue
            bd = cls._cf.CFDictionaryGetValue(d, cls._cfs("kCGWindowBounds"))
            if not bd:
                continue
            r = {k: cls._num(cls._cf.CFDictionaryGetValue(bd, cls._cfs(k)))
                 for k in ("X", "Y", "Width", "Height")}
            if None in r.values():
                continue
            area = r["Width"] * r["Height"]
            if best is None or area > best[0]:
                best = (area, r)
        if not best:
            return None
        r = best[1]
        return r["X"], r["Y"], r["Width"], r["Height"]


def dbg(path, *a):
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


def initial(name):
    for ch in (name or "?"):
        if ch.strip():
            return ch.upper()
    return "?"


class Fab:
    def __init__(self, root):
        self.root = root
        self.cfg = load_conf()
        self.panel = None
        self.busy = False
        self._queue = queue.Queue()
        self.dragging = False
        self.last_rect = None
        self._dbg = None
        self.visible = True
        self.no_dock = hide_dock_icon()

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=LINE)

        self.base = "PingFang SC"
        fams = set(tkfont.families())
        if self.base not in fams:
            self.base = "Helvetica Neue" if "Helvetica Neue" in fams else "Helvetica"
        self.f_small = tkfont.Font(family=self.base, size=11)
        self.f_body = tkfont.Font(family=self.base, size=12)
        self.f_mark = tkfont.Font(family=self.base, size=13, weight="bold")

        # 1px 边框感：外层 LINE 色 + 内层 CARD
        self.inner = tk.Frame(root, bg=CARD)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.chip = tk.Label(self.inner, text="⇄ 账号", font=self.f_body,
                             bg=CARD, fg=BRAND, padx=10, pady=0,
                             cursor="pointinghand")
        self.chip.pack(fill="both", expand=True)

        for w in (self.inner, self.chip):
            w.bind("<Button-1>", self.on_click)
            w.bind("<Button-2>", self.on_right)
            w.bind("<Button-3>", self.on_right)
            w.bind("<B1-Motion>", self.on_drag)
            w.bind("<ButtonRelease-1>", self.on_release)
            w.bind("<Enter>", lambda e: self.hover(True))
            w.bind("<Leave>", lambda e: self.hover(False))

        root.geometry(f"{W}x{H}+120+120")
        root.deiconify()
        root.update_idletasks()
        self.last_rect = None
        self.paint()
        self.root.after(300, self.tick)

    # ── 绘制 ─────────────────────────────────────────────────────────
    def paint(self):
        ident = sw.detect_identity()
        running = sw.is_running()
        if self.busy:
            text, fg, bg = "⇄ 切换中…", BRAND, BRAND_SOFT
        elif not running:
            text, fg, bg = "⇄ 千问办公未运行", SUB, CARD
        elif ident.get("logged_in"):
            who = ident.get("name") or ident.get("username") or "?"
            text, fg, bg = f"⇄ {who}", BRAND, CARD
        else:
            text, fg, bg = "⇄ 未登录", SUB, CARD
        self.chip.configure(text=text, fg=fg, bg=bg)
        self.inner.configure(bg=bg)
        self.root.configure(bg=LINE if not self.busy else BRAND)
        self.root.title(f"千问办公 · {text}")

    def hover(self, on):
        if self.busy:
            return
        bg = BRAND_SOFT if on else CARD
        self.chip.configure(bg=bg)
        self.inner.configure(bg=bg)

    # ── 跟随窗口 ─────────────────────────────────────────────────────
    def move_to(self, px, py):
        # 别让按钮被屏幕边缘切掉
        sw_ = self.root.winfo_screenwidth()
        sh_ = self.root.winfo_screenheight()
        px = max(8, min(px, sw_ - W - 8))
        py = max(8, min(py, sh_ - H - 8))
        self.root.geometry(f"{W}x{H}+{px}+{py}")
        self.root.update_idletasks()
        self.last_rect = (px, py)

    def screen_corner(self):
        sw_ = self.root.winfo_screenwidth()
        sh_ = self.root.winfo_screenheight()
        return sw_ - W - 24, sh_ - H - 96

    def set_visible(self, on):
        """窗口跟着千问办公显示/隐藏（最小化、⌘H、切到别的桌面都算不可见）"""
        if on == self.visible:
            return
        self.visible = on
        try:
            if on:
                self.root.deiconify()
                self.last_rect = None
            else:
                self.close_panel()
                self.root.withdraw()
        except tk.TclError:
            pass

    def tick(self):
        try:
            follow = self.cfg.get("follow", True) and not self.dragging

            if follow:
                rect = CG.app_rect()
                dbg(getattr(self, "_dbg", None), rect)
                too_small = bool(rect and (rect[2] < 260 or rect[3] < 200))
                # 前台是千问办公、或悬浮钮自己（用户正在点它）都算「该显示」
                front = frontmost_app_name()
                not_front = (self.cfg.get("front_only", True)
                             and front not in (APP_OWNER, "千问办公悬浮钮"))
                if rect is None or too_small or not_front:
                    self.set_visible(False)          # 没窗口 / 缩太小 / 不在前台 → 藏起来
                else:
                    x, y, w, h = rect
                    self.set_visible(True)
                    px = int(x + w - W - MARGIN)
                    py = int(y + h - H - MARGIN - 12)
                    if (px, py) != self.last_rect:
                        self.move_to(px, py)
            else:
                self.set_visible(True)               # 手动摆放模式：始终显示

            # macOS 有时会把置顶状态丢掉，每轮重申一次
            if self.visible:
                try:
                    self.root.attributes("-topmost", True)
                except tk.TclError:
                    pass
                self.paint()
        except Exception:
            pass
        self.root.after(400, self.tick)

    # ── 交互 ─────────────────────────────────────────────────────────
    def on_click(self, e):
        self._down = (e.x_root, e.y_root)
        self._moved = False

    def on_drag(self, e):
        if getattr(self, "_down", None) is None:
            return
        dx = e.x_root - self._down[0]
        dy = e.y_root - self._down[1]
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
            self.dragging = True
            x = self.root.winfo_x() + dx
            y = self.root.winfo_y() + dy
            self.root.geometry(f"{W}x{H}+{x}+{y}")
            self._down = (e.x_root, e.y_root)

    def on_release(self, e):
        down = getattr(self, "_down", None)
        self.dragging = False
        self._down = None
        if down and not self._moved:
            if self.panel and self.panel.winfo_exists():
                self.close_panel()
            else:
                self.open_panel()

    def on_right(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="保存当前登录态…", command=self.save_current)
        m.add_command(label="打开完整窗口", command=self.open_full)
        m.add_separator()
        follow = self.cfg.get("follow", True)
        m.add_command(label=("✓ 跟随窗口（最小化/隐藏时一起消失）" if follow
                             else "  跟随窗口（关闭后始终显示）"),
                      command=self.toggle_follow)
        front_only = self.cfg.get("front_only", True)
        m.add_command(label=("✓ 仅千问办公在前台时显示" if front_only
                             else "  仅千问办公在前台时显示"),
                      command=self.toggle_front_only)
        m.add_command(label="回到窗口右下角", command=self.snap_back)
        m.add_separator()
        m.add_command(label="退出悬浮按钮", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def toggle_front_only(self):
        self.cfg["front_only"] = not self.cfg.get("front_only", True)
        save_conf(self.cfg)
        if not self.cfg["front_only"]:
            self.last_rect = None

    def toggle_follow(self):
        self.cfg["follow"] = not self.cfg.get("follow", True)
        save_conf(self.cfg)
        if self.cfg["follow"]:
            self.last_rect = None

    def snap_back(self):
        self.cfg["follow"] = True
        save_conf(self.cfg)
        self.last_rect = None

    def quit(self):
        save_conf(self.cfg)
        self.root.destroy()

    def open_full(self):
        here = Path(__file__).resolve().parent
        launcher = here / "run-gui.sh"
        try:
            if launcher.exists():
                subprocess.Popen(["/bin/bash", str(launcher)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([sys.executable, str(here / "gui.py")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # ── 账号面板 ─────────────────────────────────────────────────────
    def _raise_panel(self):
        """Toplevel 的 -topmost 在 override-redirect 父窗口下不可靠，定时抬一次"""
        if self.panel and self.panel.winfo_exists():
            try:
                self.panel.lift()
                self.panel.attributes("-topmost", True)
            except tk.TclError:
                return
            self.panel.after(600, self._raise_panel)

    def close_panel(self):
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
        self.panel = None

    def _profile_row(self, parent, name, meta, is_active):
        """一行账号。要点：Tk 事件不冒泡，所以行内每个控件都得单独绑点击，
        否则用户点在文字上就没反应（这就是上一版「点了没反应」的原因）。"""
        ident = meta.get("identity") or {}
        who = ident.get("name") or ident.get("username") or "?"
        plan = ident.get("plan") or ""

        row = tk.Frame(parent, bg=CARD, cursor="pointinghand")
        row.pack(fill="x", padx=4, pady=2)

        # 右侧动作：当前账号=灰色状态签，其它=可点的「切换」
        if is_active:
            chip = tk.Label(row, text="使用中", font=self.f_small, bg="#F1F1F6",
                            fg="#B9B9C9", padx=8, pady=3)
        else:
            chip = tk.Label(row, text="切换", font=self.f_small, bg=BRAND,
                            fg="#FFFFFF", padx=10, pady=3, cursor="pointinghand")
        chip.pack(side="right", padx=(6, 4))

        av = tk.Canvas(row, width=30, height=30, bg=CARD, highlightthickness=0)
        av.pack(side="left", pady=6)
        av.create_oval(1, 1, 29, 29, fill=BRAND if is_active else "#C7C7D6", outline="")
        av.create_text(15, 15, text=initial(who), fill="#FFFFFF", font=self.f_small)

        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", fill="x", expand=True, padx=(8, 0))
        nm = tk.Label(txt, text=name, font=self.f_body, bg=CARD, fg=TEXT, anchor="w")
        nm.pack(fill="x")
        sub = tk.Label(txt, text=(who + (" · " + plan if plan else "")),
                       font=self.f_small, bg=CARD, fg=SUB, anchor="w")
        sub.pack(fill="x")

        def set_bg(color):
            for w in (row, txt, nm, sub, av):
                w.configure(bg=color)

        def enter(_e=None):
            if is_active:
                return
            set_bg(BRAND_SOFT)
            chip.configure(bg="#5A3EE8")

        def leave(_e=None):
            set_bg(CARD)
            if not is_active:
                chip.configure(bg=BRAND)

        def pick(_e=None):
            self.switch_to(name, is_active)

        for w in (row, txt, nm, sub, av, chip):
            w.bind("<Button-1>", pick)
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
        return row

    def open_panel(self):
        if self.busy:
            return
        manifest = sw.load_manifest()
        profiles = manifest.get("profiles", {})
        active = manifest.get("active")
        ident = sw.detect_identity()
        act_ident = (profiles.get(active) or {}).get("identity") or {}
        mismatch = bool(sw.identity_key(ident) and sw.identity_key(act_ident)
                        and sw.identity_key(ident) != sw.identity_key(act_ident))

        p = tk.Toplevel(self.root)
        self.panel = p
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=CARD, highlightbackground=LINE, highlightthickness=1)

        # 头部
        head = tk.Frame(p, bg=CARD)
        head.pack(fill="x", padx=14, pady=(12, 8))
        who = (ident.get("name") or ident.get("username") or "未登录") if ident.get("logged_in") else "未登录"
        tk.Label(head, text=f"当前：{who}", font=self.f_body, bg=CARD,
                 fg=TEXT, anchor="w").pack(fill="x")
        if mismatch:
            tk.Label(head, text=f"⚠ 与 profile『{active}』记录不一致",
                     font=self.f_small, bg=CARD, fg=AMBER, anchor="w").pack(fill="x")
        tk.Frame(p, bg=LINE, height=1).pack(fill="x")

        # 账号行
        body = tk.Frame(p, bg=CARD)
        body.pack(fill="both", padx=6, pady=6)
        tk.Frame(body, bg=CARD, width=248, height=1).pack()   # 撑出稳定宽度
        if not profiles:
            tk.Label(body, text="还没保存账号\n右键 → 保存当前登录态",
                     font=self.f_small, bg=CARD, fg=SUB, justify="left").pack(
                padx=10, pady=16, anchor="w")
        for name, meta in profiles.items():
            self._profile_row(body, name, meta, name == active)

        # 底部
        tk.Frame(p, bg=LINE, height=1).pack(fill="x")
        foot = tk.Frame(p, bg=CARD)
        foot.pack(fill="x", padx=10, pady=8)
        tk.Label(foot, text="＋ 保存当前登录态", font=self.f_small, bg=BRAND_SOFT,
                 fg=BRAND, padx=10, pady=6, cursor="pointinghand").pack(fill="x")
        foot.winfo_children()[0].bind("<Button-1>", lambda e: (self.close_panel(), self.save_current()))
        tk.Label(p, text="点账号行或「切换」即可切换", font=self.f_small,
                 bg=CARD, fg="#B4B4C4").pack(pady=(0, 8))

        p.update_idletasks()
        pw, ph = p.winfo_width(), p.winfo_height()
        fx, fy = self.root.winfo_x(), self.root.winfo_y()
        px = max(8, fx + W - pw)
        py = fy - ph - 10
        if py < 8:
            py = fy + H + 10
        p.geometry(f"+{px}+{py}")
        p.bind("<Escape>", lambda e: self.close_panel())
        p.bind("<FocusOut>", lambda e: self.close_panel())
        p.lift()
        p.focus_force()
        self._raise_panel()

    # ── 操作（与 gui.py 一样：子线程跑，队列回传）────────────────────
    def run_task(self, fn, label=None):
        if self.busy:
            return
        self.busy = True
        self.paint()

        def worker():
            buf_o, buf_e = io.StringIO(), io.StringIO()
            err = None
            try:
                with redirect_stdout(buf_o), redirect_stderr(buf_e):
                    fn()
            except SystemExit:
                pass
            except Exception:
                err = traceback.format_exc(limit=2)
            self._queue.put((err, (buf_o.getvalue() + buf_e.getvalue()).strip()))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self):
        try:
            err, out = self._queue.get_nowait()
        except queue.Empty:
            if self.busy:
                self.root.after(100, self._poll)
            return
        self.busy = False
        self.paint()
        if err:
            messagebox.showerror("切换失败", err.splitlines()[-1])
        else:
            lines = [l for l in out.splitlines() if l.strip()]
            tail = lines[-1].lstrip("✓ℹ⚠ ").strip() if lines else "完成"
            self.toast(tail)

    def toast(self, text):
        self.root.title(f"千问办公 · {text}")

    def switch_to(self, name, is_active):
        self.close_panel()
        if is_active:
            self.toast(f"『{name}』已是当前账号")
            return
        self.run_task(lambda: sw.cmd_switch(type("A", (), {"name": name,
                                                           "no_restart": False})))

    def save_current(self):
        manifest = sw.load_manifest()
        ident = sw.detect_identity()
        default = (ident.get("name") or ident.get("username") or "") if ident.get("logged_in") else ""
        name = simpledialog.askstring("保存当前登录态", "账号名称：",
                                      initialvalue=default if default not in manifest["profiles"] else "")
        if not name or not name.strip():
            return
        name = name.strip()
        force = False
        if name in manifest["profiles"]:
            if not messagebox.askyesno("已存在", f"覆盖『{name}』的登录态快照？"):
                return
            force = True
        self.run_task(lambda: sw.cmd_save(type("A", (), {"name": name, "force": force,
                                                         "note": "", "no_restart": True})))


def main():
    import os
    root = tk.Tk()
    fab = Fab(root)
    if os.environ.get("FAB_DEBUG"):
        fab._dbg = "/tmp/fab-debug.log"
    if os.environ.get("FAB_OPEN_PANEL"):      # 仅用于自测截图
        root.after(1200, fab.open_panel)
    root.mainloop()


if __name__ == "__main__":
    main()
