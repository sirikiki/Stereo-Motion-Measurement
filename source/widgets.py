from i18n import tr, set_language
'Camera letterboxing and an interactive, gap-preserving OpenGL trajectory.'
import numpy as np
from PySide6.QtCore import Qt, QRectF, Signal, QPointF, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QVector3D, QFont, QMatrix4x4, QPolygonF
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStyledItemDelegate, QStyleOptionViewItem, QStyle
import pyqtgraph.opengl as gl
from coordinates import to_scene, CAMERA_TO_SCENE, first_origin
from filtering import ensure_filtered

class NoCellFocusDelegate(QStyledItemDelegate):

    def paint(self, painter, option, index):
        clean = QStyleOptionViewItem(option)
        clean.state &= ~QStyle.State_HasFocus
        super().paint(painter, clean, index)

class PositionSwitch(QWidget):
    """Keyboard-accessible animated two-position segmented control."""
    changed = Signal(bool)

    def __init__(self, labels=None, initial=True):
        super().__init__()
        self.relative = initial
        self._slide = float(initial)
        labels = labels or (tr('绝对位置'), tr('相对位置'))
        self.labels = labels
        self.setFixedSize(230, 36)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(' / '.join(labels))
        self.animation = QPropertyAnimation(self, b'slide', self)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.InOutCubic)

    def get_slide(self):
        return self._slide

    def set_slide(self, value):
        self._slide = value
        self.update()
    slide = Property(float, get_slide, set_slide)

    def set_relative(self, relative):
        if self.relative == relative:
            return
        self.relative = relative
        self.animation.stop()
        self.animation.setStartValue(self._slide)
        self.animation.setEndValue(float(relative))
        self.animation.start()
        self.changed.emit(relative)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.set_relative(e.position().x() >= self.width() / 2)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Left, Qt.Key_Right):
            self.set_relative(not self.relative if e.key() in (Qt.Key_Space, Qt.Key_Return) else e.key() == Qt.Key_Right)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#e7ecf4'))
        p.drawRoundedRect(QRectF(self.rect()), 10, 10)
        half = self.width() / 2
        p.setBrush(QColor('#ffffff'))
        p.drawRoundedRect(QRectF(3 + self._slide * half, 3, half - 6, self.height() - 6), 8, 8)
        for i, text in enumerate(self.labels):
            p.setPen(QColor('#2464c2' if i == int(self.relative) else '#7c889b'))
            p.drawText(QRectF(i * half, 0, half, self.height()), Qt.AlignCenter, text)

class CameraView(QWidget):
    chosen = Signal(float, float)

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.frame = None
        self.detection = None
        self.markers = True
        self.frame_rect = QRectF()
        self.setMinimumSize(160, 140)
        self.setCursor(Qt.CrossCursor)

    def set_frame(self, image, detection=None):
        rgb = np.ascontiguousarray(image[:, :, ::-1])
        self.frame = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
        self.detection = detection
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor('#f4f6f9'))
        if self.frame is None:
            p.setPen(QColor('#8994a6'))
            p.drawText(self.rect(), Qt.AlignCenter, tr('等待相机画面'))
            return
        scale = min(self.width() / self.frame.width(), self.height() / self.frame.height())
        w, h = (self.frame.width() * scale, self.frame.height() * scale)
        self.frame_rect = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        p.drawImage(self.frame_rect, self.frame)
        if self.markers and self.detection:
            x, y = self.detection['center']
            r = self.detection['radius_px'] * scale
            cx = self.frame_rect.left() + x * scale
            cy = self.frame_rect.top() + y * scale
            p.setPen(QPen(QColor('#19b477'), 2))
            p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
            p.drawLine(int(cx - 5), int(cy), int(cx + 5), int(cy))
            p.drawLine(int(cx), int(cy - 5), int(cx), int(cy + 5))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.frame is not None and self.frame_rect.contains(event.position()):
            pos = event.position()
            self.chosen.emit((pos.x() - self.frame_rect.left()) * self.frame.width() / self.frame_rect.width(), (pos.y() - self.frame_rect.top()) * self.frame.height() / self.frame_rect.height())

