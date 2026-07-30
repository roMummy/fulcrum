from __future__ import annotations

import sys
from pathlib import Path

from PySide2.QtCore import QPoint, Qt
from PySide2.QtGui import QColor, QImage, QImageWriter, QLinearGradient, QPainter, QPen, QPolygon


def draw_launcher_icon(size: int) -> QImage:
    """绘制桌面 exe 使用的企业微信 Provider 图标。

    这里不直接复用 `launcher_app.py` 的 QPixmap 绘制函数，是为了让图标生成脚本
    不依赖 QApplication；构建机只需要普通命令行环境就能生成 Windows `.ico`。
    """
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, QColor("#12b886"))
    gradient.setColorAt(1, QColor("#1677ff"))
    painter.setBrush(gradient)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, size - 8, size - 8, int(size * 0.24), int(size * 0.24))

    # 白色气泡保留企业微信视觉联想，三个蓝点表达消息连接。
    painter.setBrush(QColor(255, 255, 255, 235))
    painter.drawRoundedRect(int(size * 0.22), int(size * 0.25), int(size * 0.48), int(size * 0.34), int(size * 0.11), int(size * 0.11))
    painter.drawPolygon(
        QPolygon(
            [
                QPoint(int(size * 0.37), int(size * 0.58)),
                QPoint(int(size * 0.45), int(size * 0.58)),
                QPoint(int(size * 0.36), int(size * 0.69)),
            ]
        )
    )

    painter.setBrush(QColor("#0d5bd7"))
    painter.setPen(Qt.NoPen)
    node_radius = max(2, int(size * 0.055))
    for x, y in ((0.34, 0.40), (0.50, 0.40), (0.66, 0.40)):
        painter.drawEllipse(int(size * x) - node_radius, int(size * y) - node_radius, node_radius * 2, node_radius * 2)

    # 右下角连接线表达 Provider/Hook 桥接能力，小尺寸下仍保持高对比。
    pen = QPen(QColor(255, 255, 255, 220), max(2, int(size * 0.035)))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(int(size * 0.50), int(size * 0.63), int(size * 0.72), int(size * 0.78))
    painter.drawLine(int(size * 0.72), int(size * 0.78), int(size * 0.82), int(size * 0.60))
    painter.setBrush(QColor(255, 255, 255, 245))
    painter.setPen(Qt.NoPen)
    for x, y in ((0.50, 0.63), (0.72, 0.78), (0.82, 0.60)):
        painter.drawEllipse(int(size * x) - node_radius, int(size * y) - node_radius, node_radius * 2, node_radius * 2)

    painter.end()
    return image


def main() -> int:
    output_path = Path(__file__).resolve().parent / "assets" / "workwechat-provider.ico"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = QImageWriter(str(output_path), b"ico")
    writer.setQuality(100)
    if not writer.write(draw_launcher_icon(256)):
        sys.stderr.write(f"failed to write icon: {writer.errorString()}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
