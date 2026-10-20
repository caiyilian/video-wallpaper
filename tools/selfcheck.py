# -*- coding: utf-8 -*-
"""自检脚本：确认壁纸是否正常挂载，并报告每个显示器的状态。

用法：

    python tools/selfcheck.py

排查「壁纸不见了 / 只有一块屏有壁纸」时先跑这个。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wallpaper as W  # noqa: E402


def _find_video_windows() -> list[int]:
    """找本程序所有的视频窗口（它们都叫 APP_NAME）。"""
    found: list[int] = []

    def _match(h):
        buf = ctypes.create_unicode_buffer(256)
        W.user32.GetWindowTextW(h, buf, 256)
        return buf.value == W.APP_NAME

    for top in W._enum_top_level():
        if _match(top):
            found.append(top)
            continue
        kids: list[int] = []

        def _cb(h, _l):
            kids.append(h)
            return True

        W.user32.EnumChildWindows(top, W.WNDENUMPROC(_cb), 0)
        found.extend(h for h in kids if _match(h))

    return sorted(set(found))


def _reaches_workerw(hwnd: int) -> bool:
    """父窗口链上是否能走到 WorkerW。

    Qt 会为视频控件额外建一个子窗口，它本身不是 WorkerW 的直接子窗口，
    但在祖先链上——所以只看直接父窗口会误报。
    """
    seen = set()
    cur = int(W.user32.GetParent(hwnd) or 0)
    while cur and cur not in seen:
        seen.add(cur)
        if W._class_name(cur) == "WorkerW":
            return True
        cur = int(W.user32.GetParent(cur) or 0)
    return False


def main() -> int:
    app = W.QApplication([])
    cfg = W.load_config()
    problems: list[str] = []

    print("=" * 68)
    print("1) 显示器")
    print("=" * 68)
    screens = {}
    for s in app.screens():
        g = s.geometry()
        screens[s.name()] = s
        orient = "竖屏" if g.height() > g.width() else "横屏"
        print(f"   {s.name():<20} {g.width():>5}×{g.height():<5} {orient}   "
              f"桌面坐标=({g.x()}, {g.y()})")

    print()
    print("=" * 68)
    print("2) 配置的显示器 → 视频")
    print("=" * 68)
    configured = cfg.get("monitors", {})
    if not configured:
        problems.append("配置里没有为任何显示器设置视频")
        print("   （未配置任何显示器）")
    for name, video in configured.items():
        exists = bool(video) and os.path.isfile(video)
        if not exists:
            problems.append(f"{name} 的视频文件不存在：{video}")
        if name not in screens:
            problems.append(f"{name} 这个显示器当前不在线")
            exists = False
        print(f"   {'✓' if exists else '✗'} {name:<20} {video}")

    print()
    print("=" * 68)
    print("3) 桌面壁纸层（WorkerW）")
    print("=" * 68)
    target, mode = W.find_wallpaper_target(timeout=1.0, spawn=False)
    if target:
        print(f"   找到：0x{target:08X}  class={W._class_name(target)!r}  mode={mode}")
    else:
        problems.append("未找到壁纸层 WorkerW（程序没在运行？）")
        print("   ✗ 未找到壁纸层")

    print()
    print("=" * 68)
    print("4) 视频窗口挂载情况")
    print("=" * 68)
    windows = _find_video_windows()
    if not windows:
        problems.append("没有找到任何视频窗口")
        print("   ✗ 没找到视频窗口（程序可能没在运行）")

    # 同一块壁纸可能对应多个 Qt 内部窗口（外层窗口 + 渲染子窗口），
    # 按「尺寸 + 位置」去重后只报告一块屏一条
    seen_shapes: dict[tuple[int, int, int, int], bool] = {}
    for hwnd in windows:
        rect = wintypes.RECT()
        W.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        shape = (rect.left, rect.top, rect.right, rect.bottom)
        ok = _reaches_workerw(hwnd)
        seen_shapes[shape] = seen_shapes.get(shape, True) and ok

    for (left, top, right, bottom), ok in sorted(seen_shapes.items()):
        if not ok:
            problems.append(f"位于 ({left}, {top}) 的壁纸窗口没有挂在 WorkerW 下")
        print(f"   {'✓' if ok else '✗'} 尺寸={right - left}×{bottom - top}  "
              f"位置=({left}, {top})  挂在壁纸层下={ok}")

    print()
    print("=" * 68)
    print("5) 运行日志（尾部）")
    print("=" * 68)
    if os.path.exists(W.LOG_PATH):
        with open(W.LOG_PATH, "r", encoding="utf-8") as fp:
            for line in fp.read().strip().splitlines()[-14:]:
                print("   " + line)
    else:
        print("   （无日志文件）")

    print()
    print("=" * 68)
    if problems:
        print("发现问题：")
        for p in problems:
            print(f"   ✗ {p}")
    else:
        print("结论：一切正常 ✓")
    print("=" * 68)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
