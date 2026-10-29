#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
千问办公（QwenWorkCN）账户一键切换工具

原理：备份 / 恢复两个目录中的「登录态数据集」，实现多账号一键切换。

  A. ~/.qwenworkcn                                  （SDK 侧：凭证 / 身份数据 / MaaS key）
  B. ~/Library/Application Support/QwenWorkCN       （Electron 侧：加密 token / Cookies / webview 会话）

与 Trae CN 版一致：切换前自动退出应用，切换后自动拉起，切换瞬间把当前登录态
回写到它自己的 profile，保证「切过去再切回来」不会丢登录态。

纯标准库实现，无第三方依赖。兼容 macOS 系统自带 Python 3.9。
"""

import argparse
import errno
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────
HOME = Path.home()

APP_NAME = "QwenWorkCN"                       # CFBundleName / 进程名
APP_BUNDLE_ID = "cn.qwenwork.desktop.mac"
APP_CANDIDATES = [
    Path("/Applications/Agent/QwenWorkCN.app"),
    Path("/Applications/QwenWorkCN.app"),
    HOME / "Applications" / "QwenWorkCN.app",
]

SDK_DIR = HOME / ".qwenworkcn"                                    # A
APP_SUPPORT_DIR = HOME / "Library" / "Application Support" / "QwenWorkCN"   # B

SWITCHER_DIR = HOME / ".qwen-work-switcher"
PROFILES_DIR = SWITCHER_DIR / "profiles"
MANIFEST_PATH = SWITCHER_DIR / "manifest.json"
LOCK_PATH = SWITCHER_DIR / ".switch.lock"
LOG_PATH = SWITCHER_DIR / "switcher.log"
TMP_BACKUP = SWITCHER_DIR / ".current-backup"
TRASH_DIR = SWITCHER_DIR / "trash"          # 被替换/被跳过时保留下来的旧数据

# 属于登录态、需要备份/恢复的数据集（相对各自根目录）
#
# 刻意不包含：
#   logs / Cache / Code Cache / Crashpad / diagnostics / tmp
#      → 缓存与日志，和账号无关，体积大
#   machine-id / installation_id / umid-cache.json / .models / bin / skills / commands
#      → 设备级标识与共享资源，切换账号不应改变，否则会触发风控或重新下载
#   data/agents.identity-v2.db
#      → 会话与智能体工作数据，切换账号时保留，避免丢历史
AUTH_DATASETS_SDK = [
    ".status.json",           # 当前账号快照（纯文本，用于身份识别）
    ".auth",                  # 凭证事务 / machine_id
    "identity-data",          # 身份维度的数据根
    "qwapp-identities",       # 应用侧身份数据（host-content.db 等）
    "engine-maas-key.dat",    # 模型网关密钥（随账号）
]

AUTH_DATASETS_APP = [
    "auth.dat",               # safeStorage 加密的登录凭证
    "auth-v2.dat",            # v2 版凭证
    "Cookies",
    "Cookies-journal",
    "Local Storage",
    "Session Storage",
    "Partitions",             # webview 分区（真正承载网页登录态的 Cookies）
    "identity-claim",         # 身份认领状态（awaiting_login → claimed）
    "identities",             # 身份维度的连接器配置
]

# 无论备份还原都必须自动重建的子目录（应用启动时会自己创建，这里只是兜底）
AUTO_MKDIRS_SDK = ["identity-data/v1", "qwapp-identities"]


# ── 颜色输出 ──────────────────────────────────────────────────────────
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"

    @staticmethod
    def ok(msg):
        print(f"{C.GREEN}✓{C.RESET} {msg}")

    @staticmethod
    def err(msg):
        print(f"{C.RED}✗{C.RESET} {msg}", file=sys.stderr)

    @staticmethod
    def warn(msg):
        print(f"{C.YELLOW}⚠{C.RESET} {msg}")

    @staticmethod
    def info(msg):
        print(f"{C.CYAN}ℹ{C.RESET} {msg}")

    @staticmethod
    def dim(msg):
        return f"{C.DIM}{msg}{C.RESET}"


# ── 日志 ─────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def die(msg, code=1):
    C.err(msg)
    log(f"ERROR: {msg}")
    sys.exit(code)


# ── 文件锁（防止并发切换互相踩踏）────────────────────────────────────
class SwitchLock:
    def __init__(self):
        self.fh = None

    def __enter__(self):
        SWITCHER_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = LOCK_PATH.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                die("另一个切换进程正在运行，请稍后再试。")
            raise
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except OSError:
                pass
        return False


# ── 应用进程控制 ─────────────────────────────────────────────────────
def find_app_bundle():
    for p in APP_CANDIDATES:
        if p.exists():
            return p
    return None


def is_running():
    """进程名精确匹配，避免误杀/误判（helper 进程不算）"""
    for name in (APP_NAME, "qwenworkhelper"):
        if subprocess.run(["pgrep", "-x", name],
                          stdout=subprocess.DEVNULL).returncode == 0:
            return True
    return False


def quit_app(timeout=20):
    """优雅退出；返回 True 表示已完全退出

    osascript 在首次调用时可能弹出「控制其他 App」的自动化授权框，
    一旦用户没点，脚本会一直挂着 —— 所以给它 5 秒硬超时，超时直接转信号退出。
    """
    if not is_running():
        return True
    C.info(f"正在退出 {APP_NAME} …")

    requested = False
    try:
        r = subprocess.run(
            ["osascript", "-e", f'tell application "{APP_NAME}" to quit'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
        requested = r.returncode == 0
    except subprocess.TimeoutExpired:
        C.warn("AppleScript 退出超时（可能卡在自动化授权弹窗），改用信号退出。")
    except OSError:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.4)

    # 优雅退出失败 → SIGTERM
    if not requested or is_running():
        for name in (APP_NAME, "qwenworkhelper"):
            subprocess.run(["pkill", "-TERM", "-x", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.4)

    # 最后 SIGKILL
    for name in (APP_NAME, "qwenworkhelper"):
        subprocess.run(["pkill", "-KILL", "-x", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    return not is_running()


def force_kill():
    for name in (APP_NAME, "qwenworkhelper"):
        subprocess.run(["pkill", "-9", "-x", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def ensure_app_stopped():
    if quit_app():
        return
    C.warn("优雅退出超时，尝试强制结束 …")
    force_kill()
    if is_running():
        die(f"无法停止 {APP_NAME}，请手动退出后重试。")


def relaunch_app():
    bundle = find_app_bundle()
    if not bundle:
        C.warn("未找到千问办公 App，跳过自动启动。")
        return
    C.info("正在重新启动千问办公 …")
    subprocess.Popen(["open", "-a", str(bundle)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Manifest ─────────────────────────────────────────────────────────
def load_manifest():
    empty = {"version": 1, "active": None, "profiles": {}}
    if not MANIFEST_PATH.exists():
        return empty
    try:
        with MANIFEST_PATH.open() as f:
            data = json.load(f)
        data.setdefault("version", 1)
        data.setdefault("active", None)
        data.setdefault("profiles", {})
        return data
    except (OSError, ValueError):
        return empty


def save_manifest(manifest):
    SWITCHER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    tmp.replace(MANIFEST_PATH)


# ── 身份识别（千问办公把当前账号明文写进 .status.json）────────────────
def detect_identity():
    """
    读取 ~/.qwenworkcn/.status.json：
      {"logged_in": true, "username": "4020pss", "name": "4020",
       "email": "...", "plan": "个人免费版", ...}
    """
    info = {
        "logged_in": False,
        "username": None,
        "name": None,
        "email": None,
        "plan": None,
        "user_type": None,
    }
    status = SDK_DIR / ".status.json"
    if not status.exists():
        return info
    try:
        with status.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return info
    info["logged_in"] = bool(data.get("logged_in"))
    info["username"] = data.get("username") or None
    info["name"] = data.get("name") or None
    info["email"] = data.get("email") or None
    info["plan"] = data.get("plan") or None
    info["user_type"] = data.get("user_type") or None
    return info


def identity_label(info):
    if not info or not info.get("logged_in"):
        return "(未登录)"
    who = info.get("name") or info.get("username") or "?"
    if info.get("username") and info.get("username") != who:
        who = f"{who}/{info['username']}"
    email = info.get("email") or ""
    if email.endswith("@phone.local"):
        email = ""                      # 手机号登录的占位邮箱，没信息量
    tail = f" {email}" if email else ""
    plan = f" [{info['plan']}]" if info.get("plan") else ""
    return f"{who}{tail}{plan}"


def identity_key(info):
    """用于判断「是不是同一个账号」的指纹"""
    if not info or not info.get("logged_in"):
        return None
    return info.get("username") or info.get("email") or info.get("name")


# ── 数据集复制 / 还原 ────────────────────────────────────────────────
def _remove(path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _copy(src, dst):
    """复制文件或目录；目标已存在时先清掉，避免残留旧数据"""
    if not src.exists() and not src.is_symlink():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    _remove(dst)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True,
                        ignore=shutil.ignore_patterns(".DS_Store"))
    else:
        shutil.copy2(src, dst)
    return True


def backup_datasets(dest):
    """把当前登录态复制到 dest（dest 下分 sdk/ 与 app-support/ 两棵子树）"""
    dest = Path(dest)
    _remove(dest)
    dest.mkdir(parents=True, exist_ok=True)
    count = 0

    for rel in AUTH_DATASETS_SDK:
        src = SDK_DIR / rel
        if _copy(src, dest / "sdk" / rel):
            count += 1

    for rel in AUTH_DATASETS_APP:
        src = APP_SUPPORT_DIR / rel
        if _copy(src, dest / "app-support" / rel):
            count += 1

    meta = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "identity": detect_identity(),
        "items": count,
    }
    with (dest / "profile-meta.json").open("w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return count


def restore_datasets(src):
    """把 src 中的登录态写回应用目录；src 里缺失的条目会从应用目录中清除"""
    src = Path(src)
    if not src.exists():
        die(f"profile 数据目录不存在：{src}")

    for rel in AUTH_DATASETS_SDK:
        s, d = src / "sdk" / rel, SDK_DIR / rel
        if s.exists() or s.is_symlink():
            _copy(s, d)
        else:
            _remove(d)

    for rel in AUTH_DATASETS_APP:
        s, d = src / "app-support" / rel, APP_SUPPORT_DIR / rel
        if s.exists() or s.is_symlink():
            _copy(s, d)
        else:
            _remove(d)

    for rel in AUTO_MKDIRS_SDK:
        (SDK_DIR / rel).mkdir(parents=True, exist_ok=True)
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


def dir_size(path):
    total = 0
    path = Path(path)
    if not path.exists():
        return 0
    if not path.is_dir():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


# ── 备份当前 → 指定 profile 目录（供 save / switch 复用）──────────────
def snapshot_into(profile_dir, extra_meta=None):
    count = backup_datasets(profile_dir)
    ident = detect_identity()
    with (profile_dir / "profile-meta.json").open() as f:
        meta = json.load(f)
    if extra_meta:
        meta.update(extra_meta)
    meta["size"] = dir_size(profile_dir)
    meta["identity"] = ident
    with (profile_dir / "profile-meta.json").open("w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return count, ident, meta


# ── 子命令 ──────────────────────────────────────────────────────────
def cmd_save(args):
    with SwitchLock():
        ensure_app_stopped()
        name = args.name.strip()
        if not name or "/" in name or name.startswith("."):
            die("profile 名称非法（不能为空、含 / 或以 . 开头）")
        profile_dir = PROFILES_DIR / name
        if profile_dir.exists() and not args.force:
            die(f"Profile '{name}' 已存在。覆盖请加 --force")

        C.info(f"正在保存当前登录态 → profile '{name}' …")
        count, ident, meta = snapshot_into(profile_dir)

        manifest = load_manifest()

        # 同一账号、同样体积的已有 profile → 提示重复
        dup = None
        for other, om in manifest["profiles"].items():
            if other == name:
                continue
            oi = om.get("identity") or {}
            if (identity_key(oi) and identity_key(oi) == identity_key(ident)
                    and (om.get("size") or 0) == meta["size"]):
                dup = other
                break

        manifest["profiles"][name] = {
            "created_at": manifest["profiles"].get(name, {}).get(
                "created_at", datetime.now().isoformat(timespec="seconds")),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "note": args.note or manifest["profiles"].get(name, {}).get("note", ""),
            "identity": ident,
            "size": meta["size"],
            "path": str(profile_dir),
        }
        # 刚保存的就是应用里正在跑的登录态 → active 必须跟着挪过来，
        # 否则界面会把"使用中"标在别的 profile 上，误导用户再去点一次切换。
        manifest["active"] = name
        save_manifest(manifest)

        log(f"SAVE profile={name} identity={identity_key(ident)} items={count}")
        C.ok(f"已保存 profile '{name}'")
        print(f"  账号：{identity_label(ident)}")
        print(f"  条目：{count} 项 · {human(meta['size'])}")
        if dup:
            C.warn(f"注意：这份登录态与已有 profile '{dup}' 完全相同（同一账号）")
        if not getattr(args, "no_restart", False):
            relaunch_app()


def cmd_switch(args):
    name = args.name
    manifest = load_manifest()
    if name not in manifest["profiles"]:
        avail = ", ".join(manifest["profiles"].keys()) or "(无)"
        die(f"Profile '{name}' 不存在。已有：{avail}")
    active = manifest.get("active")

    with SwitchLock():
        # 先读一次「即将被覆盖」的账号，用于回写到它自己的 profile
        pre_ident = detect_identity()
        if name == active:
            C.warn(f"'{name}' 已是当前 profile，将用它覆盖当前登录态。")
        ensure_app_stopped()

        # 1. 当前真实登录态 → 临时快照
        C.info("正在快照当前登录态 …")
        snapshot_into(TMP_BACKUP)

        # 2. 把临时快照回写到「当前所属 profile」，保证切回来还在
        #
        #    但前提是：应用里此刻跑的账号，必须还是该 profile 记录的那个账号。
        #    用户如果在千问办公里登出换了号、却没保存，此刻的登录态属于新账号，
        #    再回写就会把旧 profile 的旧账号快照冲掉（曾因此丢过一份快照）。
        if active and active != name and (PROFILES_DIR / active).exists():
            old_meta = manifest["profiles"].get(active, {})
            old_key = identity_key(old_meta.get("identity") or {})
            cur_key = identity_key(pre_ident)
            # 只有「两边都认得出、且是同一个账号」才允许回写；
            # 任一侧识别不出来（老版本 profile / 未登录）一律不动，宁可少同步也不覆盖。
            if old_key and cur_key and old_key == cur_key:
                C.info(f"正在把当前登录态回写到 profile '{active}' …")
                _remove(PROFILES_DIR / active)
                shutil.copytree(TMP_BACKUP, PROFILES_DIR / active, symlinks=True)
                meta = manifest["profiles"].get(active, {})
                meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
                meta["identity"] = pre_ident
                manifest["profiles"][active] = meta
                log(f"REFRESH profile={active} identity={identity_key(pre_ident)}")
            else:
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                rescue = TRASH_DIR / f"{active}-切换前登录态-{ts}"
                _remove(rescue)
                shutil.copytree(TMP_BACKUP, rescue, symlinks=True)
                why = ("账号不一致" if (old_key and cur_key)
                       else "有一侧认不出账号")
                C.warn(
                    f"{why}：当前={cur_key or '未知'}，profile '{active}'={old_key or '未知'}；"
                    f"已跳过回写，保住 '{active}' 的旧快照"
                )
                print(f"  （本次登录态另存于 {rescue}）")
                log(f"SKIP-REFRESH profile={active} cur={cur_key} old={old_key} rescue={rescue}")

        # 3. 应用目标 profile
        C.info(f"正在切换到 profile '{name}' …")
        restore_datasets(PROFILES_DIR / name)

        # 4. 收尾
        _remove(TMP_BACKUP)
        manifest["active"] = name
        save_manifest(manifest)

        post_ident = detect_identity()
        log(f"SWITCH to={name} from={active} identity={identity_key(post_ident)}")
        C.ok(f"已切换到 profile '{name}'")
        print(f"  账号：{identity_label(post_ident)}")
        if not args.no_restart:
            relaunch_app()


def cmd_list(args):
    manifest = load_manifest()
    profiles = manifest["profiles"]
    active = manifest.get("active")
    cur = detect_identity()
    cur_key = identity_key(cur)

    if not profiles:
        print(f"\n{C.YELLOW}尚未保存任何 profile。{C.RESET}")
        print(f"  先在千问办公里登录，然后运行：")
        print(f"    {Path(sys.argv[0]).name} save <名称>\n")
        return

    print(f"\n{C.BOLD}千问办公 · 已保存账号{C.RESET}  当前 active："
          f"{C.GREEN}{active or '无'}{C.RESET}")
    print(f"  当前登录：{identity_label(cur)}\n")
    print(f"{'':2}{'名称':<16}{'账号':<26}{'套餐':<12}{'大小':>8}  更新时间")
    print("─" * 84)
    for name, meta in profiles.items():
        ident = meta.get("identity") or {}
        mark = f"{C.GREEN}●{C.RESET} " if name == active else "  "
        who = (ident.get("name") or ident.get("username") or "—")
        if ident.get("username") and ident["username"] != who:
            who = f"{who}/{ident['username']}"
        same = ""
        if cur_key and identity_key(ident) == cur_key:
            same = f" {C.GREEN}←当前{C.RESET}"
        plan = ident.get("plan") or "—"
        size = human(meta.get("size", 0))
        upd = (meta.get("updated_at") or meta.get("created_at") or "—")[:19].replace("T", " ")
        print(f"{mark}{name:<16}{who:<26}{plan:<12}{size:>8}  {C.dim(upd)}{same}")
    print()


def cmd_current(args):
    ident = detect_identity()
    manifest = load_manifest()
    active = manifest.get("active")
    bundle = find_app_bundle()

    print(f"\n{C.BOLD}千问办公 · 当前状态{C.RESET}")
    print(f"  登录状态：{'已登录' if ident.get('logged_in') else '未登录'}")
    print(f"  账号    ：{identity_label(ident)}")
    print(f"  用户名  ：{ident.get('username') or '—'}")
    print(f"  邮箱    ：{ident.get('email') or '—'}")
    print(f"  套餐    ：{ident.get('plan') or '—'}")
    print(f"  关联profile：{C.GREEN}{active or '(未保存)'}{C.RESET}")
    print(f"  应用路径：{bundle or '未找到'}")
    print(f"  运行状态：{'运行中' if is_running() else '未运行'}\n")


def cmd_delete(args):
    name = args.name
    manifest = load_manifest()
    if name not in manifest["profiles"]:
        die(f"Profile '{name}' 不存在。")
    if not args.yes:
        try:
            ans = input(f"确定删除 profile '{name}'？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans != "y":
            C.info("已取消。")
            return
    _remove(PROFILES_DIR / name)
    del manifest["profiles"][name]
    if manifest.get("active") == name:
        manifest["active"] = None
    save_manifest(manifest)
    log(f"DELETE profile={name}")
    C.ok(f"已删除 profile '{name}'")


def cmd_edit(args):
    name = args.name
    manifest = load_manifest()
    if name not in manifest["profiles"]:
        die(f"Profile '{name}' 不存在。")

    if args.rename:
        new = args.rename.strip()
        if new in manifest["profiles"]:
            die(f"Profile '{new}' 已存在。")
        if (PROFILES_DIR / name).exists():
            os.rename(PROFILES_DIR / name, PROFILES_DIR / new)
        manifest["profiles"][new] = manifest["profiles"].pop(name)
        manifest["profiles"][new]["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if manifest.get("active") == name:
            manifest["active"] = new
        C.ok(f"已重命名 '{name}' → '{new}'")
        name = new

    if args.note is not None:
        manifest["profiles"][name]["note"] = args.note
        manifest["profiles"][name]["updated_at"] = datetime.now().isoformat(timespec="seconds")
        C.ok(f"已更新 '{name}' 的备注：{args.note}")

    save_manifest(manifest)


def cmd_backup(args):
    with SwitchLock():
        ensure_app_stopped()
        C.info("正在备份当前登录态 → .current-backup …")
        count, ident, meta = snapshot_into(TMP_BACKUP)
        log(f"MANUAL-BACKUP identity={identity_key(ident)} items={count}")
        C.ok(f"已备份（{count} 项 · {human(meta['size'])}）")
        print(f"  账号：{identity_label(ident)}")
        relaunch_app()


def cmd_restore(args):
    if not TMP_BACKUP.exists():
        die("没有找到 .current-backup，无法恢复。")
    with SwitchLock():
        ensure_app_stopped()
        C.info("正在从 .current-backup 恢复 …")
        restore_datasets(TMP_BACKUP)
        ident = detect_identity()
        log(f"MANUAL-RESTORE identity={identity_key(ident)}")
        C.ok("已恢复登录态")
        print(f"  账号：{identity_label(ident)}")
        relaunch_app()


def cmd_gui(args):
    """打开图形界面（复用同目录 run-gui.sh，自动挑选 Tk 版本够新的 Python）"""
    here = Path(__file__).resolve().parent
    launcher = here / "run-gui.sh"
    if launcher.exists():
        subprocess.Popen(["/bin/bash", str(launcher)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        C.ok("已启动图形界面")
        return
    gui_py = here / "gui.py"
    if not gui_py.exists():
        die("未找到 gui.py / run-gui.sh，无法启动图形界面。")
    r = subprocess.run([sys.executable, "-c",
                        "import tkinter;print(tkinter.TkVersion)"],
                       capture_output=True, text=True)
    ver = r.stdout.strip()
    try:
        ok = float(ver) >= 8.6
    except ValueError:
        ok = False
    if not ok:
        C.warn(f"当前 Python 的 Tk 版本为 {ver or '未知'}，新 macOS 上会白屏。")
        C.info("请改用 run-gui.sh 或安装带 Tk 8.6+ 的 Python。")
    subprocess.Popen([sys.executable, str(gui_py)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    C.ok("已启动图形界面")


def cmd_fab(args):
    """启动悬浮按钮（贴千问办公窗口右下角）"""
    here = Path(__file__).resolve().parent
    launcher = here / "run-fab.sh"
    if launcher.exists():
        subprocess.Popen(["/bin/bash", str(launcher)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        C.ok("已启动悬浮按钮")
        return
    fab_py = here / "fab.py"
    if not fab_py.exists():
        die("未找到 fab.py / run-fab.sh。")
    subprocess.Popen([sys.executable, str(fab_py)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    C.ok("已启动悬浮按钮（若窗口空白，请改用 run-fab.sh：需要 Tk 8.6+）")


def cmd_doctor(args):
    print(f"\n{C.BOLD}千问办公切换器 · 环境自检{C.RESET}\n")

    bundle = find_app_bundle()
    print(f"  应用包      ：{bundle or C.RED + '未找到' + C.RESET}")
    print(f"  进程名      ：{APP_NAME}（BundledID {APP_BUNDLE_ID}）")
    print(f"  运行中      ：{'是' if is_running() else '否'}")
    print(f"  SDK 目录    ：{SDK_DIR} {'✓' if SDK_DIR.exists() else '✗ 不存在'}")
    print(f"  应用数据目录：{APP_SUPPORT_DIR} {'✓' if APP_SUPPORT_DIR.exists() else '✗ 不存在'}")
    print(f"  切换器目录  ：{SWITCHER_DIR} {'✓' if SWITCHER_DIR.exists() else '(首次使用时创建)'}")
    print(f"\n  {C.BOLD}SDK 侧登录态数据集{C.RESET}")
    for rel in AUTH_DATASETS_SDK:
        p = SDK_DIR / rel
        sz = human(dir_size(p)) if p.exists() else "—"
        flag = f"{C.GREEN}✓{C.RESET}" if p.exists() else f"{C.DIM}·{C.RESET}"
        print(f"    {flag} {rel:<24}{sz:>10}")
    print(f"\n  {C.BOLD}应用侧登录态数据集{C.RESET}")
    for rel in AUTH_DATASETS_APP:
        p = APP_SUPPORT_DIR / rel
        sz = human(dir_size(p)) if p.exists() else "—"
        flag = f"{C.GREEN}✓{C.RESET}" if p.exists() else f"{C.DIM}·{C.RESET}"
        print(f"    {flag} {rel:<24}{sz:>10}")

    ident = detect_identity()
    print(f"\n  {C.BOLD}当前账号{C.RESET}：{identity_label(ident)}")
    print()


# ── 交互式 TUI ───────────────────────────────────────────────────────
def interactive_menu():
    while True:
        manifest = load_manifest()
        profiles = manifest["profiles"]
        active = manifest.get("active")
        ident = detect_identity()

        print(f"\n{C.BOLD}{'═' * 58}{C.RESET}")
        print(f"  {C.CYAN}千问办公 · 账户一键切换{C.RESET}  v1.0")
        print(f"{'═' * 58}")
        print(f"  当前登录：{identity_label(ident)}")
        print(f"  当前 profile：{C.GREEN}{active or '(未保存)'}{C.RESET}")
        print(f"\n  {C.BOLD}已保存账号{C.RESET}")
        if not profiles:
            print(f"    {C.DIM}(空 — 先登录一个账号，按 s 保存){C.RESET}")
        else:
            for i, (name, meta) in enumerate(profiles.items(), 1):
                marker = f"{C.GREEN}●{C.RESET}" if name == active else " "
                pi = meta.get("identity") or {}
                who = pi.get("name") or pi.get("username") or "—"
                note = f" {C.dim(meta['note'])}" if meta.get("note") else ""
                print(f"   {marker} {C.BOLD}{i}{C.RESET}. {name:<14} {who}{note}")

        print(f"\n  {C.BOLD}操作{C.RESET}")
        print(f"    {C.YELLOW}[序号]{C.RESET} 切换到该账号")
        print(f"    {C.YELLOW}s{C.RESET}      保存当前登录态为新账号")
        print(f"    {C.YELLOW}r{C.RESET}      重命名 profile")
        print(f"    {C.YELLOW}n{C.RESET}      改备注")
        print(f"    {C.YELLOW}d{C.RESET}      删除 profile")
        print(f"    {C.YELLOW}b{C.RESET}      仅备份当前登录态")
        print(f"    {C.YELLOW}i{C.RESET}      环境自检")
        print(f"    {C.YELLOW}q{C.RESET}      退出")

        try:
            choice = input("\n  请选择 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        low = choice.lower()

        if low in ("q", "quit", "exit"):
            return
        elif low == "s":
            try:
                nm = input("  profile 名称 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if nm:
                try:
                    cmd_save(argparse.Namespace(name=nm, force=False, note=""))
                except SystemExit:
                    pass
                input("\n  按回车继续 …")
        elif low == "r":
            try:
                old = input("  要重命名的 profile > ").strip()
                new = input("  新名称 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if old and new:
                try:
                    cmd_edit(argparse.Namespace(name=old, rename=new, note=None))
                except SystemExit:
                    pass
                input("\n  按回车继续 …")
        elif low == "n":
            try:
                nm = input("  profile 名称 > ").strip()
                note = input("  备注 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if nm:
                try:
                    cmd_edit(argparse.Namespace(name=nm, rename=None, note=note))
                except SystemExit:
                    pass
                input("\n  按回车继续 …")
        elif low == "d":
            try:
                nm = input("  要删除的 profile > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if nm:
                try:
                    cmd_delete(argparse.Namespace(name=nm, yes=False))
                except SystemExit:
                    pass
                input("\n  按回车继续 …")
        elif low == "b":
            try:
                cmd_backup(argparse.Namespace())
            except SystemExit:
                pass
            input("\n  按回车继续 …")
        elif low == "i":
            cmd_doctor(argparse.Namespace())
            input("  按回车继续 …")
        elif choice.isdigit():
            names = list(profiles.keys())
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                target = names[idx]
                if target == active:
                    C.warn(f"'{target}' 已是当前 profile。")
                    time.sleep(1)
                    continue
                print()
                try:
                    cmd_switch(argparse.Namespace(name=target, no_restart=False))
                except SystemExit:
                    pass
                input("\n  按回车继续 …")
            else:
                C.warn("序号超出范围。")
                time.sleep(1)
        else:
            C.warn("无效输入。")
            time.sleep(1)


# ── CLI ─────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog="qwenwork-switch",
        description="千问办公（QwenWorkCN）账户一键切换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  qwenwork-switch                       # 交互式菜单
  qwenwork-switch save work             # 保存当前登录态为 'work'
  qwenwork-switch save personal --force  # 覆盖保存
  qwenwork-switch switch work           # 切换到 'work'
  qwenwork-switch list                  # 列出所有账号
  qwenwork-switch current               # 查看当前登录账号
  qwenwork-switch doctor                # 环境自检

说明：save/switch/restore 会先退出千问办公，完成后自动重新拉起。
      切换前会把当前登录态回写到它所属的 profile，所以来回切不会掉登录。
""",
    )
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("save", help="保存当前登录态为指定 profile")
    sp.add_argument("name", help="profile 名称")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的同名 profile")
    sp.add_argument("--note", default="", help="备注，例如「公司账号」")
    sp.add_argument("--no-restart", action="store_true", help="保存后不自动启动应用")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("switch", help="切换到指定 profile")
    sp.add_argument("name", help="profile 名称")
    sp.add_argument("--no-restart", action="store_true", help="切换后不自动启动应用")
    sp.set_defaults(func=cmd_switch)

    sub.add_parser("list", help="列出所有已保存账号").set_defaults(func=cmd_list)
    sub.add_parser("ls", help="list 的别名").set_defaults(func=cmd_list)
    sub.add_parser("current", help="显示当前登录账号").set_defaults(func=cmd_current)
    sub.add_parser("doctor", help="环境自检：路径 / 数据集 / 进程").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("delete", help="删除指定 profile")
    sp.add_argument("name", help="profile 名称")
    sp.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("edit", help="重命名 / 改备注")
    sp.add_argument("name", help="profile 名称")
    sp.add_argument("--rename", help="新名称")
    sp.add_argument("--note", help="备注")
    sp.set_defaults(func=cmd_edit)

    sub.add_parser("backup", help="仅备份当前登录态").set_defaults(func=cmd_backup)
    sub.add_parser("restore", help="从最近一次备份恢复").set_defaults(func=cmd_restore)
    sub.add_parser("gui", help="打开图形界面").set_defaults(func=cmd_gui)
    sub.add_parser("fab", help="启动悬浮按钮（贴窗口右下角）").set_defaults(func=cmd_fab)

    return p


def main():
    args = build_parser().parse_args()
    if args.command is None:
        interactive_menu()
        return
    args.func(args)


if __name__ == "__main__":
    main()
