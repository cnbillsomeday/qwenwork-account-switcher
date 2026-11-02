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
import os
import queue
import subprocess
import sys
import threading
import time
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


# ── 读窗口几何 + 判断前台（CoreGraphics，纯 ctypes，不需要任何系统权限）──
#
# ⚠️ 内存泄漏的坑（v1.1 修掉的那个 9GB）
#   CoreFoundation 的 "Copy/Create" 规则要求「谁 Copy/Create，谁 CFRelease」，
#   而 Python 的 GC 完全管不到 CF 对象。v1.0 每 400ms 调用一次
#   CGWindowListCopyWindowInfo() 却从不释放返回的 CFArray，还给**每个窗口**
#   现建 5 个 CFString 当字典 key；再加上 NSWorkspace 那一路用 objc_msgSend
#   拿到的自动释放对象没有 autoreleasepool 兜底 —— 实测 CG 那一路
#   13.7 KB/次、ObjC 那一路 1.1 KB/次，2.5 次/秒 ≈ 3 GB/天。
#   症状很迷惑：ps 看 RSS 只有 30MB，因为泄漏的页早被内存压缩器换出去了，
#   但 phys_footprint 会一路涨到 9GB，最终触发系统内存压力。
#
#   现在：dylib 只 CDLL 一次；字典 key 启动时预建、全程复用；每轮只产生
#   1 个 CFArray 且必定 CFRelease；「前台是不是千问办公」直接利用
#   CGWindowList 本身「从前往后排序」这个特性，彻底不碰 ObjC。
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")

_CF.CFRelease.argtypes = [ctypes.c_void_p]
_CF.CFRelease.restype = None
_CF.CFEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_CF.CFEqual.restype = ctypes.c_bool
_CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_CF.CFStringCreateWithCString.restype = ctypes.c_void_p
_CF.CFStringGetLength.argtypes = [ctypes.c_void_p]
_CF.CFStringGetLength.restype = ctypes.c_long
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
_CF.CFArrayGetCount.restype = ctypes.c_long
_CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
_CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
_CF.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_CF.CFDictionaryGetValue.restype = ctypes.c_void_p
_CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_CF.CFNumberGetValue.restype = ctypes.c_bool

_CG.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
_CG.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p

UTF8 = 0x08000100
_KEYS = {}
_OWNER_CF = {}
_NUMBUF = ctypes.c_double()


def _key(name):
    """字典 key 只建一次，之后一直复用（这些 CFString 常驻，永不释放）"""
    cf = _KEYS.get(name)
    if cf is None:
        cf = _CF.CFStringCreateWithCString(None, name.encode(), UTF8)
        _KEYS[name] = cf
    return cf


def _owner_key(name):
    cf = _OWNER_CF.get(name)
    if cf is None:
        cf = _CF.CFStringCreateWithCString(None, name.encode(), UTF8)
        _OWNER_CF[name] = cf
    return cf


def _cfnum(v):
    if not v:
        return None
    for t in (13, 6, 3, 9):
        if _CF.CFNumberGetValue(v, t, ctypes.byref(_NUMBUF)):
            return _NUMBUF.value
    return None