class PlaneView(CameraView):
    plane_chosen = Signal(str)
    plane_hovered = Signal(str)
    polygon_closed = Signal(object)
    drawing_started = Signal(int)

    def __init__(self, name, side):
        super().__init__(name)
        self.side = side
        self.overlays = []
        self.selecting = False
        self.active_candidate_id = None
        self.setMouseTracking(True)
        self.manual_mode = False
        self.draw_points = []
        self.draw_closed = False
        self.draw_pending = False
        self.setFocusPolicy(Qt.StrongFocus)

    def reset_polygon(self):
        self.draw_points = []
        self.draw_closed = False
        self.draw_pending = False
        self.update()

    def preview_id(self):
        ids = [i['plane']['id'] for i in self.overlays if i['color'] == '#dc4b56']
        return self.active_candidate_id if self.active_candidate_id in ids else next(iter(ids), None)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.frame is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        scale = self.frame_rect.width() / self.frame.width()

        def screen(a):
            return QPointF(self.frame_rect.left() + float(a[0]) * scale, self.frame_rect.top() + float(a[1]) * scale)
        p.setClipRect(self.frame_rect)
        number = 0
        for item in self.overlays:
            plane = item['plane']
            candidate = item['color'] == '#dc4b56'
            active = plane['id'] == self.preview_id()
            p.setPen(QPen(QColor(item['color'] if not candidate or active else '#d99299'), 2 if not candidate or active else 1))
            polygon = QPolygonF([screen(a) for a in plane['polygons'][self.side]])
            if plane.get('draw_boundary', True):
                p.drawPolygon(polygon)
            if not candidate or active:
                for a, b in plane['grids'][self.side]:
                    if np.isfinite([a, b]).all():
                        p.drawLine(screen(a), screen(b))
            if candidate:
                number += 1
                if len(polygon):
                    p.drawText(polygon.boundingRect().topLeft() + QPointF(5, 18), str(number))
        if self.manual_mode and self.draw_points:
            p.setPen(QPen(QColor('#dc4b56'), 2))
            p.setBrush(Qt.NoBrush)
            polygon = QPolygonF([screen(a) for a in self.draw_points])
            if self.draw_closed:
                p.drawPolygon(polygon)
            else:
                p.drawPolyline(polygon)
            p.setBrush(QColor('#dc4b56'))
            for i, a in enumerate(self.draw_points):
                pt = screen(a)
                p.drawEllipse(pt, 5 if i == 0 else 3, 5 if i == 0 else 3)

    def candidate_at(self, event):
        if not self.selecting or self.frame is None or (not self.frame_rect.contains(event.position())):
            return None
        import cv2
        point = ((event.position().x() - self.frame_rect.left()) * self.frame.width() / self.frame_rect.width(), (event.position().y() - self.frame_rect.top()) * self.frame.height() / self.frame_rect.height())
        hits = [item['plane'] for item in self.overlays if item['color'] == '#dc4b56' and cv2.pointPolygonTest(np.array(item['plane']['polygons'][self.side], np.float32), point, False) >= 0]
        return min(hits, key=lambda a: abs(cv2.contourArea(np.array(a['polygons'][self.side], np.float32))))['id'] if hits else None

    def mouseMoveEvent(self, event):
        if self.manual_mode:
            return
        identifier = self.candidate_at(event)
        if identifier:
            self.active_candidate_id = identifier
            self.plane_hovered.emit(identifier)
            self.update()

    def mousePressEvent(self, event):
        if self.manual_mode:
            if event.button() != Qt.RightButton or not self.selecting or self.draw_pending or (self.frame is None) or (not self.frame_rect.contains(event.position())):
                return
            self.setFocus()
            r = self.frame_rect
            point = [(event.position().x() - r.left()) * self.frame.width() / r.width(), (event.position().y() - r.top()) * self.frame.height() / r.height()]
            if self.draw_closed:
                self.reset_polygon()
            if not self.draw_points:
                self.drawing_started.emit(self.side)
            if len(self.draw_points) >= 3 and np.linalg.norm(np.array(point) - self.draw_points[0]) * r.width() / self.frame.width() <= 12:
                self.draw_closed = True
                self.draw_pending = True
                self.polygon_closed.emit(dict(side=self.side, points=list(self.draw_points)))
            elif len(self.draw_points) < 64:
                self.draw_points.append(point)
            self.update()
            return
        if event.button() == Qt.LeftButton:
            identifier = self.candidate_at(event)
            if identifier:
                self.plane_chosen.emit(identifier)

    def keyPressEvent(self, event):
        if self.manual_mode and (not self.draw_pending):
            if event.key() == Qt.Key_Escape:
                self.reset_polygon()
                return
            if event.key() == Qt.Key_Backspace:
                self.draw_closed = False
                if self.draw_points:
                    self.draw_points.pop()
                self.update()
                return
        super().keyPressEvent(event)

