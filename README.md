# video-wallpaper

> 把视频设成 Windows 桌面动态壁纸，**每个显示器可以配不同的视频**。不依赖任何第三方软件，单文件 exe，实测单屏内存约 208 MB、整机 CPU 占用 0.27%。

Windows 原生只支持静态壁纸（连 GIF 都不会动），所以做动态壁纸一般要装 Wallpaper Engine（付费）或 Lively Wallpaper（装包 300 MB 起）。
这个工具用系统 API 直接把视频窗口挂到桌面图标下方那一层，**零第三方依赖**，打包出来是一个 54 MB 的单文件 exe。

```
WorkerW                       <- 壁纸画在这层（图标层下方）
WorkerW                       <- 图标层宿主
  └ SHELLDLL_DefView
      └ SysListView32         <- 桌面图标
```

---

## 特性

- **不装任何东西**：只用 Windows 自带的窗口机制 + Qt，不需要 Wallpaper Engine / Lively
- **多显示器各配各的**：每个显示器可以放不同的视频，托盘菜单里逐个指定；竖屏可以放竖版视频
- **省资源**：全屏应用（游戏 / 全屏播放器）和锁屏时自动暂停解码，退出全屏自动恢复
- **自动重挂**：资源管理器重启、显示器插拔、改分辨率后自动重新摆放
- **无黑框**：以 `pythonw` 或 `--noconsole` 方式运行，全程只有托盘图标
- **可开机自启**：托盘菜单一键开关（写 `HKCU` 注册表 Run 键，不碰系统目录）

## 实测资源占用

测试环境：Windows 10 22H2（Build 19045），1920×1080 @ 60Hz，独显硬件解码。

| 项目 | 数值 |
|---|---|
| 运行内存 | 单屏约 **208 MB**；双屏约 **380 MB** |
| CPU | 单核约 **6.5%**，整机 **0.27%**（单屏 1080p60） |
| GPU | 硬件解码，`videodecode` 引擎约 58% |
| 磁盘 | 单文件 exe **54 MB**；视频另计 |

> 上表是**把 4K60 源片转成 1080p60 之后**的数据。若直接喂 4K60 源片，内存会涨到约 580 MB、CPU 单核约 11%，
> 因为 4K 每一帧的缓冲就有 12 MB。屏幕是 1080p 时先转码是纯赚，见下文。

## 多显示器

每个显示器可以配不同的视频，配置与显示器型号名绑定（`settings.json` 里的 `monitors` 字段）。
托盘菜单「**显示器 / 更换视频**」里会列出当前所有显示器，标注横屏 / 竖屏与分辨率，逐个指定即可。

竖屏显示器建议配 **9:16 的竖版视频**（例如 1080×1920），比例一致才能铺满、不留黑边；
拿 16:9 的横版视频放到竖屏上会留下大片黑边。

## 快速开始

### 方式一：下载现成的 exe

到 [Releases](../../releases) 下载 `VideoWallpaper.exe`，双击运行。
首次运行没有配置视频，**右键托盘图标 → 更换壁纸视频…** 选一个 mp4 即可。

### 方式二：从源码运行

```bash
pip install -r requirements.txt      # 只有 PySide6
python wallpaper.py
```

无参数直接运行。程序常驻系统托盘。

## 视频预处理（强烈推荐）

屏幕是 1080p 就别喂 4K 视频。用 ffmpeg 转一道，画质几乎无损，开销降一个量级：

```bash
ffmpeg -i "原始视频.mp4" -vf "scale=1920:1080:flags=lanczos" \
       -c:v libx264 -preset fast -crf 19 -pix_fmt yuv420p \
       -movflags +faststart -an "output-1080p60.mp4"
```

| 参数 | 作用 |
|---|---|
| `scale=1920:1080` | 按屏幕分辨率缩放，省掉 4 倍的帧缓冲开销 |
| `-an` | 丢掉音轨。壁纸本来就不发声，还能省掉整条音频解码链路 |
| `-crf 19` | 壁纸场景画质足够；实测 17 MB 的 4K 源片转出来只有 5 MB |
| `+faststart` | 元数据前置，加载更快 |

## 使用

右键托盘图标：

| 菜单项 | 说明 |
|---|---|
| 状态行 | 显示播放状态与已启用的显示器数量 |
| 暂停 / 继续播放 | 手动暂停（对所有显示器生效） |
| 显示器 / 更换视频 ▸ | 列出每个显示器（标注横屏 / 竖屏、分辨率），逐个选择视频或移除壁纸 |
| 全屏应用时自动暂停 | 默认开启。打游戏、看全屏视频时自动停解码 |
| 开机自动启动 | 写 / 删 `HKCU\...\CurrentVersion\Run` 下的 `VideoWallpaper` 项 |
| 退出（恢复静态壁纸） | 关闭程序并移除所有视频窗口 |

