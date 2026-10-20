# -*- coding: utf-8 -*-
"""video-wallpaper —— 把视频设成 Windows 桌面动态壁纸（支持多显示器）。

用法（无参数，直接运行）：

    python wallpaper.py

原理
----
Windows 桌面由 Progman / WorkerW / SHELLDLL_DefView 三层窗口协作绘制：

    WorkerW            <- 壁纸画在这层（图标层的下方）
    WorkerW            <- 图标层宿主
      └ SHELLDLL_DefView -> SysListView32   （桌面图标）

向 Progman 发送未公开消息 0x052C，资源管理器会**异步**分裂出一个新的
顶层 WorkerW 作为纯壁纸层。拿到它的 HWND 后，把无边框、无焦点、
循环播放的视频窗口 SetParent 挂上去，视频就显示在桌面图标下方、
所有普通窗口下方，从而实现「动态壁纸」，且不依赖任何第三方软件。

多显示器：每个显示器对应一个视频窗口，都挂在同一个壁纸层 WorkerW 下，
各自按自己的显示器几何摆放，可以配不同的视频。

注意：0x052C 创建窗口是异步的，发完消息必须轮询等待，不能立刻枚举。

资源占用控制
------------
* 视频无音轨时不做任何音频解码；
* 前台出现占满整屏的窗口（游戏 / 全屏播放器）时自动暂停解码；
* 锁屏 / 远程会话断开时自动暂停解码；
* 视频解码走 D3D11 硬解。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
    QPolygonF,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon

APP_NAME = "VideoWallpaper"

# 打包成 exe 后 __file__ 指向临时解包目录，配置和日志必须放到 exe 旁边
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "video-wallpaper.log")

try:
    import autostart
except ImportError:          # 单独拷贝 wallpaper.py 运行时也不至于崩
    autostart = None


def log(msg: str) -> None:
    """写一行运行日志（无控制台时便于事后排查）。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Win32 部分
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

WM_SPAWN_WORKERW = 0x052C
SMTO_ABORTIFHUNG = 0x0002

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

HWND_BOTTOM = 1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020

MONITOR_DEFAULTTONEAREST = 2
DESKTOP_SWITCHDESKTOP = 0x0100


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.MapWindowPoints.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.POINTER(POINT), wintypes.UINT,
]
user32.MapWindowPoints.restype = ctypes.c_int


def _enum_top_level() -> list[int]:
    rows: list[int] = []

    def _cb(hwnd, _lparam):
        rows.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return rows


def _class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _icon_host() -> int:
    """返回挂着桌面图标层(SHELLDLL_DefView)的那个窗口。"""
    for hwnd in _enum_top_level():
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            return hwnd
    return 0


def find_wallpaper_target(timeout: float = 3.0, spawn: bool = True):
    """定位可以承载视频窗口的「壁纸层」。

    返回 (hwnd, mode)：
      mode = "behind"  -> 图标层之后的空白 WorkerW（标准壁纸层，最理想）
      mode = "inside"  -> 图标层宿主自身，需把视频压到其子窗口最底层
      mode = "progman" -> 兜底用 Progman

    关键：0x052C 之后资源管理器是**异步**创建 WorkerW 的，
    必须轮询等待，否则会枚举不到。

    spawn=False 时只做探测、不触发资源管理器创建（供自检脚本使用）。
    """
    progman = user32.FindWindowW("Progman", None)
    if progman and spawn:
        result = ctypes.c_size_t()
        user32.SendMessageTimeoutW(
            progman, WM_SPAWN_WORKERW, 0, 0, SMTO_ABORTIFHUNG, 1000,
            ctypes.byref(result),
        )
        user32.SendMessageTimeoutW(
            progman, WM_SPAWN_WORKERW, 0x0D, 1, SMTO_ABORTIFHUNG, 1000,
            ctypes.byref(result),
        )

    host = 0
    deadline = time.monotonic() + timeout
    while True:
        host = _icon_host()
        if host:
            nxt = user32.FindWindowExW(None, host, "WorkerW", None)
            if nxt:
                return nxt, "behind"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    if host:
        return host, "inside"
    if progman:
        return progman, "progman"
    return 0, "none"


def is_session_locked() -> bool:
    """当前会话是否已锁屏 / 已断开。"""
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not hdesk:
        return True
    user32.CloseDesktop(hdesk)
    return False