class AxisOverlay(QWidget):

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(QFont('Microsoft YaHei', 9))
        viewport = (0, 0, max(1, self.width()), max(1, self.height()))
        transform = self.view.projectionMatrix(viewport, viewport) * self.view.viewMatrix()
        labels = [([108, 0, 0], tr('X 右 +'), '#cf4e59'), ([-108, 0, 0], tr('左 −'), '#cf4e59'), ([0, 108, 0], tr('Y 前 / 靠近 +'), '#238e65'), ([0, -108, 0], tr('后 / 远离 −'), '#238e65'), ([0, 0, 108], tr('Z 上 +'), '#286bd4'), ([0, 0, -108], tr('下 −'), '#286bd4')]
        labels += getattr(self.view, 'reference_labels', [])
        for position, text, color in labels:
            v = transform.map(QVector3D(*position))
            x = (v.x() + 1) * self.width() / 2
            y = (1 - v.y()) * self.height() / 2
            if -1 <= v.z() <= 1 and 0 <= x <= self.width() and (0 <= y <= self.height()):
                metrics = p.fontMetrics()
                w = metrics.horizontalAdvance(text)
                r = QRectF(max(2, min(self.width() - w - 8, x - w / 2)), max(2, min(self.height() - 22, y - 9)), w + 8, 20)
                p.fillRect(r, QColor(251, 252, 254, 230))
                p.setPen(QColor(color))
                p.drawText(r, Qt.AlignCenter, text)

class OrbitView(gl.GLViewWidget):

    def __init__(self):
        super().__init__()
        self.axis_overlay = AxisOverlay(self)
        self.auto_follow = False

    def mouseMoveEvent(self, event):
        if event.buttons():
            self.auto_follow = False
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self.auto_follow = False
        super().wheelEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.axis_overlay.setGeometry(self.rect())

    def projectionMatrix(self, region, viewport):
        x, y, w, h = viewport
        w = max(w, 1)
        h = max(h, 1)
        distance = max(float(self.opts['distance']), 0.001)
        r = distance * np.tan(np.radians(self.opts['fov'] / 2))
        t = r * h / w
        left = r * ((region[0] - x) * 2 / w - 1)
        right = r * ((region[0] + region[2] - x) * 2 / w - 1)
        bottom = t * ((region[1] - y) * 2 / h - 1)
        top = t * ((region[1] + region[3] - y) * 2 / h - 1)
        m = QMatrix4x4()
        m.ortho(left, right, bottom, top, distance * 0.001, distance * 1000)
        return m

    def paintGL(self, *args, **kwargs):
        super().paintGL(*args, **kwargs)
        self.axis_overlay.update()

    def mouseDoubleClickEvent(self, event):
        if hasattr(self, 'reset_callback'):
            self.reset_callback()

