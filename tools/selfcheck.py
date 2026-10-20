# -*- coding: utf-8 -*-
"""自检脚本：确认壁纸是否正常挂载，并报告资源占用。

用法：

    python tools/selfcheck.py

排查「壁纸不见了」时先跑这个。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wallpaper as W  # noqa: E402


def main() -> int:
    app = W.QApplication([])
    ok = True
    print("=" * 60)
    print("1) 目标显示器")
    print("=" * 60)
    for s in app.screens():
        g = s.geometry()
        orient = "竖屏" if g.height() > g.width() else "横屏"
        print(f"   {s.name()}  {g.width()}x{g.height()}  {orient}")

    print()
    print("=" * 60)
    print("2) 桌面壁纸层（WorkerW）")
    print("=" * 60)
    # spawn=False：只探测，不触发资源管理器新建窗口
    target, mode = W.find_wallpaper_target(timeout=1.0, spawn=False)
    if target:
        print(f"   找到：0x{target:08X}  class={W._class_name(target)!r}  mode={mode}")
    else:
        ok = False
        print("   ✗ 未找到壁纸层（程序没在运行，或桌面状态异常）")

    print()
    print("=" * 60)
    print("3) 视频窗口挂载情况")
    print("=" * 60)
    video_hwnd = _find_video_window()
    if not video_hwnd:
        ok = False
        print("   ✗ 未找到视频窗口（程序可能没在运行）")
    else:
        parent = int(W.user32.GetParent(video_hwnd) or 0)
        print(f"   视频窗口 hwnd = 0x{video_hwnd:08X}")
        print(f"   父窗口        = 0x{parent:08X}  ({W._class_name(parent)})")
        print(f"   可见          = {bool(W.user32.IsWindowVisible(video_hwnd))}")
        if parent and W._class_name(parent) == "WorkerW":
            print("   ✓ 已正确挂在桌面壁纸层下方")
        else:
            ok = False
            print("   ✗ 父窗口不是 WorkerW，壁纸可能显示异常")

    print()
    print("=" * 60)
    print("4) 运行日志")
    print("=" * 60)
    if os.path.exists(W.LOG_PATH):
        with open(W.LOG_PATH, "r", encoding="utf-8") as fp:
            for line in fp.read().strip().splitlines()[-12:]:
                print("   " + line)
    else:
        print("   （无日志文件）")

    print()
    print("=" * 60)
    print("结论：" + ("一切正常 ✓" if ok else "存在问题，见上面标 ✗ 的项"))
    print("=" * 60)
    return 0 if ok else 1


def _find_video_window() -> int:
    """按标题找本程序的视频窗口。"""
    found = []

    def _cb(hwnd, _l):
        buf = ctypes.create_unicode_buffer(256)
        W.user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == W.APP_NAME:
            found.append(hwnd)
        return True

    W.user32.EnumWindows(W.WNDENUMPROC(_cb), 0)
    if found:
        return found[0]

    # 子窗口不会被 EnumWindows 枚举到，遍历所有顶层窗口的后代再找一遍
    tops = W._enum_top_level()
    for top in tops:
        kids = []

        def _cb2(h, _l):
            kids.append(h)
            return True

        W.user32.EnumChildWindows(top, W.WNDENUMPROC(_cb2), 0)
        for h in kids:
            buf = ctypes.create_unicode_buffer(256)
            W.user32.GetWindowTextW(h, buf, 256)
            if buf.value == W.APP_NAME:
                return h
    return 0


if __name__ == "__main__":
    sys.exit(main())