def foreground_is_fullscreen(ignore_hwnds: set[int]) -> bool:
    """前台窗口是否占满整块屏幕（游戏、全屏播放器等）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in ignore_hwnds:
        return False
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False

    cls = _class_name(hwnd)
    if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return False

    m = mi.rcMonitor
    return (
        rect.left <= m.left and rect.top <= m.top
        and rect.right >= m.right and rect.bottom >= m.bottom
    )


def make_tray_icon() -> QIcon:
    """代码画托盘图标，省掉外部图片资源。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    path = QPainterPath()
    path.addRoundedRect(3, 3, 58, 58, 14, 14)
    p.fillPath(path, QColor("#2b2f36"))
    p.setPen(QColor("#5ac8fa"))
    p.drawPath(path)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5ac8fa"))
    p.drawPolygon(QPolygonF([QPointF(25, 20), QPointF(25, 44), QPointF(46, 32)]))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "monitors": {},                # {"显示器名": "视频路径"}
    "pause_on_fullscreen": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            cfg.update(json.load(fp))
    except (OSError, ValueError):
        pass

    if not isinstance(cfg.get("monitors"), dict):
        cfg["monitors"] = {}

    # 兼容旧版单显示器配置：{"video": ..., "screen": ...}
    old_video = cfg.pop("video", "")
    old_screen = cfg.pop("screen", "")
    if old_video and not cfg["monitors"]:
        cfg["monitors"] = {old_screen or "": old_video}

    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 一个显示器上的视频窗口
# --------------------------------------------------------------------------

class Surface:
    """某个显示器上的一块视频壁纸（一个播放器 + 一个窗口）。"""

    def __init__(self, app: QApplication, screen_name: str, video: str):
        self.app = app
        self.screen_name = screen_name
        self.video_path = video
        self.workerw = 0
        self.embedded = False

        self.player = QMediaPlayer()
        self.player.setLoops(QMediaPlayer.Loops.Infinite)   # 无缝循环
        self.player.errorOccurred.connect(self._on_error)

        self.widget = QVideoWidget()
        self.widget.setWindowTitle(APP_NAME)
        self.widget.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.widget.setAttribute(Qt.WA_NativeWindow, True)
        self.widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.widget.setAspectRatioMode(Qt.KeepAspectRatio)  # 保持比例，两侧留黑
        self.widget.setStyleSheet("background:#000;")
        self.player.setVideoOutput(self.widget)

    # ---- 窗口句柄与存在性 ----

    @property
    def hwnd(self) -> int:
        return int(self.widget.winId())

    def screen(self):
        for s in self.app.screens():
            if s.name() == self.screen_name:
                return s
        return None

    # ---- 嵌入桌面底层 ----

    def embed(self, workerw: int) -> None:
        self.workerw = workerw
        self.widget.show()
        hwnd = self.hwnd

        # 改成子窗口样式，并确保不抢焦点、不出现在任务栏 / Alt-Tab
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, (style | WS_CHILD) & ~WS_POPUP)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW,
        )

        user32.SetParent(hwnd, workerw)
        self.embedded = True
        self.apply_geometry()

    def apply_geometry(self) -> None:
        """摆到本显示器对应的物理区域上，并压到最底层。

        坐标系注意：窗口此时是 WorkerW 的子窗口，SetWindowPos 用的是
        **父窗口客户区坐标**；壁纸层 WorkerW 的客户区覆盖整个虚拟桌面，
        多显示器时其左上角通常是负坐标。所以必须先把屏幕坐标用
        MapWindowPoints 换算成父客户区坐标，否则会偏到别的显示器上。
        """
        if not self.embedded or not self.workerw or not user32.IsWindow(self.workerw):
            return

        screen = self.screen()
        if screen is None:
            return

        geo = screen.geometry()
        dpr = screen.devicePixelRatio()
        sx = int(round(geo.x() * dpr))
        sy = int(round(geo.y() * dpr))
        w = int(round(geo.width() * dpr))
        h = int(round(geo.height() * dpr))

        point = POINT(sx, sy)
        user32.MapWindowPoints(None, self.workerw, ctypes.byref(point), 1)

        user32.SetWindowPos(
            self.hwnd, wintypes.HWND(HWND_BOTTOM), point.x, point.y, w, h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
        )
        log(f"[{self.screen_name}] 摆放：屏幕坐标=({sx},{sy}) "
            f"父客户区坐标=({point.x},{point.y}) 尺寸={w}x{h}")

    def is_attached(self) -> bool:
        """窗口是否还挂在有效的父窗口下。"""
        parent = int(user32.GetParent(self.hwnd) or 0)
        return bool(parent) and bool(user32.IsWindow(parent))

    # ---- 播放控制 ----

    def load(self, path: str) -> None:
        self.video_path = path
        if not path or not os.path.isfile(path):
            log(f"[{self.screen_name}] 视频不存在，跳过：{path!r}")
            return
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        log(f"[{self.screen_name}] 载入视频：{path}")

    def set_playing(self, playing: bool) -> None:
        if playing:
            self.player.play()
        else:
            self.player.pause()

    def close(self) -> None:
        self.player.stop()
        self.player.setVideoOutput(None)
        self.widget.hide()
        self.widget.deleteLater()
        self.embedded = False

    def _on_error(self, *_args) -> None:
        log(f"[{self.screen_name}] 播放出错：{self.player.errorString()}")


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------

