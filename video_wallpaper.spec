# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

    python -m PyInstaller video_wallpaper.spec --noconfirm

产物：dist/VideoWallpaper.exe（单文件、无控制台窗口）。
"""

# 用不到的 Qt 模块全部排除，能把体积砍掉一大半。
# 只保留：QtCore / QtGui / QtWidgets / QtMultimedia / QtMultimediaWidgets
EXCLUDES = [
    # 体积大户
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    # 其他用不到的功能模块
    "PySide6.QtBluetooth", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtNfc", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    # 标准库里用不到的大块
    "tkinter", "test", "unittest", "pydoc", "doctest", "sqlite3",
    "numpy", "PIL", "setuptools", "pip",
]
# 只保留：QtCore / QtGui / QtWidgets / QtMultimedia / QtMultimediaWidgets
# 注意 QtMultimediaWidgets 是核心依赖，不要加进 EXCLUDES。

a = Analysis(
    ["wallpaper.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["autostart"],       # 运行时 import，静态分析抓不到
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoWallpaper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                     # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/video-wallpaper.ico",
)
