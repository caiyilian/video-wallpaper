# -*- coding: utf-8 -*-
"""生成程序图标 assets/video-wallpaper.ico（多尺寸，纯代码绘制，无外部素材）。

用法：

    python tools/make_icon.py

Qt 自身不支持写 .ico，所以这里用「PNG 塞进 ICO 容器」的标准做法手工封装
（Vista 以后的 ICO 都支持内嵌 PNG）。
"""

from __future__ import annotations

import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "video-wallpaper.ico",
)


def render(size: int) -> bytes:
    """按给定边长画一张图标，返回 PNG 字节。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    k = size / 64.0                      # 以 64×64 为设计基准等比缩放
    pad = 3 * k
    radius = 14 * k

    path = QPainterPath()
    path.addRoundedRect(pad, pad, size - 2 * pad, size - 2 * pad, radius, radius)
    p.fillPath(path, QColor("#2b2f36"))
    p.setPen(QColor("#5ac8fa"))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5ac8fa"))
    p.drawPolygon(QPolygonF([
        QPointF(25 * k, 20 * k),
        QPointF(25 * k, 44 * k),
        QPointF(46 * k, 32 * k),
    ]))
    p.end()

    ba = QByteArray()
    buf = QBuffer(ba)                     # ba 必须活着，否则 QBuffer 内部指针悬空
    buf.open(QIODevice.WriteOnly)
    ok = pm.save(buf, "PNG")
    buf.close()
    if not ok:
        raise RuntimeError("QPixmap 保存 PNG 失败")
    return bytes(ba)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """把若干张 PNG 封成 ICO 容器。"""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    payload = b""
    offset = 6 + 16 * len(images)

    for size, png in images:
        dim = 0 if size >= 256 else size       # 256 在 ICO 里记 0
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(payload)
        )
        payload += png

    return header + entries + payload


def main() -> int:
    # 必须持有引用：QApplication 被 GC 回收后再用 QPixmap/QPainter 会直接段错误
    app = QApplication([])
    images = [(s, render(s)) for s in SIZES]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as fp:
        fp.write(build_ico(images))
    print(f"已生成 {OUT}  （{len(SIZES)} 种尺寸，{os.path.getsize(OUT) / 1024:.1f} KB）")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