class WallpaperApp:
    def __init__(self, app: QApplication):
        self.app = app
        self.cfg = load_config()

        self.workerw = 0
        self.mode = "none"
        self.surfaces: dict[str, Surface] = {}
        self.user_paused = False
        self.auto_paused_reason = ""      # "fullscreen" / "locked"

        self._normalize_monitor_keys()
        self._build_tray()

        # 桌面被重建 / 资源管理器重启时自动重新贴回底层
        self.watchdog = QTimer()
        self.watchdog.setInterval(5000)
        self.watchdog.timeout.connect(self._check_embed)
        self.watchdog.start()

        # 全屏 / 锁屏检测，空闲时自动停解码
        self.guard = QTimer()
        self.guard.setInterval(2000)
        self.guard.timeout.connect(self._check_auto_pause)
        self.guard.start()

        # 显示器插拔 / 改分辨率后重新摆放
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)
        for scr in app.screens():
            scr.geometryChanged.connect(self._on_screens_changed)

        self.embed()
        self._sync_surfaces()

    # ---------------- 配置归一 ----------------

    def _normalize_monitor_keys(self) -> None:
        """把旧配置里空显示器名（表示主屏）解析成真实名字。"""
        raw = dict(self.cfg.get("monitors", {}))
        if "" in raw:
            prim = self.app.primaryScreen()
            name = prim.name() if prim is not None else ""
            video = raw.pop("")
            if name and name not in raw:
                raw[name] = video
            save_config(self.cfg)
        self.cfg["monitors"] = raw

    # ---------------- 桌面嵌入 ----------------

    def embed(self) -> None:
        """找到壁纸层，并把所有视频窗口挂上去。"""
        target, mode = find_wallpaper_target()
        if not target:
            log("embed 失败：未找到桌面壁纸层 WorkerW")
            self.tray.showMessage(
                APP_NAME, "未能找到桌面壁纸层（WorkerW），壁纸无法显示。",
                QSystemTrayIcon.Warning, 5000,
            )
            return

        self.workerw, self.mode = target, mode
        for surface in self.surfaces.values():
            surface.embed(target)

    def _on_screens_changed(self, *_args) -> None:
        log("检测到显示器变化，重新同步")
        self._sync_surfaces()

    def _check_embed(self) -> None:
        """周期性确认窗口仍然挂在有效父窗口下（资源管理器重启后自动修复）。"""
        configured = {k: v for k, v in self.cfg.get("monitors", {}).items() if v}
        alive = [s for s in self.surfaces.values() if s.embedded]

        # 有配置但一个窗口都没有 —— 说明首次嵌入失败，重试
        if configured and not alive:
            log("看门狗：尚未挂载任何窗口，重新尝试")
            self.embed()
            self._sync_surfaces()
            return

        if any(not s.is_attached() for s in alive):
            log("看门狗：父窗口失效，重新挂载")
            self.embed()

    # ---------------- 显示器 ↔ 视频 的同步 ----------------

    def _sync_surfaces(self) -> None:
        """让「配置里的显示器」与「实际存在的窗口」保持一致。"""
        if not self.workerw:
            return

        want = {k: v for k, v in self.cfg.get("monitors", {}).items() if v}
        screens = {s.name() for s in self.app.screens()}

        # 移除：已不再配置的、显示器已拔掉的、视频路径变了的
        for name in list(self.surfaces):
            surface = self.surfaces[name]
            gone = name not in want or name not in screens
            changed = not gone and surface.video_path != want[name]
            if gone:
                log(f"[{name}] 关闭该显示器的壁纸")
                surface.close()
                del self.surfaces[name]
            elif changed:
                log(f"[{name}] 切换视频")
                surface.load(want[name])

        # 新增：新配置的显示器
        for name, video in want.items():
            if name in self.surfaces or name not in screens:
                continue
            surface = Surface(self.app, name, video)
            surface.embed(self.workerw)
            surface.load(video)
            self.surfaces[name] = surface

        # 已存在的窗口重新摆一次位置（分辨率可能变了）
        for surface in self.surfaces.values():
            surface.apply_geometry()

        log(f"同步完成：壁纸层=0x{self.workerw:08X}（{self.mode}），"
            f"已挂载 {len(self.surfaces)} 个显示器：{', '.join(self.surfaces) or '无'}")
        self._refresh_menu()

    # ---------------- 播放控制 ----------------

    def set_play_all(self, playing: bool) -> None:
        self.user_paused = not playing
        if playing:
            self.auto_paused_reason = ""
        for surface in self.surfaces.values():
            surface.set_playing(playing)
        log("全部" + ("继续播放" if playing else "暂停"))
        self._refresh_menu()

    def _check_auto_pause(self) -> None:
        if self.user_paused:
            return

        reason = ""
        if is_session_locked():
            reason = "locked"
        elif self.cfg.get("pause_on_fullscreen", True):
            ignore = {s.hwnd for s in self.surfaces.values()}
            if foreground_is_fullscreen(ignore):
                reason = "fullscreen"

        if reason == self.auto_paused_reason:
            return

        self.auto_paused_reason = reason
        for surface in self.surfaces.values():
            surface.set_playing(not reason)
        log(f"自动{'暂停（' + reason + '）' if reason else '恢复播放'}")
        self._refresh_menu()

    # ---------------- 托盘 ----------------

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_tray_icon())
        self.tray.setToolTip(APP_NAME)

        self.menu = QMenu()

        self.act_status = QAction("", self.menu)
        self.act_status.setEnabled(False)
        self.menu.addAction(self.act_status)
        self.menu.addSeparator()

        self.act_toggle = QAction("暂停播放", self.menu)
        self.act_toggle.triggered.connect(self._on_toggle)
        self.menu.addAction(self.act_toggle)
        self.menu.addSeparator()

        self.menu_monitors = self.menu.addMenu("显示器 / 更换视频")
        self.menu_monitors.aboutToShow.connect(self._rebuild_monitor_menu)
        self.menu.addSeparator()

        self.act_pause_fs = QAction("全屏应用时自动暂停", self.menu)
        self.act_pause_fs.setCheckable(True)
        self.act_pause_fs.setChecked(bool(self.cfg.get("pause_on_fullscreen", True)))
        self.act_pause_fs.triggered.connect(self._on_toggle_pause_fs)
        self.menu.addAction(self.act_pause_fs)

        self.act_autostart = QAction("开机自动启动", self.menu)
        self.act_autostart.setCheckable(True)
        if autostart is None:
            self.act_autostart.setEnabled(False)
        else:
            self.act_autostart.setChecked(self._autostart_enabled())
        self.act_autostart.triggered.connect(self._on_toggle_autostart)
        self.menu.addAction(self.act_autostart)
        self.menu.addSeparator()

        self.act_quit = QAction("退出（恢复静态壁纸）", self.menu)
        self.act_quit.triggered.connect(self._on_quit)
        self.menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()
        self._refresh_menu()

    def _rebuild_monitor_menu(self) -> None:
        """每次打开菜单时重建（显示器插拔后自动刷新）。"""
        self.menu_monitors.clear()
        configured = self.cfg.get("monitors", {})

        for screen in self.app.screens():
            geo = screen.geometry()
            orient = "竖屏" if geo.height() > geo.width() else "横屏"
            sub = self.menu_monitors.addMenu(
                f"{screen.name()}  {geo.width()}×{geo.height()}  {orient}"
            )

            video = configured.get(screen.name(), "")
            head = sub.addAction(
                os.path.basename(video) if video else "（未设置）"
            )
            head.setEnabled(False)
            sub.addSeparator()

            pick = sub.addAction("选择视频…" if not video else "更换视频…")
            pick.triggered.connect(
                lambda _=False, n=screen.name(): self._on_pick_video(n)
            )

            if video:
                remove = sub.addAction("移除这个显示器的壁纸")
                remove.triggered.connect(
                    lambda _=False, n=screen.name(): self._on_clear_monitor(n)
                )

        if not self.app.screens():
            head = self.menu_monitors.addAction("未检测到显示器")
            head.setEnabled(False)

    def _refresh_menu(self) -> None:
        count = len(self.surfaces)
        if not count:
            state = "未设置"
        elif self.user_paused:
            state = "已暂停"
        elif self.auto_paused_reason == "fullscreen":
            state = "已暂停（全屏应用）"
        elif self.auto_paused_reason == "locked":
            state = "已暂停（锁屏）"
        else:
            state = "播放中"
        self.act_status.setText(f"{state} · {count} 个显示器")
        self.act_toggle.setText("继续播放" if self.user_paused else "暂停播放")
        if autostart is not None:
            self.act_autostart.setChecked(self._autostart_enabled())

    @staticmethod
    def _autostart_enabled() -> bool:
        if autostart is None:
            return False
        try:
            return autostart.is_enabled()
        except OSError:
            return False

    def _on_toggle(self) -> None:
        self.set_play_all(bool(self.user_paused))

    def _on_pick_video(self, screen_name: str) -> None:
        configured = self.cfg.get("monitors", {})
        current = configured.get(screen_name, "")
        start_dir = os.path.dirname(current) if current else APP_DIR

        path, _ = QFileDialog.getOpenFileName(
            None, f"为 {screen_name} 选择壁纸视频", start_dir,
            "视频文件 (*.mp4 *.mkv *.webm *.avi *.mov)",
        )
        if not path:
            return

        configured[screen_name] = path
        self.cfg["monitors"] = configured
        save_config(self.cfg)
        log(f"[{screen_name}] 用户指定视频：{path}")
        self._sync_surfaces()

    def _on_clear_monitor(self, screen_name: str) -> None:
        configured = self.cfg.get("monitors", {})
        configured.pop(screen_name, None)
        self.cfg["monitors"] = configured
        save_config(self.cfg)
        self._sync_surfaces()

    def _on_toggle_pause_fs(self, checked: bool) -> None:
        self.cfg["pause_on_fullscreen"] = bool(checked)
        save_config(self.cfg)

    def _on_toggle_autostart(self, checked: bool) -> None:
        if autostart is None:
            return
        try:
            autostart.set_enabled(bool(checked))
        except OSError as exc:  # pragma: no cover
            self.tray.showMessage(APP_NAME, f"设置开机自启失败：{exc}",
                                  QSystemTrayIcon.Warning, 5000)
        self._refresh_menu()

    def _on_quit(self) -> None:
        self.watchdog.stop()
        self.guard.stop()
        for surface in self.surfaces.values():
            surface.close()
        self.surfaces.clear()
        self.tray.hide()
        self.app.quit()


def single_instance_guard() -> bool:
    """同一时间只允许运行一个实例。"""
    ctypes.set_last_error(0)
    kernel32.CreateMutexW(None, False, APP_NAME + "_mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    if not single_instance_guard():
        return 0

    try:
        os.remove(LOG_PATH)          # 每次启动重开日志
    except OSError:
        pass
    log("=== 启动 ===")

    # pythonw / --noconsole 没有控制台，未捕获异常必须落盘，否则崩溃是无声的
    def _hook(exc_type, exc, tb):
        import traceback
        log("!!! 未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = _hook

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log("系统托盘不可用，退出")
        return 1

    # 必须持有引用：否则实例会被 GC 回收，
    # 连带销毁视频窗口与定时器，事件循环中定时器被删会直接崩进程
    wallpaper = WallpaperApp(app)
    code = app.exec()
    log(f"=== 退出（code={code}）===")
    return code


if __name__ == "__main__":
    sys.exit(main())
