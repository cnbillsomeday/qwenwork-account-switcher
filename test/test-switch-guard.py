#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：账户切换不得互相覆盖

对应线上事故（2026-10-29 16:26）：
  用户登录新账号 → 保存为 profile '6468' → 界面仍把「使用中」标在旧的
  '当前账号' 上 → 用户点了一次「切换」→ 旧 profile 的旧账号快照被覆盖，
  两个 profile 里的数据变成同一个账号。

被测行为：
  1. save 之后 active 必须跟着变成新保存的 profile；
  2. 当前登录账号 ≠ 旧 active profile 记录的账号时，切换不得回写覆盖旧 profile，
     且要把当前登录态另存到 trash 里；
  3. 当前登录账号 == 旧 active profile 的账号时，切换仍按原逻辑回写（不回归）。

运行：python3 test/test-switch-guard.py
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import switcher as sw  # noqa: E402

PASS, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"
failures = []


def check(cond, label, detail=""):
    print(f"  {PASS if cond else FAIL} {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def sandbox():
    """把模块级路径全部重定向到临时目录"""
    root = Path(tempfile.mkdtemp(prefix="qw-test-"))
    sw.SDK_DIR = root / "sdk"
    sw.APP_SUPPORT_DIR = root / "app"
    sw.SWITCHER_DIR = root / "sw"
    sw.PROFILES_DIR = sw.SWITCHER_DIR / "profiles"
    sw.MANIFEST_PATH = sw.SWITCHER_DIR / "manifest.json"
    sw.LOG_PATH = sw.SWITCHER_DIR / "switcher.log"
    sw.LOCK_PATH = sw.SWITCHER_DIR / ".lock"
    sw.TMP_BACKUP = sw.SWITCHER_DIR / ".current-backup"
    sw.TRASH_DIR = sw.SWITCHER_DIR / "trash"
    for d in (sw.SDK_DIR, sw.APP_SUPPORT_DIR, sw.SWITCHER_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # 不真的去开关千问办公
    sw.ensure_app_stopped = lambda *a, **k: None
    sw.relaunch_app = lambda *a, **k: None
    return root


def login_as(user, plan="个人免费版"):
    """伪造一份"当前登录态"，带上会被备份的特征文件"""
    (sw.SDK_DIR / ".status.json").write_text(json.dumps(
        {"logged_in": True, "username": user, "name": user,
         "plan": plan, "user_type": "personal"}, ensure_ascii=False))
    (sw.SDK_DIR / ".auth").mkdir(parents=True, exist_ok=True)
    (sw.SDK_DIR / ".auth" / "machine_id").write_text("m-" + user)
    (sw.APP_SUPPORT_DIR / "auth.dat").write_bytes(("token-" + user).encode())


def stored_user(profile):
    """读 profile 里实际存的账号"""
    p = sw.PROFILES_DIR / profile / "sdk" / ".status.json"
    if not p.exists():
        return None
    return json.loads(p.read_text()).get("username")


def argns(**kw):
    return type("A", (), kw)


def run():
    root = sandbox()

    print("\n场景 1：保存新账号后 active 应跟着切换")
    login_as("acctA")
    sw.cmd_save(argns(name="A", force=False, note="", no_restart=True))
    check(sw.load_manifest()["active"] == "A", "保存 A 后 active == A")

    login_as("acctB")
    sw.cmd_save(argns(name="B", force=False, note="", no_restart=True))
    man = sw.load_manifest()
    check(man["active"] == "B", "保存 B 后 active == B（这是事故的直接诱因）",
          f"实际 active={man['active']}")
    check(stored_user("A") == "acctA" and stored_user("B") == "acctB",
          "两个 profile 各自存的是自己的账号")

    print("\n场景 2：登录态已变成别的账号时，切换不得覆盖旧 profile")
    login_as("acctC")                      # 用户手动登出换了号，但没保存
    sw.cmd_switch(argns(name="A", no_restart=True))
    check(stored_user("B") == "acctB",
          "旧 active profile 'B' 未被 acctC 覆盖", f"实际={stored_user('B')}")
    rescues = list((sw.TRASH_DIR).glob("B-*")) if sw.TRASH_DIR.exists() else []
    check(len(rescues) == 1, "当前登录态已另存到 trash 备查",
          f"{rescues[0].name if rescues else '未生成'}")
    check(stored_user("A") == "acctA", "目标 profile 'A' 正确落地")
    check(sw.detect_identity().get("username") == "acctA",
          "切换后应用登录态确实变成了 A")

    print("\n场景 3：登录态与 active 一致时，切换照常回写（不回归）")
    login_as("acctA2")                     # 假装在 A 里又刷新了 token
    sw.cmd_save(argns(name="A", force=True, note="", no_restart=True))
    before = (sw.PROFILES_DIR / "A" / "app-support" / "auth.dat").read_bytes()
    sw.cmd_switch(argns(name="B", no_restart=True))
    after = (sw.PROFILES_DIR / "A" / "app-support" / "auth.dat").read_bytes()
    check(before == after, "active=A 时切到 B，A 的快照被正常回写且内容一致")
    check(stored_user("B") == "acctB", "切到 B 后登录态为 acctB")

    print("\n场景 4：同一账号重复保存应给出重复提示")
    login_as("acctB")
    sw.cmd_save(argns(name="B2", force=False, note="", no_restart=True))
    check((sw.PROFILES_DIR / "B2").exists(), "重复账号仍可保存（但会提示）")

    shutil.rmtree(root, ignore_errors=True)

    print("\n" + ("─" * 46))
    if failures:
        print(f"{FAIL} 失败 {len(failures)} 项：" + "、".join(failures))
        return 1
    print(f"{PASS} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
