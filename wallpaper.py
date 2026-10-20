# -*- coding: utf-8 -*-
"""video-wallpaper —— 把视频设成 Windows 桌面动态壁纸。

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

注意：0x052C 创建窗口是异步的，发完消息必须轮询等待，不能立刻枚举。

资源占用控制
------------
* 视频无音轨时不做任何音频解码（本项目默认视频即无音轨）；
* 前台出现占满整屏的窗口（游戏 / 全屏播放器）时自动暂停解码；
* 锁屏 / 远程会话断开时自动暂停解码；
* 视频解码走 D3D11 硬解，4K60 实测 CPU 占用接近 0。
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
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "video-wallpaper.log")

DEFAULT_VIDEO = r"C:\path\to\your-video.mp4"


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


def foreground_is_fullscreen(own_hwnd: int) -> bool:
    """前台窗口是否占满整块屏幕（游戏、全屏播放器等）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd == own_hwnd:
        return False
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False

    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
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
    "video": DEFAULT_VIDEO,
    "screen": "",
    "pause_on_fullscreen": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            cfg.update(json.load(fp))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------

class WallpaperApp:
    def __init__(self, app: QApplication):
        self.app = app
        self.cfg = load_config()

        self.player = QMediaPlayer()
        self.player.setLoops(QMediaPlayer.Loops.Infinite)   # 无缝循环
        self.player.errorOccurred.connect(self._on_media_error)

        self.video = QVideoWidget()
        self.video.setWindowTitle(APP_NAME)
        self.video.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.video.setAttribute(Qt.WA_NativeWindow, True)
        self.video.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)   # 保持比例，两侧留黑
        self.video.setStyleSheet("background:#000;")
        self.player.setVideoOutput(self.video)

        self.target = 0
        self.mode = "none"
        self.embed_ok = False
        self.user_paused = False
        self.auto_paused_reason = ""      # "fullscreen" / "locked"

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
        app.screenAdded.connect(self.resize_to_screen)
        app.screenRemoved.connect(self.resize_to_screen)
        for scr in app.screens():
            scr.geometryChanged.connect(self.resize_to_screen)

        self.embed()
        self.load_video(self.cfg.get("video", DEFAULT_VIDEO))

    # ---------------- 桌面嵌入 ----------------

    def embed(self) -> None:
        """把视频窗口挂到桌面图标下方那一层。"""
        target, mode = find_wallpaper_target()
        if not target:
            self.embed_ok = False
            log("embed 失败：未找到桌面壁纸层 WorkerW")
            self.tray.showMessage(
                APP_NAME, "未能找到桌面壁纸层（WorkerW），壁纸无法显示。",
                QSystemTrayIcon.Warning, 5000,
            )
            return

        self.target, self.mode = target, mode

        self.video.show()
        hwnd = int(self.video.winId())

        # 改成子窗口样式，并确保不抢焦点、不出现在任务栏 / Alt-Tab
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, (style | WS_CHILD) & ~WS_POPUP)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW,
        )

        user32.SetParent(hwnd, target)
        self.embed_ok = True
        self.resize_to_screen()

        actual = int(user32.GetParent(hwnd) or 0)
        log(f"embed 完成：mode={mode} target=0x{target:08X} "
            f"video_hwnd=0x{hwnd:08X} 实际父窗口=0x{actual:08X} "
            f"({_class_name(actual) if actual else 'TOP-LEVEL'})")

    def resize_to_screen(self, *_args) -> None:
        """把视频窗口摆到目标显示器的物理区域上，并压到最底层。

        注意坐标系：窗口此时已经是 WorkerW 的子窗口，SetWindowPos 用的是
        **父窗口客户区坐标**；而 WorkerW 的客户区覆盖整个虚拟桌面，其左上角
        并不在 (0, 0)（多显示器时通常是个负数）。所以必须先把屏幕坐标用
        MapWindowPoints 换算成父窗口客户区坐标，否则整体会偏移到别的显示器上。
        """
        if not self.target or not user32.IsWindow(self.target):
            return

        screen = self.current_screen()
        if screen is None:
            return

        geo = screen.geometry()
        dpr = screen.devicePixelRatio()
        w = int(round(geo.width() * dpr))
        h = int(round(geo.height() * dpr))
        sx = int(round(geo.x() * dpr))
        sy = int(round(geo.y() * dpr))

        point = POINT(sx, sy)
        user32.MapWindowPoints(None, self.target, ctypes.byref(point), 1)

        hwnd = int(self.video.winId())
        user32.SetWindowPos(
            hwnd, wintypes.HWND(HWND_BOTTOM), point.x, point.y, w, h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
        )
        log(f"摆放：屏幕={screen.name()} 屏幕坐标=({sx},{sy}) "
            f"父客户区坐标=({point.x},{point.y}) 尺寸={w}x{h}")

    # ---------------- 显示器选择 ----------------

    def current_screen(self):
        """按配置挑显示器；配置为空或该显示器已拔掉时回落到主显示器。"""
        want = self.cfg.get("screen", "")
        screens = self.app.screens()
        for s in screens:
            if s.name() == want:
                return s
        return self.app.primaryScreen()

    def _on_pick_screen(self, name: str) -> None:
        self.cfg["screen"] = name
        save_config(self.cfg)
        log(f"切换显示器 -> {name}")
        self.resize_to_screen()
        self._refresh_menu()

    def _check_embed(self) -> None:
        """周期性确认窗口仍然挂在有效的父窗口下。

        资源管理器重启 / 切换分辨率后父窗口会被销毁，此时需要重新挂载。
        不依赖记下来的 target，而是直接问系统「我的父窗口还在不在」。
        """
        if not self.embed_ok:
            self.embed()
            return
        hwnd = int(self.video.winId())
        parent = int(user32.GetParent(hwnd) or 0)
        if not parent or not user32.IsWindow(parent):
            log("看门狗：父窗口失效，重新挂载")
            self.embed_ok = False
            self.embed()

    # ---------------- 播放控制 ----------------

    def load_video(self, path: str) -> None:
        if not path or not os.path.isfile(path):
            self.tray.showMessage(
                APP_NAME, f"找不到视频文件：\n{path}", QSystemTrayIcon.Warning, 6000,
            )
            self._refresh_menu()
            return

        self.cfg["video"] = path
        save_config(self.cfg)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.user_paused = False
        self.auto_paused_reason = ""
        log(f"载入视频：{path}")
        self._refresh_menu()

    def set_play(self, playing: bool) -> None:
        self.user_paused = not playing
        if playing:
            self.auto_paused_reason = ""
            self.player.play()
        else:
            self.player.pause()
        self._refresh_menu()

    def _check_auto_pause(self) -> None:
        if self.user_paused:
            return

        reason = ""
        if is_session_locked():
            reason = "locked"
        elif self.cfg.get("pause_on_fullscreen", True):
            if foreground_is_fullscreen(int(self.video.winId())):
                reason = "fullscreen"

        if reason == self.auto_paused_reason:
            return

        self.auto_paused_reason = reason
        if reason:
            self.player.pause()
            log(f"自动暂停（{reason}）")
        else:
            self.player.play()
            log("自动恢复播放")
        self._refresh_menu()

    def _on_media_error(self, *_args) -> None:
        self.tray.showMessage(
            APP_NAME, f"视频播放出错：{self.player.errorString()}",
            QSystemTrayIcon.Warning, 6000,
        )

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

        self.act_change = QAction("更换壁纸视频…", self.menu)
        self.act_change.triggered.connect(self._on_change)
        self.menu.addAction(self.act_change)

        self.menu_screen = self.menu.addMenu("显示在哪个显示器")
        self.menu_screen.aboutToShow.connect(self._rebuild_screen_menu)
        self.menu.addSeparator()

        self.act_pause_fs = QAction("全屏应用时自动暂停", self.menu)
        self.act_pause_fs.setCheckable(True)
        self.act_pause_fs.setChecked(bool(self.cfg.get("pause_on_fullscreen", True)))
        self.act_pause_fs.triggered.connect(self._on_toggle_pause_fs)
        self.menu.addAction(self.act_pause_fs)

        self.act_autostart = QAction("开机自动启动", self.menu)
        self.act_autostart.setCheckable(True)
        try:
            import autostart
            self.act_autostart.setChecked(autostart.is_enabled())
        except Exception:
            self.act_autostart.setEnabled(False)
        self.act_autostart.triggered.connect(self._on_toggle_autostart)
        self.menu.addAction(self.act_autostart)
        self.menu.addSeparator()

        self.act_quit = QAction("退出（恢复静态壁纸）", self.menu)
        self.act_quit.triggered.connect(self._on_quit)
        self.menu.addAction(self.act_quit)

        self.tray.setContextMenu(self.menu)
        self.tray.show()
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        name = os.path.basename(self.cfg.get("video", "")) or "（未设置）"
        state = "播放中"
        if self.user_paused:
            state = "已暂停"
        elif self.auto_paused_reason == "fullscreen":
            state = "已暂停（全屏应用）"
        elif self.auto_paused_reason == "locked":
            state = "已暂停（锁屏）"
        if not self.embed_ok:
            state = "未挂载"
        screen = self.current_screen()
        where = screen.name() if screen is not None else "?"
        self.act_status.setText(f"{state} · {where[:18]} · {name[:18]}")
        self.act_toggle.setText("继续播放" if self.user_paused else "暂停播放")
        try:
            import autostart
            self.act_autostart.setChecked(autostart.is_enabled())
        except Exception:
            pass

    def _rebuild_screen_menu(self) -> None:
        """每次打开菜单时重建显示器列表（插拔显示器后自动刷新）。"""
        self.menu_screen.clear()
        cur = self.current_screen()
        for s in self.app.screens():
            g = s.geometry()
            orient = "竖屏" if g.height() > g.width() else "横屏"
            act = self.menu_screen.addAction(
                f"{s.name()}  {g.width()}×{g.height()}  {orient}"
            )
            act.setCheckable(True)
            act.setChecked(s is cur)
            act.triggered.connect(lambda _=False, n=s.name(): self._on_pick_screen(n))

    def _on_toggle(self) -> None:
        self.set_play(bool(self.user_paused))

    def _on_change(self) -> None:
        start_dir = os.path.dirname(self.cfg.get("video") or "") or APP_DIR
        path, _ = QFileDialog.getOpenFileName(
            None, "选择壁纸视频", start_dir, "视频文件 (*.mp4 *.mkv *.webm *.avi *.mov)"
        )
        if path:
            self.load_video(path)

    def _on_toggle_pause_fs(self, checked: bool) -> None:
        self.cfg["pause_on_fullscreen"] = bool(checked)
        save_config(self.cfg)

    def _on_toggle_autostart(self, checked: bool) -> None:
        try:
            import autostart
            autostart.set_enabled(bool(checked))
        except Exception as exc:  # pragma: no cover
            self.tray.showMessage(APP_NAME, f"设置开机自启失败：{exc}",
                                  QSystemTrayIcon.Warning, 5000)
        self._refresh_menu()

    def _on_quit(self) -> None:
        self.watchdog.stop()
        self.guard.stop()
        self.player.stop()
        self.player.setVideoOutput(None)
        self.video.hide()
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

    # pythonw 没有控制台，未捕获异常必须落盘，否则崩溃是无声的
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