class Trajectory(QWidget):
    expand_requested = Signal()
    mode_changed = Signal(bool)

    def __init__(self, expandable=True):
        super().__init__()
        self.setObjectName('card')
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 8)
        bar = QHBoxLayout()
        self.expand = QPushButton('⛶')
        self.expand.setObjectName('iconButton')
        self.expand.setFixedSize(32, 28)
        self.expand.setToolTip(tr('放大三维图 / 恢复（Esc）'))
        self.expand.clicked.connect(self.expand_requested.emit)
        self.expand.setVisible(expandable)
        bar.addWidget(self.expand)
        bar.addWidget(QLabel(tr('三维轨迹')))
        bar.addStretch()
        self.caption = QLabel(tr('左键旋转 · 中键平移 · 滚轮缩放 · 双击复位'))
        self.caption.setObjectName('muted')
        bar.addWidget(self.caption)
        layout.addLayout(bar)
        presets = QHBoxLayout()
        presets.addStretch()
        self.center_button = QPushButton(tr('轨迹居中'))
        self.center_button.clicked.connect(lambda: self.fit_trajectory(True))
        presets.addWidget(self.center_button)
        self.reference_button = QPushButton(tr('相机原点'))
        self.reference_button.setCheckable(True)
        self.reference_button.setChecked(True)
        self.reference_button.setObjectName('viewToggle')
        self.reference_button.toggled.connect(self.set_reference_visible)
        presets.addWidget(self.reference_button)
        self.follow_button = QPushButton(tr('轨迹追踪'))
        self.follow_button.setCheckable(True)
        self.follow_button.setObjectName('viewToggle')
        self.follow_button.toggled.connect(self.follow_latest)
        presets.addWidget(self.follow_button)
        for title, azimuth, elevation in [(tr('正视 XZ'), -90, 0), (tr('俯视 XY'), -90, 90), (tr('侧视 YZ'), 0, 0)]:
            b = QPushButton(title)
            b.setStyleSheet('padding: 3px 9px; font-size: 11px;')
            b.clicked.connect(lambda checked=False, a=azimuth, e=elevation: self.view.setCameraPosition(azimuth=a, elevation=e))
            presets.addWidget(b)
        layout.addLayout(presets)
        self.view = OrbitView()
        self.view.setBackgroundColor('#fbfcfe')
        self.view.reset_callback = self.reset_view
        self.view.setMinimumHeight(150)
        self.view.setCameraPosition(distance=850, elevation=24, azimuth=-90)
        layout.addWidget(self.view, 1)
        self.grid = gl.GLGridItem()
        self.grid.setSize(400, 400)
        self.grid.setSpacing(25, 25)
        self.grid.setColor((184, 196, 214, 110))
        self.view.addItem(self.grid)
        for end, color, label in [([90, 0, 0], (0.9, 0.35, 0.36, 1), 'X'), ([0, 90, 0], (0.22, 0.65, 0.45, 1), 'Y'), ([0, 0, 90], (0.2, 0.48, 0.92, 1), 'Z')]:
            axis = gl.GLLinePlotItem(pos=np.array([-np.array(end), end]), color=color, width=1.5, antialias=True)
            self.view.addItem(axis)
        self.line = gl.GLLinePlotItem(pos=np.empty((0, 3)), color=(0.18, 0.43, 0.9, 0.85), width=2, mode='lines', antialias=True)
        self.dot = gl.GLScatterPlotItem(pos=np.empty((0, 3)), color=(0.13, 0.45, 0.95, 1), size=9, pxMode=True, glOptions='translucent')
        self.view.addItem(self.line)
        self.view.addItem(self.dot)
        self.foot = QLabel(tr('等待有效测量 · 原始坐标不随视角改变'))
        self.foot.setObjectName('muted')
        self.foot.hide()
        bottom = QHBoxLayout()
        legend = QLabel(tr('X右＋ · Y靠近＋ · Z上＋'))
        legend.setObjectName('muted')
        bottom.addWidget(legend)
        bottom.addStretch()
        self.position_switch = PositionSwitch()
        bottom.addWidget(self.position_switch)
        layout.addLayout(bottom)
        self.denoise_switch = PositionSwitch((tr('降噪关'), tr('降噪开')), False)
        bottom.insertWidget(2, self.denoise_switch)
        self.denoise_switch.setToolTip(tr('One Euro自适应低通；会有延迟，不能判断动作意图。原始测量始终保存。'))
        self.denoise_switch.changed.connect(self.change_denoise)
        self.position_switch.changed.connect(self.change_mode)
        self.origin_dot = gl.GLScatterPlotItem(pos=np.empty((0, 3)), color=(1.0, 0.58, 0.15, 1.0), size=10, pxMode=True)
        self.view.addItem(self.origin_dot)
        self.reference_line = gl.GLLinePlotItem(pos=np.empty((0, 3)), color=(0.8, 0.53, 0.2, 0.7), width=1.2, mode='lines', antialias=True)
        self.camera_dot = gl.GLScatterPlotItem(pos=np.empty((0, 3)), color=(0.45, 0.48, 0.55, 1), size=8, pxMode=True)
        self.view.addItem(self.reference_line)
        self.view.addItem(self.camera_dot)
        self.anchor = None
        self.cached_samples = []
        self.relative = True
        self.denoise = False
        self.last_points = np.empty((0, 3))
        self.camera_labels = []
        self.rotation = CAMERA_TO_SCENE.copy()
        self.frame_label = tr('相机方向转换')

    def set_coordinate_frame(self, rotation, name):
        self.rotation = np.asarray(rotation)
        self.frame_label = name
        if self.cached_samples:
            self.set_samples(self.cached_samples)
        self.reset_view()

    def change_mode(self, relative):
        self.relative = relative
        self.set_samples(self.cached_samples)
        self.reset_view()
        self.mode_changed.emit(relative)

    def change_denoise(self, value):
        self.denoise = value
        self.set_samples(self.cached_samples)
        self.mode_changed.emit(self.relative)

    def fit_trajectory(self, force=False):
        if force:
            self.follow_button.setChecked(False)
        points = self.last_points[np.isfinite(self.last_points).all(axis=1)]
        if not len(points):
            return
        self.fit_points(points)

    def set_reference_visible(self, visible):
        self.reference_line.setVisible(visible)
        self.camera_dot.setVisible(visible)
        self.view.reference_labels = self.camera_labels if visible else []
        self.view.update()

    def follow_latest(self, *args):
        if not self.follow_button.isChecked() or not len(self.last_points):
            return
        point = self.last_points[-1]
        if np.isfinite(point).all():
            self.view.opts['center'] = QVector3D(*point)
            self.view.update()

    def fit_points(self, points):
        low, high = (points.min(axis=0), points.max(axis=0))
        center = (low + high) / 2
        radius = max(80, float(np.linalg.norm(high - low)) / 2)
        distance = radius * 1.3 * max(1, self.view.width() / max(1, self.view.height())) / np.tan(np.radians(self.view.opts['fov'] / 2))
        self.view.opts['center'] = QVector3D(*center)
        self.view.setCameraPosition(distance=max(350, distance))

    def reset_view(self):
        self.view.setCameraPosition(distance=850, elevation=24, azimuth=-90)
        self.view.opts['center'] = QVector3D(0, 0, 0)
        if self.follow_button.isChecked():
            self.follow_latest()
        else:
            self.fit_trajectory()
        self.view.update()

    def displayed_point(self, xyz):
        point = np.asarray(xyz) * 1000
        if self.relative:
            if self.anchor is None:
                return np.full(3, np.nan)
            point = point - self.anchor
        return to_scene(point, self.rotation)

    def reset(self):
        self.anchor = None
        self.cached_samples = []
        self.last_points = np.empty((0, 3))
        self.line.setData(pos=np.empty((0, 3)))
        self.dot.setData(pos=np.empty((0, 3)))
        self.origin_dot.setData(pos=np.empty((0, 3)))
        self.reset_view()
        self.reference_line.setData(pos=np.empty((0, 3)))
        self.camera_dot.setData(pos=np.empty((0, 3)))
        self.camera_labels = []
        self.view.reference_labels = []
        self.foot.setText(tr('等待本轮第一个有效测量点作为起点'))

    def set_samples(self, samples, seconds=None):
        self.cached_samples = samples
        first_display = not len(self.last_points)
        if not samples:
            self.line.setData(pos=np.empty((0, 3)))
            self.dot.setData(pos=np.empty((0, 3)))
            self.last_points = np.empty((0, 3))
            return
        if self.anchor is None:
            origin = first_origin(samples)
            if origin is not None:
                self.anchor = origin * 1000
                self.reset_view()
        if self.anchor is None:
            self.dot.setData(pos=np.empty((0, 3)))
            return
        if self.denoise:
            ensure_filtered(samples)
        key = 'filtered_xyz' if self.denoise else 'xyz'
        raw = np.array([s[key] if s['valid'] and s.get('environment_valid', True) else [np.nan] * 3 for s in samples]) * 1000
        pts = to_scene(raw - (self.anchor if self.relative else 0), self.rotation)
        self.last_points = pts
        finite = np.isfinite(pts).all(axis=1)
        ok = finite[:-1] & finite[1:]
        segments = np.stack([pts[:-1][ok], pts[1:][ok]], axis=1).reshape(-1, 3) if len(pts) > 1 else np.empty((0, 3))
        self.line.setData(pos=segments)
        self.dot.setData(pos=pts[-1:] if finite[-1] else np.empty((0, 3)))
        self.origin_dot.setData(pos=np.zeros((1, 3)) if self.relative else to_scene(self.anchor, self.rotation).reshape(1, 3))
        absolute_start = to_scene(self.anchor, self.rotation)
        camera = -absolute_start if self.relative else np.zeros(3)
        start = np.zeros(3) if self.relative else absolute_start
        count = max(2, int(np.linalg.norm(start - camera) / 12))
        fractions = np.linspace(0, 1, count + 1)
        guide = camera + fractions[:, None] * (start - camera)
        segments = guide[:len(guide) // 2 * 2]
        self.reference_line.setData(pos=segments)
        self.camera_dot.setData(pos=camera.reshape(1, 3))
        self.camera_labels = [(camera.tolist(), tr('相机原点'), '#7e8797')]
        self.set_reference_visible(self.reference_button.isChecked())
        if first_display and (not self.follow_button.isChecked()):
            self.fit_trajectory()
        self.follow_latest()
        mode = tr('相对位置 · 本轮首个有效点为零点') if self.relative else tr('绝对位置 · 校正相机光心为零点')
        self.foot.setText(tr('全程 {0} 帧 · {1} · ').format(len(samples), mode) + (tr('降噪估计') if self.denoise else tr('原始测量')))