def cfstr(v):
    """CFString → Python str（只读，不持有引用）"""
    if not v:
        return ""
    n = _CF.CFStringGetLength(v) * 4 + 1
    buf = ctypes.create_string_buffer(n)
    if _CF.CFStringGetCString(v, buf, n, UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


class Screen:
    """一趟 CGWindowList 同时回答两件事：
         1. 千问办公最大那扇窗口在哪（贴按钮用）
         2. 最前面那个「正常 App 窗口」是不是千问办公（决定显不显示）
    """

    ON_SCREEN = 1 << 0        # kCGWindowListOptionOnScreenOnly
    NO_DESKTOP = 1 << 4       # kCGWindowListExcludeDesktopElements

    @staticmethod
    def snapshot(owner=APP_OWNER, self_pid=None):
        """返回 (rect|None, front_is_owner: bool|None)"""
        arr = _CG.CGWindowListCopyWindowInfo(
            Screen.ON_SCREEN | Screen.NO_DESKTOP, 0)
        if not arr:
            return None, None
        try:
            gets = _CF.CFDictionaryGetValue
            k_pid, k_layer = _key("kCGWindowOwnerPID"), _key("kCGWindowLayer")
            k_bounds, k_owner = _key("kCGWindowBounds"), _key("kCGWindowOwnerName")
            k_x, k_y = _key("X"), _key("Y")
            k_w, k_h = _key("Width"), _key("Height")
            want = _owner_key(owner)

            best = None          # (面积, rect)
            front = None         # 最前那个正常窗口属于千问办公吗
            for i in range(_CF.CFArrayGetCount(arr)):
                d = _CF.CFArrayGetValueAtIndex(arr, i)
                if not d:
                    continue
                if self_pid is not None and _cfnum(gets(d, k_pid)) == self_pid:
                    continue                      # 自己的悬浮钮 / 面板，跳过
                if _cfnum(gets(d, k_layer)) != 0:
                    continue                      # 菜单、Dock、通知条等非普通层
                bd = gets(d, k_bounds)
                if not bd:
                    continue
                x, y = _cfnum(gets(bd, k_x)), _cfnum(gets(bd, k_y))
                w, h = _cfnum(gets(bd, k_w)), _cfnum(gets(bd, k_h))
                if None in (x, y, w, h) or w < 60 or h < 40:
                    continue                      # 0×0 之类的隐形辅助窗口
                mine = bool(_CF.CFEqual(gets(d, k_owner), want))
                if front is None:
                    front = mine                  # 列表从前往后，取第一个即前台
                if mine:
                    area = w * h
                    if best is None or area > best[0]:
                        best = (area, (int(x), int(y), int(w), int(h)))
            return (best[1] if best else None), front
        finally:
            _CF.CFRelease(arr)                    # ← 关键：不释放就是那个 9GB


# ── 自身内存占用（ps 在部分环境下拿不到，libproc 一定能拿到）──────────
_PROC = None
_RI = None


def own_footprint():
    """返回本进程 phys_footprint（字节），失败返回 0。

    用 footprint 而不是 RSS：被内存压缩器换出去的泄漏页不计入 RSS，
    只看 RSS 会得出「才 30MB，很健康」的错误结论。
    """
    global _PROC, _RI
    try:
        if _RI is None:
            class _RusageInfo(ctypes.Structure):
                _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
                    (n, ctypes.c_uint64) for n in (
                        "ri_user_time ri_system_time ri_pkg_idle_wkups "
                        "ri_interrupt_wkups ri_pageins ri_wired_size "
                        "ri_resident_size ri_phys_footprint ri_proc_start_abstime "
                        "ri_proc_exit_abstime ri_child_user_time ri_child_system_time "
                        "ri_child_pkg_idle_wkups ri_child_interrupt_wkups "
                        "ri_child_pageins ri_child_elapsed_abstime "
                        "ri_diskio_bytesread ri_diskio_byteswritten").split()]
            _PROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            _PROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            _PROC.proc_pid_rusage.restype = ctypes.c_int
            _RI = _RusageInfo
        buf = _RI()
        if _PROC.proc_pid_rusage(os.getpid(), 2, ctypes.byref(buf)) != 0:
            return 0
        return buf.ri_phys_footprint
    except Exception:
        return 0


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
    TICK_MS = 400           # 跟手位置刷新（只读窗口几何，几乎不分配内存）
    STATE_MS = 2.5          # 账号/进程状态刷新（读文件 + pgrep，没必要 400ms 一次）
    TOP_MS = 2.0            # 重申置顶的间隔
    FOOTPRINT_LIMIT = 480 * 1024 * 1024   # 兜底自愈阈值

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

        self._pid = os.getpid()
        self._running = False
        self._ident = {}
        self._state_ts = 0.0
        self._top_ts = 0.0
        self._mem_ts = time.monotonic()

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
        self._refresh_state(force=True)
        self.paint()
        self.root.after(300, self.tick)

    # ── 状态 / 绘制 ──────────────────────────────────────────────────
    def _refresh_state(self, force=False):
        """账号身份 + 进程状态。这俩要读文件、要 pgrep，别每 400ms 来一次。"""
        now = time.monotonic()
        if not force and now - self._state_ts < self.STATE_MS:
            return
        self._state_ts = now
        self._running = sw.is_running()
        self._ident = sw.detect_identity()

    def paint(self):
        """只在内容真的变了才 configure —— 每帧重设 Tk 属性纯属白烧 CPU"""
        self._refresh_state()
        ident, running = self._ident, self._running
        if self.busy:
            text, fg, bg = "⇄ 切换中…", BRAND, BRAND_SOFT
        elif not running:
            text, fg, bg = "⇄ 千问办公未运行", SUB, CARD
        elif ident.get("logged_in"):
            who = ident.get("name") or ident.get("username") or "?"
            text, fg, bg = f"⇄ {who}", BRAND, CARD
        else:
            text, fg, bg = "⇄ 未登录", SUB, CARD
        if self.chip.cget("text") == text and self.chip.cget("bg") == bg:
            return
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
            self._follow()
            if self.visible:
                now = time.monotonic()
                if now - self._top_ts >= self.TOP_MS:
                    # macOS 偶尔会把置顶状态丢掉，隔两秒重申一次就够
                    self._top_ts = now
                    try:
                        self.root.attributes("-topmost", True)
                    except tk.TclError:
                        pass
                self.paint()
            self._mem_guard()
        except Exception:
            pass
        self.root.after(self.TICK_MS, self.tick)

    def _follow(self):
        """贴在千问办公窗口右下角；窗口没了/缩太小/不在前台就一起藏起来"""
        if not (self.cfg.get("follow", True) and not self.dragging):
            self.set_visible(True)                 # 手动摆放模式：始终显示
            return

        rect, front_is_owner = Screen.snapshot(self_pid=self._pid)
        dbg(self._dbg, rect, front_is_owner)

        if rect is None or rect[2] < 260 or rect[3] < 200:
            self.set_visible(False)                # 没窗口 / 缩太小
            return
        # 前台不是千问办公就藏起来。自己的按钮和账号面板按 pid 排除了，
        # 所以「用户正在点按钮」这种情形天然算作「前台是千问办公」。
        if self.cfg.get("front_only", True) and front_is_owner is not True:
            self.set_visible(False)
            return

        x, y, w, h = rect
        px = int(x + w - W - MARGIN)
        py = int(y + h - H - MARGIN - 12)
        if (px, py) != self.last_rect:
            self.move_to(px, py)      # 先把位置摆好（withdraw 状态下也能设）
        self.set_visible(True)        # 再显示，避免 deiconify 时在 (0,0) 闪一帧

    def _mem_guard(self):
        """兜底：真出泄漏时自愈重启，而不是默默吃到 9 GB。

        正常运行时 footprint 稳定在 40MB 上下，这里几乎不会被触发。
        """
        now = time.monotonic()
        if now - self._mem_ts < 30:
            return
        self._mem_ts = now
        fp = own_footprint()
        if fp and fp > self.FOOTPRINT_LIMIT:
            dbg(self._dbg, "footprint", fp, "→ 重启")
            self.restart()

    def restart(self):
        try:
            save_conf(self.cfg)
            self.root.destroy()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
        except Exception:
            os._exit(0)

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
            m.destroy()          # 菜单是一种 Tk 控件，不 destroy 就一次右键留一个

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
        self._refresh_state(force=True)     # 刚切完/刚存完，立刻重读，别等 2.5s
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
    root = tk.Tk()
    fab = Fab(root)
    if os.environ.get("FAB_DEBUG"):
        fab._dbg = "/tmp/fab-debug.log"
    if os.environ.get("FAB_OPEN_PANEL"):      # 仅用于自测截图
        root.after(1200, fab.open_panel)
    root.mainloop()


if __name__ == "__main__":
    main()