### 配置文件

程序目录下的 `settings.json`，可以直接改：

```json
{
  "monitors": {
    "<显示器型号名>": "D:\\wallpaper\\横版视频-1080p60.mp4",
    "<另一台显示器型号名>": "D:\\wallpaper\\竖版视频-1080p60.mp4"
  },
  "pause_on_fullscreen": true
}
```

键是**显示器的型号名**（在「设置 → 系统 → 显示 → 高级显示设置」里能看到，或跑 `tools/selfcheck.py` 看第 1 节）。
删掉某个键就不给那块屏放壁纸。旧版单显示器配置（`video` + `screen`）会自动迁移过来。

命令行辅助：

```bash
python autostart.py status     # 查看开机自启状态
python autostart.py on|off     # 开关开机自启
python tools/selfcheck.py      # 自检：显示器 / 壁纸层 / 视频窗口挂载情况
```

## 工作原理

Windows 桌面由 `Progman`、`WorkerW`、`SHELLDLL_DefView` 三层窗口协作绘制。
向 `Progman` 发送未公开消息 `0x052C`，资源管理器会分裂出一个**新的 `WorkerW`** 作为纯壁纸层；
拿到它的 HWND 后，把无边框、无焦点、不抢激活的视频窗口 `SetParent` 挂上去，
视频就显示在桌面图标下方、所有普通窗口下方。

这块新 `WorkerW` 挂在哪，随 Windows 版本变过：

| 系统 | 结构 | 日志里的 `mode` |
|---|---|---|
| Windows 10 / 早期 Win11 | 新的 `WorkerW` 是 `Progman` 的**顶层兄弟**，排在图标层宿主后面 | `behind` |
| Windows 11 24H2+（实测 26200 / 25H2） | 新的 `WorkerW` 变成 **`Progman` 的子窗口**，与 `SHELLDLL_DefView` 并列 | `nested` |

两种都要认。只搜顶层兄弟时，新系统上会退回「挂到 `Progman`」的兜底路径，
视频窗口反而排到壁纸层**下面**，被它不透明的背景整块盖住——
表现就是「进程在解码、内存涨了，桌面却一动不动」。

## 排查

壁纸不见了，先跑自检：

```bash
python tools/selfcheck.py
```

它会依次检查显示器列表、壁纸层 `WorkerW` 是否存在、视频窗口是否挂在正确的父窗口下，并打印运行日志尾部。
运行日志在程序目录下的 `video-wallpaper.log`。

常见情况：

| 现象 | 原因 / 处理 |
|---|---|
| 托盘图标在，但桌面没变化 | 跑自检看第 4 节。若没挂到壁纸层，退出重开即可 |
| 只有一块屏有壁纸 | 那块屏还没配视频，或它被拔过又插回来（重名会重新匹配）——托盘菜单里重新选一次 |
| 壁纸跑到别的显示器上 | 托盘 →「显示器 / 更换视频」重新指定一次 |
| 资源管理器重启后壁纸消失 | 程序每 5 秒自检一次会自动重挂；若没恢复，退出重开 |
| 玩游戏时壁纸卡住不再动 | 这是设计行为：检测到全屏窗口会自动暂停解码，退出全屏自动恢复 |
| 内存占用偏高 | 用上面的 ffmpeg 命令把视频转成屏幕分辨率 |
| 竖屏有黑边 | 用了 16:9 的横版视频。换成 9:16 的竖版视频 |

## 开发说明：几个容易踩的坑

写这类工具时踩到的坑，都已在代码里修掉，记录如下。

### 1. `0x052C` 创建 `WorkerW` 是**异步**的

发完消息立刻 `EnumWindows` 会枚举不到，必须轮询等待（本项目最多等 3 秒）。
少了重试就会直接找不到挂载点。

### 2. 子窗口的 `SetWindowPos` 用的是**父窗口客户区坐标**

视频窗口 `SetParent` 之后就成了子窗口，此时 `SetWindowPos` 的坐标**不是屏幕坐标**，而是相对于父窗口客户区的。
壁纸层 `WorkerW` 的客户区覆盖**整个虚拟桌面**，多显示器时它的左上角通常在负坐标
（例如副屏摆在 `(-1080, -728)`）。

不换算的症状：壁纸整体偏到副屏上、再向右下溢出到主屏。
修法：先算屏幕坐标，再用 `MapWindowPoints(NULL, parent_hwnd, ...)` 换算成父客户区坐标。

