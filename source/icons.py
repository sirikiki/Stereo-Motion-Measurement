from i18n import tr, set_language
'Consistent stroke icons drawn natively, with HiDPI support.'
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
import math

def icon(name, color='#61718a'):
    result = QIcon()
    for scale in [1, 2, 3]:
        pix = QPixmap(24 * scale, 24 * scale)
        pix.fill(Qt.transparent)
        pix.setDevicePixelRatio(scale)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

        def line(x, y, a, b):
            p.drawLine(QPointF(x, y), QPointF(a, b))

        def circle(x, y, r):
            p.drawEllipse(QRectF(x - r, y - r, 2 * r, 2 * r))
        if name == 'eye':
            path = QPainterPath(QPointF(2, 12))
            path.cubicTo(7, 4, 17, 4, 22, 12)
            path.cubicTo(17, 20, 7, 20, 2, 12)
            p.drawPath(path)
            circle(12, 12, 3)
        elif name == 'eye_closed':
            path = QPainterPath(QPointF(2, 10))
            path.cubicTo(7, 18, 17, 18, 22, 10)
            p.drawPath(path)
            line(5, 13, 3, 17)
            line(9, 16, 8, 20)
            line(15, 16, 16, 20)
            line(19, 13, 21, 17)
        elif name == 'trash':
            line(3, 6, 21, 6)
            line(8, 6, 8, 3)
            line(8, 3, 16, 3)
            line(16, 3, 16, 6)
            path = QPainterPath(QPointF(5, 6))
            path.lineTo(6, 21)
            path.lineTo(18, 21)
            path.lineTo(19, 6)
            p.drawPath(path)
            line(10, 10, 10, 17)
            line(14, 10, 14, 17)
        elif name == 'timer':
            circle(12, 13, 7.5)
            line(9, 2, 15, 2)
            line(12, 2, 12, 5)
            line(19, 5, 21, 7)
            line(12, 13, 12, 8)
            line(12, 13, 15, 15)
        elif name == 'pendulum':
            line(4, 4, 20, 4)
            line(12, 4, 17, 16)
            circle(18, 19, 2.5)
            path = QPainterPath(QPointF(4, 17))
            path.quadTo(8, 22, 13, 21)
            p.drawPath(path)
        elif name == 'percent':
            circle(6, 6, 2.5)
            circle(18, 18, 2.5)
            line(5, 20, 19, 4)
        elif name == 'clipboard':
            p.drawRoundedRect(QRectF(5, 4, 14, 18), 2, 2)
            p.drawRoundedRect(QRectF(9, 2, 6, 5), 1, 1)
            line(9, 11, 15, 11)
            line(9, 15, 15, 15)
            line(9, 19, 13, 19)
        elif name == 'play':
            circle(12, 12, 9)
            path = QPainterPath(QPointF(10, 7))
            path.lineTo(17, 12)
            path.lineTo(10, 17)
            path.closeSubpath()
            p.drawPath(path)
        elif name == 'gear':
            circle(12, 12, 6)
            circle(12, 12, 2.5)
            for i in range(8):
                a = i * math.pi / 4
                line(12 + 6 * math.cos(a), 12 + 6 * math.sin(a), 12 + 9 * math.cos(a), 12 + 9 * math.sin(a))
        elif name == 'planes':
            path = QPainterPath(QPointF(2, 14))
            path.lineTo(12, 9)
            path.lineTo(22, 14)
            path.lineTo(12, 20)
            path.closeSubpath()
            p.drawPath(path)
            path = QPainterPath(QPointF(8, 4))
            path.lineTo(16, 2)
            path.lineTo(16, 17)
            path.lineTo(8, 20)
            path.closeSubpath()
            p.drawPath(path)
        elif name == 'camera':
            p.drawRoundedRect(QRectF(2, 7, 20, 14), 2, 2)
            circle(12, 14, 4)
            line(7, 7, 9, 3)
            line(9, 3, 15, 3)
            line(15, 3, 17, 7)
        elif name == 'back':
            line(5, 12, 21, 12)
            line(5, 12, 11, 6)
            line(5, 12, 11, 18)
        p.end()
        result.addPixmap(pix)
    return result
