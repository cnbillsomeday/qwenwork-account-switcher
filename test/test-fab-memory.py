#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：悬浮钮的热路径不许泄漏（v1.1 修的是 9GB 那个坑）

为什么用 phys_footprint 而不是 RSS：
  泄漏的页被内存压缩器换出去之后，RSS 只剩 30MB，看着完全正常；
  真实占用要看 ri_phys_footprint（libproc），v1.0 泄漏到 9684MB 时 RSS 才 31.8MB。

判据：连打 6000 次 Screen.snapshot()，footprint 增量必须 < 2MB。
  v1.0：13.7 KB/次 × 6000 ≈ 82 MB → 必失败
  v1.1：收敛在 +48KB 左右（分配器预热，不再增长）

用法：python3 test/test-fab-memory.py
"""

import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fab  # noqa: E402

LIMIT_MB = 2.0
ROUNDS = 6000


def footprint():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)

    class RI(ctypes.Structure):
        _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
            (n, ctypes.c_uint64) for n in (
                "ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups "
                "ri_pageins ri_wired_size ri_resident_size ri_phys_footprint "
                "ri_proc_start_abstime ri_proc_exit_abstime ri_child_user_time "
                "ri_child_system_time ri_child_pkg_idle_wkups ri_child_interrupt_wkups "
                "ri_child_pageins ri_child_elapsed_abstime ri_diskio_bytesread "
                "ri_diskio_byteswritten").split()]

    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    buf = RI()
    if lib.proc_pid_rusage(os.getpid(), 2, ctypes.byref(buf)) != 0:
        raise OSError("proc_pid_rusage 失败")
    return buf.ri_phys_footprint


def main():
    try:
        footprint()
    except OSError:
        print("SKIP：拿不到 phys_footprint（非 macOS？）")
        return 0

    me = os.getpid()
    snap = lambda: fab.Screen.snapshot(self_pid=me)   # noqa: E731

    for _ in range(100):          # 预热：让 dylib 符号、key 缓存都就位
        snap()
    base = footprint()
    for _ in range(ROUNDS):
        snap()
    grew = (footprint() - base) / 1024 / 1024

    print(f"Screen.snapshot() × {ROUNDS}：footprint {grew:+.2f} MB"
          f"（判据 < {LIMIT_MB} MB）")
    if grew > LIMIT_MB:
        print("FAIL：热路径又泄漏了。检查 CGWindowListCopyWindowInfo 的返回值有没有 "
              "CFRelease、字典 key 是不是每次都在新建 CFString。")
        return 1

    # 顺手打印一次真实读数，方便人肉核对判断逻辑
    print("当前 snapshot：", snap(), "（(rect, 前台是不是千问办公)；没开千问办公就是 (None, False)）")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