### 3. 主窗口对象必须持有引用，否则进程会**静默崩溃**

```python
WallpaperApp(app)                # ✗ 错误写法
wallpaper = WallpaperApp(app)    # ✓ 正确写法
```

第一种写法下实例没有任何引用，Python 的循环 GC 会回收它，
连带销毁视频窗口和 `QTimer`；在事件循环正使用定时器时销毁它，进程直接崩掉。
而且 `pythonw` / `--noconsole` 没有控制台，**崩溃是完全无声的**。

程序里同时安装了 `sys.excepthook` 把未捕获异常写进日志，避免以后再出现这种静默死亡。

### 4. ctypes 调 `EnumWindows` / `EnumChildWindows` 必须声明 `argtypes`

不声明参数类型时，回调函数指针可能被截断成 32 位，导致枚举结果不可信。
本项目所有 Win32 调用都显式声明了 `argtypes` / `restype`。

### 5. Win11 24H2+ 的壁纸层是 `Progman` 的**子窗口**，不是顶层兄弟

Win10 上 `0x052C` 之后的结构是 `Progman` → `WorkerW`（图标层）→ `WorkerW`（壁纸层），壁纸层是**顶层**窗口，
用 `FindWindowExW(NULL, 图标层宿主, "WorkerW", NULL)` 找它后面的兄弟即可。

Win11 24H2（实测 Build 26200）上，新建的 `WorkerW` 直接挂在 `Progman` 里，和 `SHELLDLL_DefView` 并列：

```
Progman 0x00010208  (1920×1080)
 ├ SHELLDLL_DefView  0x0001020C   ← 图标层，背景透明
 ├ WorkerW           0x00030B32   ← 壁纸层（Progman 的子窗口）
 └ 视频窗口          0x002708D0   ← 挂错位置时就在这儿，排在壁纸层下面
```

此时顶层兄弟搜索必然失败，`_icon_host()` 返回的是 `Progman` 本身，
于是退回「挂到 Progman」的兜底分支。而那块 `WorkerW` 是**不透明**的（桌面背景由它绘制），
视频被压在它下面 → 解码照跑、内存照涨、屏幕上一片旧壁纸。

`_pick_layer()` 因此会两种结构都试：先找顶层兄弟，再遍历 `Progman` 的直接子窗口找那块铺满主屏的 `WorkerW`，
并用「尺寸不小于主显示器」把输入法、托盘那些迷你 `WorkerW` 排除掉。

### 6. 判断「还挂着」不能只看父窗口是否存在

资源管理器重启后，系统会把孤儿子窗口重新挂到 `Progman` 上——父窗口依然有效，
但壁纸已经跑到壁纸层下面了。所以 `is_attached()` 改成沿父链追到**当初嵌入的那块壁纸层**，
对不上就交给看门狗重新挂载。

## 从源码打包

```bash
python tools/make_icon.py                       # 生成图标（纯代码绘制，无外部素材）
python -m PyInstaller video_wallpaper.spec --noconfirm
# 产物：dist/VideoWallpaper.exe
```

`video_wallpaper.spec` 里排除了大量用不到的 Qt 模块（WebEngine、Quick/QML、3D、Charts…），
否则体积会大好几倍。实测单文件 exe 54 MB，冷启动（含解包）约 1.4 秒。

> **打包环境必须装完整的 `PySide6`，不能只装 `PySide6-Essentials`。**
> 从 Qt 6.11 起，多媒体后端（`ffmpegmediaplugin.dll` + `avcodec` / `avformat` / `avutil`）
> 放在 **`PySide6-Addons`** 里，Essentials 只有 `QtMultimedia.pyd` 壳子。
> 只装 Essentials 也能打包成功、exe 也能启动，但 `QMediaPlayer` 没有解码后端，
> **壁纸是纯黑的，而且日志里一行报错都没有**——进程内存只有 80 MB 左右就是它（正常解码时几百 MB）。

## 环境要求

- Windows 10 / 11（x64）。开发环境为 Windows 10 22H2 Build 19045
- 从源码运行需 Python 3.9+ 与 PySide6 6.5+
- 打包 exe 需要 PyInstaller 6.x

## 已知限制

- 壁纸按显示器的**型号名**绑定，同一台机器的两块同型号显示器无法区分（会都匹配到同一个配置）
- 视频按 `KeepAspectRatio` 缩放，比例与屏幕不一致时会有黑边（16:9 视频配 16:9 屏幕则正好铺满）
- 竖屏显示器请配 9:16 的竖版视频，否则会留下大片黑边
