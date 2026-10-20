"""开机自启管理：通过 Windows 注册表 HKCU Run 键控制。

开机自启指向本程序自身：
  - 打包成 exe 时：直接运行 exe
  - 以脚本运行时：用 pythonw.exe（无控制台窗口）运行 wallpaper.py
"""

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "VideoWallpaper"


def _command() -> str:
    """返回注册表里写入的启动命令字符串。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：直接运行 exe
        return f'"{sys.executable}"'

    # 脚本运行：优先 pythonw.exe（无黑框），否则退回 python.exe
    interp_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(interp_dir, "pythonw.exe")
    interp = pythonw if os.path.exists(pythonw) else sys.executable

    main_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallpaper.py")
    if not os.path.exists(main_script) and sys.argv:
        main_script = os.path.abspath(sys.argv[0])
    return f'"{interp}" "{main_script}"'


def is_enabled() -> bool:
    """开机自启是否已开启。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """开启/关闭开机自启。"""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "on":
        set_enabled(True)
        print("开机自启：已开启")
    elif action == "off":
        set_enabled(False)
        print("开机自启：已关闭")
    else:
        print("开机自启：", "已开启" if is_enabled() else "未开启")
        print("命令：", _command())
