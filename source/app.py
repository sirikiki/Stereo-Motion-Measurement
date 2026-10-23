from i18n import tr, set_language
'Stereo Motion desktop preview, portable and usable without a camera on launch.'
import argparse
import bisect
import json
import logging
import hashlib
import os
import sys
import time
from collections import deque
from pathlib import Path
os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PySide6')
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QUrl, QSize
from PySide6.QtGui import QFont, QDesktopServices, QKeySequence, QShortcut, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QStackedWidget, QSplitter, QFrame, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QFormLayout, QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QFileDialog, QMessageBox, QSlider, QScrollArea
from storage import ROOT, settings, write_json, read_session, statistics
from capture import CaptureWorker
from widgets import CameraView, PlaneView, Trajectory, NoCellFocusDelegate, PositionSwitch
from environment import EnvironmentWorker
from icons import icon
from filtering import ensure_filtered
from record_management import move_to_bin, restore_batch
from coordinates import CAMERA_TO_SCENE, to_scene, SUBJECT_FROM_OBSERVER, first_origin
STYLE = '\nQMainWindow, QWidget { font-family: "Microsoft YaHei"; font-size: 13px; color: #243248; }\nQMainWindow, #shell { background: #f4f6fa; }\n#header, #footer { background: #ffffff; border: 0; }\n#sidebar { background: #edf1f7; border-right: 1px solid #dce3ed; }\n#card { background: white; border: 1px solid #e1e7ef; border-radius: 12px; }\n#title { font-size: 19px; font-weight: 600; color: #15243b; }\n#pageTitle { font-size: 24px; font-weight: 600; }\n#muted { color: #7d899b; font-size: 12px; }\n#coordinate { font-size: 21px; font-weight: 600; color: #183962; }\n#badge { color: #466485; background: #edf3fc; border-radius: 10px; padding: 6px 12px; }\nQLabel { background: transparent; border: none; }\nQPushButton { background: white; border: 1px solid #dbe2ec; border-radius: 8px; padding: 8px 16px; }\nQPushButton:hover { background: #eaf1fc; border-color: #adc5ed; }\nQPushButton:pressed { background: #dbe7fa; }\nQPushButton:disabled { color: #a8b2c0; background: #eef1f5; border-color: #e3e8f0; }\nQPushButton#primary { background: #3274df; color: white; border: none; }\nQPushButton#start { background: #14875e; color: white; border: none; padding: 11px 24px; }\nQPushButton#stop { background: #c74852; color: white; border: none; padding: 11px 24px; }\nQPushButton#start:disabled, QPushButton#stop:disabled { background: #e7ebf0; color: #98a2b1; }\nQPushButton#nav { text-align: left; border: none; background: transparent; padding: 11px 16px; }\nQPushButton#nav:checked { background: #dce8fb; color: #245cb0; font-weight: 600; }\nQPushButton#nav:hover { background: #e2e9f4; }\nQPushButton#viewToggle:checked { background: #3274df; color: white; border-color: #3274df; }\nQPushButton#iconButton { padding: 0; font-size: 19px; background: transparent; border: none; }\nQCheckBox { spacing: 9px; padding: 5px 0; }\nQCheckBox::indicator { width: 16px; height: 16px; }\nQLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QPlainTextEdit { background: white; border: 1px solid #dbe2ec; border-radius: 6px; padding: 7px; }\nQTableWidget,QListWidget { background: white; border: 1px solid #e1e7ef; border-radius: 8px; gridline-color: #eef1f6; selection-background-color: #dfebfe; selection-color: #244b87; }\nQTableWidget::item { padding: 7px; }\nQHeaderView::section { background: #f0f4fa; color: #61718a; border: none; padding: 10px 8px; }\nQSplitter::handle { background: #e1e7f0; height: 5px; width: 5px; }\nQSlider::groove:horizontal { background: #dce4ef; height: 5px; border-radius: 2px; }\nQSlider::handle:horizontal { background: #3477e0; width: 14px; margin: -5px 0; border-radius: 7px; }\nQScrollArea { border: none; background: transparent; }\n'

def label(text, object_name=None):
    widget = QLabel(text)
    if len(text) > 65 or text in (tr('黑圆标记 · 视觉测量'), tr('球心与IMU功能后续接入')):
        widget.setWordWrap(True)
    if object_name:
        widget.setObjectName(object_name)
    return widget

def button(text, callback, object_name=None):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    if object_name:
        widget.setObjectName(object_name)
    return widget

def card():
    widget = QFrame()
    widget.setObjectName('card')
    box = QVBoxLayout(widget)
    box.setContentsMargins(18, 16, 18, 16)
    box.setSpacing(12)
    return (widget, box)

def page(title, subtitle):
    widget = QWidget()
    box = QVBoxLayout(widget)
    box.setContentsMargins(22, 18, 22, 18)
    box.setSpacing(14)
    box.addWidget(label(title, 'pageTitle'))
    sub = label(subtitle, 'muted')
    sub.setWordWrap(True)
    box.addWidget(sub)
    return (widget, box)

def table(headers):
    w = QTableWidget(0, len(headers))
    w.setHorizontalHeaderLabels(headers)
    w.setEditTriggers(QAbstractItemView.NoEditTriggers)
    w.setSelectionBehavior(QAbstractItemView.SelectRows)
    w.setSelectionMode(QAbstractItemView.SingleSelection)
    w.verticalHeader().hide()
    w.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    w.setAlternatingRowColors(True)
    return w

class CenteredAxisItem(pg.AxisItem):
    """Relayout rotated text when its content changes without an axis resize."""
    def _updateLabel(self):
        super()._updateLabel()
        self.resizeEvent()


def application_icon():
    for path in (ROOT / 'assets/app.ico', Path(__file__).resolve().parent / 'assets/app.ico'):
        candidate = QIcon(str(path))
        if not candidate.isNull():
            return candidate
    return QIcon()


def configure_windows_identity():
    if sys.platform != 'win32':
        return
    import ctypes
    result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Siriy.StereoMotionMeasurement')
    if result != 0:
        logging.warning('Windows app identity returned %s', result)


class Window(QMainWindow):

    def __init__(self):
        super().__init__()
        for p in ['config', 'data/sessions', 'data/history', 'logs', 'calibration']:
            (ROOT / p).mkdir(parents=True, exist_ok=True)
        self.config = settings()
        self.config_warning = self.config.pop('_load_warning', '')
        self.worker = None
        self.last_session = None
        self.live = deque()
        self.last_packet = None
        self.detecting = False
        self.pending_start = False
        self.stopping_record = False
        self.camera_ready = False
        self.camera_closing = False
        self.was_stopped = False
        self.curve_from_replay = False
        self.env_worker = None
        self.env_snapshot = None
        self.env_state = {}
        self.last_env_submit = 0
        self.environment_invalid = False
        self.session_rotation = None
        self.display_rotation = CAMERA_TO_SCENE.copy()
        self.history_rotation = self.display_rotation.copy()
        self.replay_rotation = self.display_rotation.copy()
        self.frame_metadata = {}
        self.history_samples = []
        self.history_report = {}
        self.history_path = None
        self.replay_samples = []
        self.replay_report = {}
        self.replay_times = []
        self.replay_index = 0
        self.playing = False
        self.replay_base = 0
        self.play_started = 0
        self.expanded = False
        self.stage_busy = False
        self.last_chart = 0
        self._curve_key = None
        self.current_error = None
        self.normal_split_sizes = [310, 370]
        self.setWindowTitle(tr('双目运动测量'))
        self.setWindowIcon(application_icon())
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, screen.width() - 40), min(900, screen.height() - 60))
        self.setMinimumSize(1000, 680)
        shell = QWidget()
        shell.setObjectName('shell')
        self.setCentralWidget(shell)
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName('header')
        bar = QHBoxLayout(header)
        bar.setContentsMargins(20, 12, 22, 12)
        self.menu = button('☰', self.toggle_sidebar, 'iconButton')
        self.menu.setFixedSize(34, 32)
        bar.addWidget(self.menu)
        bar.addSpacing(8)
        self.brand_title = label(tr('双目运动测量'), 'title')
        bar.addWidget(self.brand_title)
        bar.addStretch()
        self.camera_button = button(tr('相机开启'), self.toggle_camera)
        self.camera_button.setIcon(icon('camera', '#c74852'))
        self.camera_button.setIconSize(QSize(20, 20))
        self.camera_button.setToolTip(tr('开启预览并选择目标；不开始检测或保存实验'))
        bar.addWidget(self.camera_button)
        bar.addSpacing(10)
        self.state = label(tr('未连接 · 等待开始'), 'badge')
        bar.addWidget(self.state)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName('sidebar')
        self.sidebar.setFixedWidth(250)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(14, 22, 14, 18)
        side.setSpacing(5)
        side.addWidget(label(tr('工作空间'), 'muted'))
        side.addSpacing(8)
        self.nav = []
        self.page_ids = ['monitor', 'environment', 'curves', 'precision', 'records', 'replay', 'camera_setup', 'settings']
        self.nav_icons = ['eye', 'planes', 'timer', 'percent', 'clipboard', 'play', 'camera', 'gear']
        for i, name in enumerate([tr('监视'), tr('环境基准'), tr('时间曲线'), tr('精度验证'), tr('实验记录'), tr('数据回放'), tr('相机配置'), tr('设置')]):
            b = button(name, lambda checked=False, n=self.page_ids[i]: self.navigate(n), 'nav')
            b.setCheckable(True)
            b.setIcon(icon(self.nav_icons[i]))
            b.setIconSize(QSize(19, 19))
            self.nav.append(b)
            side.addWidget(b)
        side.addSpacing(22)
        side.addWidget(label(tr('监视显示'), 'muted'))
        self.toggles = {}
        for key, title in [('images', tr('双目影像')), ('trajectory', tr('三维轨迹')), ('xyz', tr('XYZ坐标')), ('markers', tr('检测标记'))]:
            c = QPushButton(title)
            c.setCheckable(True)
            c.setChecked(True)
            c.setIcon(icon('eye'))
            c.setObjectName('nav')
            c.toggled.connect(lambda checked, w=c: (w.setIcon(icon('eye' if checked else 'eye_closed')), self.apply_visibility()))
            self.toggles[key] = c
            side.addWidget(c)
        side.addStretch()
        side.addWidget(label(tr('黑圆标记 · 视觉测量'), 'muted'))
        side.addWidget(label(tr('球心与IMU功能后续接入'), 'muted'))
        side.addWidget(label('A software made by Siri.y', 'muted'))
        body.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.make_monitor()
        self.make_environment()
        self.make_curves()
        self.make_precision()
        self.make_records()
        self.make_replay()
        self.make_camera_setup()
        self.make_settings()
        self.apply_display_frame()
        footer = QFrame()
        footer.setObjectName('footer')
        fb = QHBoxLayout(footer)
        fb.setContentsMargins(22, 12, 22, 12)
        self.status = label(tr('相机未启动  ·  标定待加载'), 'muted')
        fb.addWidget(self.status, 1)
        self.start_btn = button(tr('开始检测'), self.start_capture, 'start')
        self.stop_btn = button(tr('停止检测'), self.stop_capture, 'stop')
        self.stop_btn.setEnabled(False)
        fb.addWidget(self.start_btn)
        fb.addWidget(self.stop_btn)
        outer.addWidget(footer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)
        from windows_taskbar import TaskbarGuard
        self.taskbar_guard = TaskbarGuard(self, ROOT)
        QShortcut(QKeySequence('Escape'), self, activated=self.escape_view)
        self.navigate('monitor')
        self.refresh_records()
        if self.config_warning:
            self.status.setText(self.config_warning)

    def make_monitor(self):
        self.monitor = QWidget()
        box = QVBoxLayout(self.monitor)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)
        self.coordinates = QFrame()
        self.coordinates.setObjectName('card')
        row = QHBoxLayout(self.coordinates)
        row.setContentsMargins(20, 13, 20, 13)
        row.addWidget(label(tr('显示坐标  /  mm'), 'muted'))
        row.addSpacing(20)
        self.xyz_labels = []
        for axis in 'XYZ':
            w = label(f'{axis}  —', 'coordinate')
            w.setMinimumWidth(150)
            row.addWidget(w)
            self.xyz_labels.append(w)
        row.addStretch()
        self.tracking = label(tr('未检测'), 'muted')
        row.addWidget(self.tracking)
        box.addWidget(self.coordinates)
        self.monitor_split = QSplitter(Qt.Vertical)
        self.images = QWidget()
        ib = QHBoxLayout(self.images)
        ib.setContentsMargins(0, 0, 0, 0)
        ib.setSpacing(12)
        self.cameras = []
        for i, title in enumerate([tr('左相机'), tr('右相机')]):
            panel, pb = card()
            pb.setContentsMargins(12, 10, 12, 10)
            head = QHBoxLayout()
            head.addWidget(label(title))
            head.addStretch()
            head.addWidget(label(tr('校正画面'), 'muted'))
            pb.addLayout(head)
            camera = CameraView(title)
            camera.chosen.connect(lambda x, y, n=i: self.choose_target(n, x, y))
            pb.addWidget(camera, 1)
            self.cameras.append(camera)
            ib.addWidget(panel, 1)
        self.monitor_split.addWidget(self.images)
        self.trajectory = Trajectory()
        self.trajectory.expand_requested.connect(self.toggle_expand)
        self.trajectory.mode_changed.connect(self.position_mode_changed)
        self.monitor_split.addWidget(self.trajectory)
        self.monitor_split.setSizes([310, 370])
        self.monitor_split.setChildrenCollapsible(False)
        box.addWidget(self.monitor_split, 1)
        self.empty = label(tr('显示区域已隐藏 · 检测与保存仍可继续'), 'muted')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        box.addWidget(self.empty, 1)
        self.pages.addWidget(self.monitor)

    def make_environment(self):
        widget, box = page(tr('环境基准'), tr('定义水平和竖直方向。自动：左键选择稳定网格。手动：选择类别后右键圈出大致位置，点回首点闭合，软件在附近寻找平面。网格表示拟合方向与有效区域，不代表完整桌墙边界。'))
        mode_row = QHBoxLayout()
        self.env_board = button(tr('棋盘格辅助'), self.environment_board_changed)
        self.env_board.setCheckable(True)
        self.env_board.setToolTip(tr('自动识别棋盘格；可选输入方格列数、行数和格长。格长不是定向必需项。'))
        mode_row.addWidget(self.env_board)
        self.board_fields = QScrollArea()
        self.board_fields.setWidgetResizable(True)
        self.board_fields.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.board_fields.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.board_fields.setFixedHeight(53)
        board_policy = self.board_fields.sizePolicy()
        board_policy.setRetainSizeWhenHidden(True)
        self.board_fields.setSizePolicy(board_policy)
        self.board_fields.setMinimumWidth(0)
        editor = QWidget()
        self.board_fields.setWidget(editor)
        self.board_fields.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        self.board_fields.viewport().setAutoFillBackground(False)
        editor.setAutoFillBackground(False)
        editor.setObjectName('boardInputEditor')
        editor.setStyleSheet("QWidget#boardInputEditor { background: transparent; } QLineEdit { background: white; }")
        fields = QHBoxLayout(editor)
        fields.setSpacing(5)
        fields.setContentsMargins(0, 0, 0, 0)
        self.board_cols = QLineEdit()
        self.board_rows = QLineEdit()
        self.board_square = QLineEdit()
        self.board_square_height = QLineEdit()
        from PySide6.QtGui import QIntValidator, QDoubleValidator
        for field in (self.board_cols, self.board_rows):
            field.setValidator(QIntValidator(0, 50, field))
            field.setPlaceholderText(tr('自动'))
            field.setToolTip(tr('黑白方格数量，不是内角点。至少4；0表示自动。'))
        counts_label = label(tr('方格数'))
        counts_label.setToolTip(tr('方格数量（列×行）'))
        fields.addWidget(counts_label)
        fields.addWidget(self.board_cols)
        fields.addWidget(label('×'))
        fields.addWidget(self.board_rows)
        size_label = label(tr('单格 mm'))
        size_label.setToolTip(tr('单个方格的宽×高，单位毫米；例如25×25。定向可全部留空。'))
        fields.addWidget(size_label)
        fields.addWidget(self.board_square)
        fields.addWidget(label('×'))
        fields.addWidget(self.board_square_height)
        for field in (self.board_square, self.board_square_height):
            field.setValidator(QDoubleValidator(0, 500, 2, field))
            field.setPlaceholderText('')
            field.setToolTip(tr('单个方格的宽×高，单位毫米；例如25×25。定向可全部留空。'))
        for field in (self.board_cols, self.board_rows, self.board_square, self.board_square_height):
            field.setFixedWidth(56)
        fields.addStretch()
        self.board_options_timer = QTimer(self)
        self.board_options_timer.setSingleShot(True)
        self.board_options_timer.setInterval(500)
        self.board_options_timer.timeout.connect(self.environment_board_options_changed)
        for field in (self.board_cols, self.board_rows, self.board_square, self.board_square_height):
            field.textChanged.connect(lambda *_: self.board_options_timer.start())
        self.board_fields.hide()
        mode_row.addWidget(self.board_fields, 1)
        self.env_mode = PositionSwitch((tr('自动'), tr('手动')), False)
        self.env_mode.changed.connect(self.environment_mode_changed)
        mode_row.addStretch()
        mode_row.addWidget(self.env_mode)
        box.addLayout(mode_row)
        row = QHBoxLayout()
        self.env_cameras = []
        for side, title in enumerate([tr('左相机'), tr('右相机')]):
            panel, pb = card()
            pb.addWidget(label(title))
            camera = PlaneView(title, side)
            camera.plane_chosen.connect(self.confirm_environment)
            camera.plane_hovered.connect(self.preview_environment_candidate)
            pb.addWidget(camera, 1)
            row.addWidget(panel, 1)
            self.env_cameras.append(camera)
            camera.polygon_closed.connect(self.submit_manual_environment)
            camera.drawing_started.connect(self.manual_drawing_started)
        box.addLayout(row, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        self.env_buttons = {}
        for kind, title in [('horizontal', tr('水平面基准')), ('vertical', tr('竖直面基准'))]:
            b = button(title, lambda checked=False, k=kind: self.select_environment(k))
            self.env_buttons[kind] = b
            actions.addWidget(b)
        self.env_clear = button(tr('清空'), self.clear_environment)
        actions.addWidget(self.env_clear)
        actions.addStretch()
        box.addLayout(actions)
        self.env_message = label(tr('尚未设置 · 相机默认参考。选择类别后保持场景静止，等待稳定候选；移入轮廓预览，左键确认。'), 'muted')
        self.env_message.setWordWrap(True)
        self.env_message.setMinimumHeight(2 * self.env_message.fontMetrics().lineSpacing() + 6)
        box.addWidget(self.env_message)
        self.env_blink = False
        self.env_blink_timer = QTimer(self)
        self.env_blink_timer.setInterval(450)
        self.env_blink_timer.timeout.connect(self.blink_environment)
        self.env_manual_busy = False
        self.env_manual_status = ''
        self.env_busy_timer = QTimer(self)
        self.env_busy_timer.setInterval(100)
        self.env_busy_timer.timeout.connect(self.environment_busy_tick)
        self.pages.addWidget(widget)
        self.paint_environment_buttons()

    def environment_board_options_changed(self):
        if self.env_worker:
            fields = (self.board_cols, self.board_rows, self.board_square, self.board_square_height)
            if any(field.text() and not field.hasAcceptableInput() for field in fields):
                self.env_worker.command('board_options', {'invalid': True})
                return
            size = self.board_square.locale().toDouble(self.board_square.text())[0] if self.board_square.text() else 0.
            height = self.board_square_height.locale().toDouble(self.board_square_height.text())[0] if self.board_square_height.text() else 0.
            self.env_worker.command('board_options', dict(cols=int(self.board_cols.text() or 0), rows=int(self.board_rows.text() or 0), square_mm=[size, height] if size or height else None))

    def environment_board_changed(self, checked):
        self.board_fields.setVisible(checked)
        self.environment_board_options_changed()
        for camera in self.env_cameras:
            camera.reset_polygon()
            camera.selecting = False
        self.env_board.setStyleSheet('background:#3274df;color:white;' if checked else '')
        if self.env_worker:
            self.env_worker.command('board', checked)
        else:
            self.env_message.setText(tr('开启相机，将棋盘格贴在目标表面，再选择类别；留空自动识别。') if checked else tr('自然平面模式。'))

    def environment_busy_tick(self):
        if self.env_manual_busy:
            self.env_message.setText(self.env_manual_status + '  ' + str(round(time.perf_counter() - self.env_busy_start, 1)) + tr('秒 · 可点击取消处理'))

    def environment_progress(self, message):
        self.env_manual_status = message
        self.environment_busy_tick()

    def escape_view(self):
        if getattr(self, 'env_manual_busy', False):
            self.clear_environment()
            return
        if self.page_is('environment') and self.env_mode.relative:
            for camera in self.env_cameras:
                if not camera.draw_pending:
                    camera.reset_polygon()
        else:
            self.leave_expand()

    def manual_drawing_started(self, side):
        for camera in self.env_cameras:
            if camera.side != side:
                camera.reset_polygon()

    def environment_mode_changed(self, manual):
        for camera in self.env_cameras:
            camera.manual_mode = manual
            camera.reset_polygon()
            camera.selecting = False
        if self.env_worker:
            self.env_worker.command('mode', manual)
        else:
            self.env_message.setText(tr('手动：开启相机，选择类别后右键圈选同一平面。') if manual else tr('自动：开启相机，选择类别后等待稳定网格。'))

    def submit_manual_environment(self, payload):
        if not self.env_worker or not self.last_packet or self.detecting:
            self.manual_environment_result(dict(ok=False, message=tr('请先开启相机并选择水平或竖直类别。')))
            return
        self.env_manual_busy = True
        self.env_busy_start = time.perf_counter()
        self.env_manual_status = tr('正在检查圈选位置…')
        self.env_busy_timer.start()
        self.env_mode.setEnabled(False)
        self.env_board.setEnabled(False)
        for b in self.env_buttons.values():
            b.setEnabled(False)
        self.env_clear.setText(tr('取消处理'))
        self.env_clear.setEnabled(True)
        for camera in self.env_cameras:
            camera.draw_pending = True
        self.env_message.setText(tr('正在圈选区域及附近寻找可靠平面…'))
        payload = dict(payload, views=[v.copy() for v in self.last_packet['views']])
        self.env_worker.command('manual', payload)

    def manual_environment_result(self, result):
        self.env_manual_busy = False
        self.env_busy_timer.stop()
        self.env_clear.setText(tr('清空'))
        editable = not (self.detecting or self.pending_start or self.stopping_record)
        self.env_controls_enabled(editable)
        for camera in self.env_cameras:
            camera.draw_pending = False
            if result['ok']:
                camera.reset_polygon()
            camera.update()
        if not result['ok'] and (not result.get('cancelled')):
            self.environment_error(result['message'])
        self.env_message.setText(result['message'])

    def environment_error(self, message):
        if hasattr(self, 'env_error_toast'):
            self.env_error_toast.deleteLater()
        toast = QLabel(message, self)
        self.env_error_toast = toast
        toast.setWordWrap(True)
        toast.setFixedWidth(390)
        toast.setStyleSheet('background:#fff0f1;color:#a22d3b;border:1px solid #e5a1aa;border-radius:10px;padding:14px;')
        toast.adjustSize()
        toast.move(max(18, self.width() - toast.width() - 18), max(0, self.height() - toast.height() - 82))
        toast.show()
        toast.raise_()
        QTimer.singleShot(5000, toast.hide)

    def preview_environment_candidate(self, identifier):
        for camera in self.env_cameras:
            camera.active_candidate_id = identifier
            camera.update()

    def blink_environment(self):
        self.env_blink = not self.env_blink
        self.paint_environment_buttons()

    def ensure_environment_worker(self):
        if self.env_worker and self.env_worker.isRunning():
            return True
        if not self.camera_ready or not self.worker:
            self.env_message.setText(tr('请点击上方“相机开启”，画面稳定后再选择基准。'))
            return False
        if not self.worker.metric_ready:
            message = tr('环境检测尚未启动：请到左侧相机配置→结果与导入，核对并启用当前设备的标定，再保存配置。')
            details = getattr(self.worker, 'calibration_error', '')
            if details: message += '\n' + details
            self.env_message.setText(message)
            self.environment_error(message)
            return False
        try:
            self.env_worker = EnvironmentWorker(ROOT / self.config['calibration'])
        except Exception as error:
            self.env_worker = None
            message = tr('环境分析启动失败：') + str(error)
            self.env_message.setText(message)
            self.environment_error(message)
            return False
        self.env_worker.updated.connect(self.environment_updated)
        self.env_worker.motion_detected.connect(self.environment_moved)
        self.env_worker.manual_result.connect(self.manual_environment_result)
        self.env_worker.manual_progress.connect(self.environment_progress)
        self.env_worker.command('mode', self.env_mode.relative)
        self.environment_board_options_changed()
        self.env_worker.command('board', self.env_board.isChecked())
        self.env_worker.command('page', self.page_is('environment'))
        self.env_worker.start()
        return True

    def env_controls_enabled(self, enabled):
        self.env_mode.setEnabled(enabled)
        self.env_board.setEnabled(enabled)
        self.board_fields.setEnabled(enabled)
        for b in list(self.env_buttons.values()) + [self.env_clear]:
            b.setEnabled(enabled)
        for camera in self.env_cameras:
            camera.selecting = enabled and bool(self.env_state.get('selecting'))

    def select_environment(self, kind):
        if self.detecting or self.pending_start or self.stopping_record:
            return
        if not self.camera_ready:
            self.env_message.setText(tr('请点击上方“相机开启”，画面稳定后再选择基准。'))
            return
        for camera in self.env_cameras:
            camera.reset_polygon()
        if self.ensure_environment_worker():
            self.env_worker.command('select', kind)

    def confirm_environment(self, identifier):
        if self.env_worker and (not (self.detecting or self.pending_start or self.stopping_record)):
            self.env_worker.command('confirm', identifier)

    def clear_environment(self):
        if getattr(self, 'env_manual_busy', False):
            if self.env_worker:
                self.env_worker.cancel_manual()
            self.env_manual_status = tr('正在取消，原基准保留…')
            self.env_clear.setEnabled(False)
            return
        if self.detecting or self.pending_start or self.stopping_record:
            return
        for camera in self.env_cameras:
            camera.reset_polygon()
        if self.env_worker:
            self.env_worker.command('clear')
        else:
            self.environment_updated(dict(planes={}, selecting=None, overlays=[], snapshot=None, message=tr('已清空，使用相机参考。')))

    def paint_environment_buttons(self):
        planes = self.env_state.get('planes', {})
        selecting = self.env_state.get('selecting')
        if selecting and (not self.env_blink_timer.isActive()):
            self.env_blink = False
            self.env_blink_timer.start()
        elif not selecting:
            self.env_blink_timer.stop()
            self.env_blink = False
        for kind, b in self.env_buttons.items():
            color = '#3274df' if kind in planes and selecting and (selecting != kind) else '#14996b' if kind in planes else '#dc4b56'
            if selecting == kind:
                color = '#ed8991' if self.env_blink else '#dc4b56'
            b.setStyleSheet('QPushButton {color:white;background:' + color + ';border:0;} QPushButton:disabled {color:#eef1f5;background:' + color + ';}')

    def environment_updated(self, state):
        old_id = (self.env_snapshot or {}).get('id')
        self.env_state = state
        self.env_snapshot = state.get('snapshot')
        if not getattr(self, 'env_manual_busy', False):
            self.env_message.setText(state.get('message', ''))
        self.paint_environment_buttons()
        editable = not (self.detecting or self.pending_start or self.stopping_record)
        for camera in self.env_cameras:
            camera.overlays = state.get('overlays', [])
            camera.selecting = editable and bool(state.get('selecting'))
            camera.update()
        if old_id != (self.env_snapshot or {}).get('id') and editable:
            self.apply_display_frame()
        if self.env_snapshot:
            self.coordinate_mode.setEnabled(False)
        elif editable:
            self.coordinate_mode.setEnabled(True)

    def environment_moved(self, details):
        self.env_snapshot = None
        if self.worker and (self.detecting or self.pending_start):
            self.environment_invalid = True
            self.worker.invalidate_environment(details)
            self.update_xyz(None)
            self.set_phase_enabled(False)
            if self.stop_on_motion.isChecked():
                self.stop_capture()
                QApplication.beep()
            message = tr('已确认相机移动，环境基准已清除。') + (tr('本轮停止并保存。') if self.stop_on_motion.isChecked() else tr('继续保存原始数据，环境坐标显示已暂停。'))
        else:
            message = tr('已确认相机移动，环境基准已清除，请重新设置。')
        self.motion_toast(message)

    def motion_toast(self, message):
        if hasattr(self, 'toast'):
            self.toast.deleteLater()
        self.toast = QLabel(message, self)
        self.toast.setWordWrap(True)
        self.toast.setFixedWidth(360)
        self.toast.setStyleSheet('background:#fff0f1;color:#a22d3b;border:1px solid #e5a1aa;border-radius:10px;padding:16px;')
        self.toast.adjustSize()
        self.toast.move(max(0, self.width() - 380), max(0, self.height() - self.toast.height() - 78))
        self.toast.show()
        self.toast.raise_()
        QTimer.singleShot(9000, self.toast.hide)

    def make_curves(self):
        widget, box = page(tr('时间曲线'), tr('显示坐标：X左右、Y前后、Z上下。单位毫米；原始相机坐标另存，缺失点不插值。'))
        row = QHBoxLayout()
        self.curve_source = QComboBox()
        self.curve_source.addItems([tr('当前实验'), tr('最近加载的历史记录')])
        row.addWidget(self.curve_source)
        row.addStretch()
        self.curve_back = button(tr('返回'), self.return_to_replay)
        self.curve_back.setIcon(icon('back'))
        self.curve_back.hide()
        row.addWidget(self.curve_back)
        row.addWidget(button(tr('载入历史CSV'), self.import_history))
        box.addLayout(row)
        self.curve_plots = []
        self.curve_lines = []
        for axis, color in zip('XYZ', ['#d95b66', '#2b9b7b', '#377ee3']):
            plot = pg.PlotWidget(background='white', axisItems={'left': CenteredAxisItem('left')})
            plot.showGrid(x=True, y=True, alpha=0.12)
            plot.getAxis('left').setStyle(maxTickLevel=0)
            plot.getAxis('bottom').setStyle(maxTickLevel=0)
            plot.getAxis('left').enableAutoSIPrefix(False)
            plot.getAxis('bottom').enableAutoSIPrefix(False)
            plot.setLabel('left', axis, units='mm')
            plot.setLabel('bottom', tr('时间'), units='s')
            plot.getAxis('left').setWidth(75)
            plot.setMenuEnabled(False)
            self.curve_plots.append(plot)
            line = plot.plot(pen=pg.mkPen(color, width=1), connect='finite', antialias=False)
            line.setClipToView(True)
            line.setDownsampling(auto=True, method='peak')
            self.curve_lines.append(line)
            box.addWidget(plot, 1)
        self.pages.addWidget(widget)

    def make_precision(self):
        widget, box = page(tr('精度验证'), tr('把参考位移、静止抖动与返回偏差分别记录，避免混淆。'))
        panel, pb = card()
        row = QHBoxLayout()
        row.addWidget(label(tr('参考位移')))
        self.reference = QDoubleSpinBox()
        self.reference.setRange(10, 500)
        self.reference.setValue(self.config['reference_mm'])
        self.reference.setSuffix(' mm')
        row.addWidget(self.reference)
        self.reference.valueChanged.connect(self.update_precision)
        self.precision_source = QComboBox()
        self.precision_source.addItems([tr('当前实验'), tr('最近加载的历史记录')])
        self.precision_source.currentIndexChanged.connect(self.update_precision)
        row.addWidget(self.precision_source)
        row.addStretch()
        row.addWidget(button(tr('载入历史CSV'), self.import_history))
        pb.addLayout(row)
        self.phase_note = label(tr('实时验证：先开始检测并选好左右目标，再依次采集A1、B、A2。'), 'muted')
        self.phase_note.setWordWrap(True)
        pb.addWidget(self.phase_note)
        controls = QHBoxLayout()
        self.phase_buttons = []
        for name, text in [('A1', tr('记录 A1 · 5秒')), ('B', tr('记录 B · 5秒')), ('A2', tr('返回 A · 5秒'))]:
            b = button(text, lambda checked=False, n=name: self.begin_phase(n))
            b.setEnabled(False)
            self.phase_buttons.append(b)
            controls.addWidget(b)
        controls.addStretch()
        pb.addLayout(controls)
        box.addWidget(panel)
        self.precision_table = table([tr('阶段'), tr('有效 / 总帧'), tr('X抖动 σ (mm)'), tr('Y抖动 σ (mm)'), tr('Z抖动 σ (mm)')])
        box.addWidget(self.precision_table, 1)
        self.precision_result = label(tr('等待A1、B、A2数据。'))
        self.precision_result.setWordWrap(True)
        box.addWidget(self.precision_result)
        note = label(tr('单段至少20个有效点、有效率≥80%且覆盖≥4.5秒才参与比较。标准差表示短时静止波动，不代表绝对精度。'), 'muted')
        note.setWordWrap(True)
        box.addWidget(note)
        self.pages.addWidget(widget)

    def make_records(self):
        widget, box = page(tr('实验记录'), tr('每次开始自动建立独立记录；停止后可回放、添加备注或打开数据目录。'))
        row = QHBoxLayout()
        row.addWidget(button(tr('刷新列表'), self.refresh_records))
        row.addWidget(button(tr('导入历史CSV'), self.import_history))
        row.addStretch()
        self.delete_button = button('', self.delete_records)
        self.delete_button.setIcon(icon('trash', '#c74852'))
        self.delete_button.setToolTip(tr('删除选中记录（可撤销）'))
        row.addWidget(self.delete_button)
        self.batch_delete = button(tr('批量删除'), self.toggle_batch_delete)
        self.batch_delete.setCheckable(True)
        row.addWidget(self.batch_delete)
        self.undo_delete = button(tr('撤销删除'), self.undo_records_delete)
        self.undo_delete.setEnabled(False)
        row.addWidget(self.undo_delete)
        self.last_delete_batch = None
        box.addLayout(row)
        self.records_table = table([tr('实验记录'), tr('目标'), tr('数据条数'), tr('有效率'), tr('状态')])
        self.records_table.itemSelectionChanged.connect(self.record_selected)
        self.records_table.setItemDelegate(NoCellFocusDelegate(self.records_table))
        box.addWidget(self.records_table, 1)
        self.record_detail = label(tr('选择一条记录查看信息。'), 'muted')
        self.record_detail.setWordWrap(True)
        box.addWidget(self.record_detail)
        row = QHBoxLayout()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(tr('实验名称或备注（独立保存，不修改原始测量）'))
        row.addWidget(self.note_edit, 1)
        row.addWidget(button(tr('保存备注'), self.save_note))
        row.addWidget(button(tr('打开目录'), self.open_record_folder))
        row.addWidget(button(tr('回放此记录'), self.replay_record, 'primary'))
        box.addLayout(row)
        self.record_paths = []
        self.pages.addWidget(widget)

    def make_replay(self):
        widget, box = page(tr('数据回放'), tr('历史数据模式 · 不创建新的实测记录，不连接相机。'))
        row = QHBoxLayout()
        row.addWidget(button(tr('打开CSV / 实验记录'), self.import_replay))
        self.replay_name = label(tr('未加载记录'), 'muted')
        row.addWidget(self.replay_name, 1)
        self.replay_curves = button(tr('时间曲线'), self.open_replay_curves, 'primary')
        self.replay_curves.setIcon(icon('timer', '#ffffff'))
        self.replay_curves.setEnabled(False)
        row.addWidget(self.replay_curves)
        box.addLayout(row)
        self.replay_xyz = label('X：—     Y：—     Z：—   mm', 'coordinate')
        box.addWidget(self.replay_xyz)
        self.replay_view = Trajectory(expandable=False)
        box.addWidget(self.replay_view, 1)
        self.replay_view.mode_changed.connect(lambda _: self.seek_replay(self.slider.value()))
        self.replay_warning = label(tr('旧实验没有连续影像时，仅回放坐标与轨迹。'), 'muted')
        self.replay_warning.setWordWrap(True)
        box.addWidget(self.replay_warning)
        controls = QHBoxLayout()
        self.play_button = button(tr('播放'), self.toggle_play, 'primary')
        controls.addWidget(self.play_button)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self.pause_replay)
        self.slider.valueChanged.connect(self.user_seek_replay)
        controls.addWidget(self.slider, 1)
        self.replay_clock = label('0.00 / 0.00 s', 'muted')
        controls.addWidget(self.replay_clock)
        self.speed = QComboBox()
        self.speed.addItems(['0.25×', '0.5×', '1×', '2×'])
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(self.change_speed)
        controls.addWidget(self.speed)
        box.addLayout(controls)
        self.pages.addWidget(widget)

    def make_settings(self):
        widget, box = page(tr('设置'), tr('实验运行期间锁定采集参数。显示开关与三维视角仍可调整。'))
        panel, pb = card()
        form = QFormLayout()
        self.language = QComboBox()
        self.language.addItem(tr('简体中文'), 'zh_CN')
        self.language.addItem('English', 'en')
        self.language.setCurrentIndex(0 if self.config.get('language') == 'zh_CN' else 1)
        form.addRow(tr('语言 / Language'), self.language)
        self.language.currentIndexChanged.connect(self.change_language)
        self.camera_index = QSpinBox()
        self.camera_index.setRange(0, 20)
        self.camera_index.setValue(self.config['camera_index'])
        self.trail = QSpinBox()
        self.trail.setRange(2, 120)
        self.trail.setSuffix(tr(' 秒'))
        self.trail.setValue(self.config['trail_seconds'])
        self.trail.hide()
        self.video = QCheckBox(tr('保存原始拼接视频（占用较多磁盘空间）'))
        self.video.setChecked(self.config['record_video'])
        self.diagnostic_images = QCheckBox(tr('整轮抽样保存诊断原图（每2秒一张）'))
        self.diagnostic_images.setChecked(self.config.get('diagnostic_images', True))
        self.depth_min = QDoubleSpinBox()
        self.depth_min.setRange(0.1, 2)
        self.depth_min.setSingleStep(0.1)
        self.depth_min.setSuffix(' m')
        self.depth_min.setValue(self.config['depth_min_m'])
        self.depth_max = QDoubleSpinBox()
        self.depth_max.setRange(0.3, 5)
        self.depth_max.setSingleStep(0.1)
        self.depth_max.setSuffix(' m')
        self.depth_max.setValue(self.config['depth_max_m'])
        form.addRow(tr('最近探测距离（未验证）'), self.depth_min)
        form.addRow(tr('最远探测距离（未验证）'), self.depth_max)
        self.camera_index.hide()
        form.addRow(tr('视频录制'), self.video)
        form.addRow(tr('深度诊断'), self.diagnostic_images)
        self.stop_on_motion = QCheckBox(tr('确认相机移动后自动停止检测'))
        self.stop_on_motion.setChecked(self.config.get('stop_on_camera_motion', True))
        form.addRow(tr('环境基准保护'), self.stop_on_motion)
        self.coordinate_mode = QComboBox()
        self.coordinate_mode.addItems([tr('面对相机：本人右 / 靠近相机 / 上为正')])
        self.coordinate_mode.setCurrentIndex(1 if self.config.get('display_alignment') == 'hand_estimate' else 0)
        self.coordinate_mode.hide()
        self.camera_summary = label(self.camera_configuration_text())
        form.addRow(tr('采集模式'), self.camera_summary)
        pb.addLayout(form)
        box.addWidget(panel)
        self.frame_tip = label('', 'muted')
        self.frame_tip.setWordWrap(True)
        self.frame_tip.hide()
        self.coordinate_mode.currentIndexChanged.connect(self.apply_display_frame)
        row = QHBoxLayout()
        row.addWidget(button(tr('保存设置'), self.save_settings, 'primary'))
        row.addWidget(button(tr('打开软件目录'), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT)))))
        row.addStretch()
        box.addLayout(row)
        box.addStretch()
        self.pages.addWidget(widget)

    def camera_configuration_text(self):
        from camera_setup import profile
        p = profile(self.config)
        return f"#{p['camera_index']} · {p['camera_width']} × {p['camera_height']} · {p['camera_format']} · {p['camera_fps']} FPS · {p['camera_layout']}"

    def make_camera_setup(self):
        from camera_dialog import CameraSetupPage
        widget,box=page(tr('相机配置'),tr('连接相机、采集标定图，或导入此设备的标定参数。'))
        self.camera_setup=CameraSetupPage(self.config,self)
        self.camera_setup.can_access=lambda:self.worker is None and not self.camera_closing and not self.detecting and not self.pending_start
        self.camera_setup.applied.connect(self.apply_camera_configuration)
        box.addWidget(self.camera_setup,1);self.pages.addWidget(widget)

    def configure_camera(self):
        self.navigate('camera_setup')

    def apply_camera_configuration(self,config):
        if self.worker:return
        if self.env_worker:
            self.env_worker.stop()
            if not self.env_worker.wait(3000):
                self.show_error(tr('环境分析正在结束，请稍后重试。'));return
            self.env_worker=None
        self.config.update(config);self.camera_index.setValue(self.config['camera_index'])
        self.env_snapshot=None;self.environment_invalid=False
        self.clear_environment();self.apply_display_frame()
        self.camera_summary.setText(self.camera_configuration_text())
        if self.save_settings():self.camera_setup.mark_saved(self.config)

    def rotation_for_report(self, report):
        saved = report.get('environment_reference') or {}
        matrix = saved.get('camera_to_display')
        if matrix is None and 'environment_reference' in report:
            matrix = report.get('position_reference', {}).get('camera_to_display')
        if matrix is not None:
            return np.array(matrix, dtype=float)
        if self.coordinate_mode.currentIndex() == 1 and report.get('calibration_sha256') != self.frame_metadata.get('calibration_sha256'):
            return CAMERA_TO_SCENE.copy()
        return self.base_rotation.copy()

    def apply_display_frame(self, *args):
        rotation = CAMERA_TO_SCENE.copy()
        metadata = {'name': tr('面对相机的本人方向'), 'kind': 'camera_axes_convention', 'camera_to_display': rotation.tolist()}
        self.base_rotation = rotation.copy()
        if self.env_snapshot:
            rotation = np.array(self.env_snapshot['camera_to_display'], float)
            metadata = self.env_snapshot
        if (self.detecting or self.pending_start or self.stopping_record or self.was_stopped) and self.session_rotation is not None:
            rotation = self.session_rotation.copy()
        self.display_rotation = rotation
        self.frame_metadata = metadata
        self.config['display_frame_metadata'] = metadata
        self.trajectory.set_coordinate_frame(rotation, metadata['name'])
        self.history_rotation = self.rotation_for_report(self.history_report)
        self.replay_rotation = self.rotation_for_report(self.replay_report)
        self.replay_view.set_coordinate_frame(self.replay_rotation, metadata['name'])
        if self.live:
            self.update_xyz(self.live[-1])
            self.trajectory.set_samples(list(self.live), self.trail.value())
        if self.replay_samples:
            self.seek_replay(self.slider.value())
        self.update_curves()
        self.update_precision()

    def change_language(self):
        selected = self.language.currentData()
        if selected == self.config.get('language', 'en'):
            return
        if self.worker or self.env_worker or self.camera_setup.has_work():
            self.language.blockSignals(True)
            self.language.setCurrentIndex(0 if self.config.get('language') == 'zh_CN' else 1)
            self.language.blockSignals(False)
            self.show_error(tr('请先关闭相机再切换语言。'))
            return
        self.config['language'] = selected
        if not self.save_settings():
            return
        self.timer.stop()
        self.close()
        QApplication.exit(42)

    def toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def page_is(self, name):
        return self.pages.currentIndex() == self.page_ids.index(name)

    def navigate(self, index, from_replay=False):
        if isinstance(index, int):
            index = ['monitor', 'curves', 'monitor', 'precision', 'records', 'replay', 'settings'][index]
        if index == 'camera_setup' and (self.detecting or self.pending_start or self.stopping_record):
            self.show_error(tr('请先停止检测并保存，再配置相机。'));return
        if index != 'camera_setup':self.camera_setup.stop_preview()
        if index == 'replay' and (self.detecting or self.pending_start or self.stopping_record):
            QMessageBox.information(self, tr('请先停止检测'), tr('回放历史数据前，请先点击右下角“停止检测”。'))
            return
        if self.expanded:
            self.leave_expand()
        self.pages.setCurrentIndex(self.page_ids.index(index))
        self.start_btn.setVisible(index != 'camera_setup');self.stop_btn.setVisible(index != 'camera_setup')
        if self.env_worker:
            self.env_worker.command('page', index == 'environment')
        self.curve_from_replay = index == 'curves' and from_replay
        self.curve_back.setVisible(self.curve_from_replay)
        for i, b in enumerate(self.nav):
            b.setChecked(self.page_ids[i] == index)
            b.setIcon(icon(self.nav_icons[i], '#245cb0' if self.page_ids[i] == index else '#61718a'))
        if index != 'replay':
            self.pause_replay()
        if index == 'curves':
            self.update_curves()
        if index == 'monitor' and self.live:
            self.trajectory.set_samples(list(self.live))
        if index == 'records':
            self.refresh_records()
        if index == 'precision':
            self.update_precision()

    def open_replay_curves(self):
        if not self.replay_samples:
            return
        self.curve_source.setCurrentIndex(1)
        self.update_curves()
        self.navigate('curves', from_replay=True)

    def return_to_replay(self):
        self.navigate('replay')

    def apply_visibility(self):
        if not hasattr(self, 'coordinates'):
            return
        if not self.expanded and (not self.images.isHidden()) and (not self.trajectory.isHidden()):
            sizes = self.monitor_split.sizes()
            if min(sizes) > 0:
                self.normal_split_sizes = sizes
        self.coordinates.setVisible(self.toggles['xyz'].isChecked() and (not self.expanded))
        self.images.setVisible(self.toggles['images'].isChecked() and (not self.expanded))
        self.trajectory.setVisible(self.toggles['trajectory'].isChecked() or self.expanded)
        for camera in self.cameras:
            camera.markers = self.toggles['markers'].isChecked()
            camera.update()
        if self.last_packet and self.toggles['images'].isChecked():
            for w, im, d in zip(self.cameras, self.last_packet['views'], self.last_packet['detections']):
                w.set_frame(im, d)
        any_panel = self.expanded or self.toggles['images'].isChecked() or self.toggles['trajectory'].isChecked()
        self.monitor_split.setVisible(any_panel)
        self.empty.setVisible(not any_panel)
        if not self.expanded and self.toggles['images'].isChecked() and self.toggles['trajectory'].isChecked():
            self.monitor_split.setSizes(self.normal_split_sizes)

    def toggle_expand(self):
        if self.expanded:
            self.leave_expand()
            return
        self.previous_side = not self.sidebar.isHidden()
        self.previous_sizes = self.monitor_split.sizes()
        self.expanded = True
        self.sidebar.hide()
        self.trajectory.expand.setText('↙')
        self.apply_visibility()

    def leave_expand(self):
        if not self.expanded:
            return
        self.expanded = False
        self.sidebar.setVisible(self.previous_side)
        self.trajectory.expand.setText('⛶')
        self.apply_visibility()
        self.monitor_split.setSizes(self.previous_sizes)

    def choose_target(self, side, x, y):
        if self.worker:
            self.worker.set_hint(side, x, y)

    def save_settings(self):
        if self.detecting or self.pending_start or self.stopping_record:
            QMessageBox.information(self, tr('正在检测'), tr('停止检测后再保存采集参数。'))
            return False
        if self.depth_min.value() >= self.depth_max.value():
            self.show_error(tr('最近距离必须小于最远距离。'))
            return False
        self.config.update(camera_index=self.camera_index.value(), depth_min_m=self.depth_min.value(), depth_max_m=self.depth_max.value(), trail_seconds=self.trail.value(), record_video=self.video.isChecked(), diagnostic_images=self.diagnostic_images.isChecked(), stop_on_camera_motion=self.stop_on_motion.isChecked(), display_alignment='hand_estimate' if self.coordinate_mode.currentIndex() == 1 else 'camera', reference_mm=self.reference.value())
        try:
            write_json(ROOT / 'config/settings.json', {k: v for k, v in self.config.items() if k not in ['environment_reference', 'display_frame_metadata']})
        except OSError as e:
            self.show_error(tr('无法保存设置：{0}').format(e))
            return False
        self.status.setText(tr('设置已保存'))
        return True

    def toggle_camera(self):
        if self.worker:
            self.camera_closing = True
            self.worker.stop_event.set()
            self.camera_button.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.state.setText(tr('正在关闭相机…'))
            self.set_phase_enabled(False)
        else:
            self.open_camera()

    def open_camera(self):
        if self.camera_setup.has_work():
            self.show_error(tr('相机配置预览或后台任务尚未结束，请稍候。'));return False
        if self.worker:
            return True
        if not self.save_settings():
            return False
        self.current_error = None
        self.camera_ready = False
        self.camera_closing = False
        self.was_stopped = False
        self.state.setText(tr('正在开启相机…'))
        self.camera_button.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.camera_index.setEnabled(False)
        self.worker = CaptureWorker(self.config, auto_record=False)
        self.worker.camera_opened.connect(self.preview_opened)
        self.worker.opened.connect(self.capture_opened)
        self.worker.recording_stopped.connect(self.recording_finished)
        self.worker.failed.connect(self.capture_failed)
        self.worker.phase_finished.connect(self.phase_done)
        self.worker.finished.connect(self.capture_finished)
        self.worker.start()
        return True

    def preview_opened(self):
        self.camera_ready = True
        self.camera_button.setEnabled(True)
        self.camera_button.setText(tr('相机关闭'))
        self.camera_button.setIcon(icon('camera', '#14875e'))
        self.camera_button.setToolTip(tr('关闭相机；正在检测时会先完成保存'))
        self.start_btn.setEnabled(False)
        if not self.pending_start:
            self.state.setText(tr('相机预览 · 可选择目标'))

    def start_capture(self):
        if self.detecting or self.pending_start or self.stopping_record or self.camera_closing:
            return
        if any((camera.draw_pending for camera in self.env_cameras)):
            self.status.setText(tr('请等待手动基准验证完成后开始检测。'))
            return
        if self.worker:
            if not self.save_settings():
                return
        elif not self.open_camera():
            return
        if not self.worker.metric_ready:
            self.show_error(tr('请先开启相机预览；实际尺寸和标定核对通过后才能检测。') + ' ' + self.worker.calibration_error)
            return
        self.pause_replay()
        self.navigate('monitor')
        self.live.clear()
        self.trajectory.reset()
        self.current_error = None
        self.last_session = None
        self.was_stopped = False
        self.session_rotation = None
        self.environment_invalid = False
        if self.env_worker:
            self.env_worker.lock_selection = True
            self.env_worker.command('select', None)
            self.env_snapshot = self.env_worker.snapshot()
        self.apply_display_frame()
        self.session_rotation = self.display_rotation.copy()
        self.config['environment_reference'] = self.env_snapshot
        self.env_controls_enabled(False)
        self.curve_source.setCurrentIndex(0)
        self.precision_source.setCurrentIndex(0)
        self.update_xyz(None)
        self.pending_start = True
        self.state.setText(tr('正在开始检测…'))
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        for w in [self.reference, self.video, self.diagnostic_images, self.coordinate_mode, self.depth_min, self.depth_max, self.stop_on_motion]:
            w.setEnabled(False)
        self.worker.request_start(self.config)

    def capture_opened(self, path):
        self.last_session = Path(path)
        self.detecting = True
        self.pending_start = False
        if not self.stopping_record:
            self.state.setText(tr('正在检测 · 可重新选择目标'))
        self.set_phase_enabled(not self.stopping_record)

    def stop_capture(self):
        if self.worker and (self.detecting or self.pending_start):
            self.stopping_record = True
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)
            self.state.setText(tr('正在停止并保存…'))
            self.set_phase_enabled(False)

    def capture_failed(self, message):
        self.current_error = message
        logging.error(message)

    def preserve_result(self):
        self.tracking.setText(tr('停止后的最后结果'))
        if self.last_session and (self.last_session / 'measurements.csv').exists():
            try:
                samples, report, _ = read_session(self.last_session)
                self.live = deque(samples)
            except Exception:
                logging.exception('Read completed session')
        self.trajectory.set_samples(list(self.live))
        if self.live:
            self.update_xyz(self.live[-1])
        self.update_precision()
        self.refresh_records()

    def recording_finished(self, path):
        self.poll_capture()
        self.last_session = Path(path)
        self.detecting = False
        self.pending_start = False
        self.stopping_record = False
        self.stage_busy = False
        self.was_stopped = True
        self.start_btn.setEnabled(not self.camera_closing)
        self.stop_btn.setEnabled(False)
        self.set_phase_enabled(False)
        for w in [self.reference, self.video, self.diagnostic_images, self.coordinate_mode, self.depth_min, self.depth_max, self.stop_on_motion]:
            w.setEnabled(True)
        self.env_controls_enabled(True)
        if self.env_worker:
            self.env_worker.lock_selection = False
        self.state.setText(tr('停止'))
        self.status.setText(tr('记录已保存：') + self.last_session.name)
        self.preserve_result()

    def capture_finished(self):
        self.poll_capture()
        error = self.current_error or self.worker.error
        if self.worker.path:
            self.last_session = self.worker.path
        self.worker.deleteLater()
        self.worker = None
        self.detecting = False
        self.pending_start = False
        self.stopping_record = False
        self.camera_closing = False
        self.camera_ready = False
        self.stage_busy = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.set_phase_enabled(False)
        self.camera_button.setEnabled(True)
        self.camera_button.setText(tr('相机开启'))
        self.camera_button.setIcon(icon('camera', '#c74852'))
        for w in [self.camera_index, self.reference, self.video, self.diagnostic_images, self.coordinate_mode, self.depth_min, self.depth_max, self.stop_on_motion]:
            w.setEnabled(True)
        self.env_controls_enabled(True)
        if self.env_worker:
            self.env_worker.lock_selection = False
        for camera in self.env_cameras:
            camera.frame = None
            camera.overlays = []
            camera.update()
        if self.env_worker:
            self.env_worker.command('select', None)
        self.state.setText(tr('异常停止') if error else tr('停止'))
        self.status.setText(tr('相机已关闭 · 记录已保存') if self.last_session else tr('相机已关闭'))
        self.preserve_result()
        for camera in self.cameras:
            camera.frame = None
            camera.detection = None
            camera.update()
        self.last_packet = None
        if error:
            self.update_xyz(None)
            self.show_error(error)

    def update_xyz(self, sample):
        valid = sample and sample['valid'] and sample.get('environment_valid', True) and (not self.environment_invalid)
        if valid and self.trajectory.anchor is None:
            origin = first_origin(self.live)
            if origin is not None:
                self.trajectory.anchor = origin * 1000
                self.trajectory.reset_view()
        if valid and self.trajectory.denoise:
            ensure_filtered(list(self.live))
        displayed = self.trajectory.displayed_point(sample['filtered_xyz'] if self.trajectory.denoise else sample['xyz']) if valid else None
        for i, w in enumerate(self.xyz_labels):
            w.setText(f"{'XYZ'[i]}  {displayed[i]:,.2f}" if valid else f"{'XYZ'[i]}  —")
        self.tracking.setText((tr('降噪估计 · ') if self.trajectory.denoise else '') + (tr('已追踪 · 相对位置') if self.trajectory.relative else tr('已追踪 · 绝对位置')) if valid else tr('目标丢失 / 等待选取'))

    def position_mode_changed(self, *args):
        if self.live:
            self.update_xyz(self.live[-1])
        self.update_curves()

    def poll_capture(self):
        if not self.worker:
            return
        packet, samples = self.worker.take()
        self.live.extend(samples)
        if samples:
            self.update_xyz(samples[-1])
        if packet:
            self.last_packet = packet
            if self.page_is('environment'):
                for w, im in zip(self.env_cameras, packet['views']):
                    w.set_frame(im)
            if packet.get('metric_ready', False) and self.env_worker and (self.page_is('environment') or self.env_snapshot) and (time.perf_counter() - self.last_env_submit > 0.35):
                self.last_env_submit = time.perf_counter()
                self.env_worker.submit(packet['views'], packet.get('host_read_complete_ns', time.perf_counter_ns()))
            if self.toggles['images'].isChecked() and self.page_is('monitor'):
                for w, im, d in zip(self.cameras, packet['views'], packet['detections']):
                    w.set_frame(im, d)
            if packet['recording'] and self.detecting and (not self.stopping_record) and (not self.camera_closing):
                self.state.setText(tr('环境基准失效 · 仅保存原始数据') if self.environment_invalid else tr('正在检测 · 已追踪') if packet['sample']['valid'] else tr('正在检测 · ') + {'marker_not_detected_in_both_views': tr('圆检测丢失，请检查遮挡或重新点选'), 'depth_outside_test_workspace': tr('超出设置测距范围'), 'epipolar_mismatch': tr('左右匹配未通过')}.get(packet['sample']['reason'], tr('无有效三维点')))
                self.status.setText(tr('采集 {0:.1f} Hz   ·   有效三维 {1:.1f} Hz   ·   有效率 {2:.1%}   ·   自动保存中').format(packet['capture_hz'], packet['valid_hz'], packet['valid_ratio']))
            elif not self.pending_start and (not self.stopping_record) and (not self.camera_closing):
                self.state.setText(tr('停止') if self.was_stopped else tr('相机预览 · 可选择目标'))
                self.start_btn.setEnabled(packet.get('metric_ready', False))
                if not packet.get('metric_ready', False):
                    self.state.setText(tr('未标定 · 仅原始预览'))
                    self.status.setText(tr('请在左侧相机配置中核对设备并完成标定。') + ' ' + packet.get('calibration_error', ''))
                    return
                selected = sum((d is not None for d in packet['detections']))
                self.status.setText(tr('相机预览 {0:.1f} Hz · 目标已选 {1}/2 · 未记录实验').format(packet['capture_hz'], selected))
        if samples and self.page_is('monitor') and self.toggles['trajectory'].isChecked():
            self.trajectory.set_samples(list(self.live))

    def tick(self):
        self.poll_capture()
        now = time.perf_counter()
        if now - self.last_chart > 0.2:
            self.last_chart = now
            if self.page_is('curves'):
                self.update_curves()
        if self.playing and self.replay_samples:
            target = self.replay_base + (now - self.play_started) * [0.25, 0.5, 1, 2][self.speed.currentIndex()]
            index = min(len(self.replay_times) - 1, bisect.bisect_right(self.replay_times, target) - 1)
            self.slider.blockSignals(True)
            self.slider.setValue(max(0, index))
            self.slider.blockSignals(False)
            self.seek_replay(max(0, index))
            if target >= self.replay_times[-1]:
                self.pause_replay()

    def update_curves(self):
        history = self.curve_source.currentIndex() != 0
        source = self.history_samples if history else self.live
        relative = self.replay_view.relative if history else self.trajectory.relative
        denoise = self.replay_view.denoise if history else self.trajectory.denoise
        rotation = self.history_rotation if history else self.display_rotation
        last = source[-1] if source else None
        signature = (id(source), len(source), id(last), relative, denoise, np.asarray(rotation).tobytes())
        if signature == self._curve_key:
            return False
        self._curve_key = signature
        samples = list(source)
        labels = ['XYZ'[i] + (tr(' 相对') if relative else tr(' 绝对')) + (tr(' 降噪') if denoise else tr(' 原始')) for i in range(3)]
        for plot, text in zip(self.curve_plots, labels):
            if plot.getAxis('left').labelText != text:
                plot.setLabel('left', text, units='mm')
        if not samples:
            for line in self.curve_lines:
                line.setData([], [])
            return True
        if denoise:
            ensure_filtered(samples)
        origin = first_origin(samples) if relative else None
        times = np.fromiter((s['t'] for s in samples), dtype=float, count=len(samples))
        raw = np.array([s['filtered_xyz'] if denoise else s['xyz'] for s in samples])
        valid = np.array([s['valid'] and s.get('environment_valid', True) for s in samples])
        raw[~valid] = np.nan
        points = to_scene(raw - (origin if origin is not None else 0), rotation) * 1000
        for i, line in enumerate(self.curve_lines):
            # Keep original arrays intact; viewport clipping / peak reduction is display only.
            line.setData(times, points[:, i], connect='finite')
        return True

    def set_phase_enabled(self, value):
        for b in self.phase_buttons:
            b.setEnabled(value)

    def begin_phase(self, name):
        if not self.worker or not self.detecting or self.stage_busy:
            return
        if not self.live or not self.live[-1]['valid']:
            QMessageBox.information(self, tr('目标尚未有效追踪'), tr('请先在两侧画面点击黑圆，确认XYZ有效。'))
            return
        self.precision_source.setCurrentIndex(0)
        self.stage_busy = True
        self.worker.begin_phase(name)
        self.set_phase_enabled(False)
        self.phase_note.setText(tr('正在记录 {0}，保持静止5秒；完成后会提示下一步。').format(name))

    def phase_done(self, name):
        self.stage_busy = False
        self.set_phase_enabled(self.worker is not None and self.detecting and (not self.stopping_record) and (not self.worker.stop_event.is_set()))
        self.phase_note.setText({'A1': tr('A1已记录。沿导向边移动到B，停稳后点击“记录B”。'), 'B': tr('B已记录。返回A挡块，停稳后点击“返回A”。'), 'A2': tr('A→B→A已记录。查看下方结果；不合格阶段可重采，旧数据保留。')}[name])
        self.poll_capture()
        self.update_precision()

    def update_precision(self, *args):
        if not hasattr(self, 'precision_table'):
            return
        history = self.precision_source.currentIndex() == 1
        samples = self.history_samples if history else list(self.live)
        rotation = self.history_rotation if history else self.display_rotation
        samples = [dict(s, xyz=to_scene(s['xyz'], rotation)) for s in samples]
        reference = self.reference.value()
        if history:
            reference = self.history_report.get('reference_displacement_mm', reference)
        groups, comparison = statistics(samples, reference)
        self.precision_table.setRowCount(len(groups))
        for r, (name, g) in enumerate(groups.items()):
            values = [name + (' ✓' if g['accepted'] else tr(' · 待检查')), f"{g['valid']} / {g['total']}"] + [f'{x:.3f}' for x in g.get('std_xyz_mm', [float('nan')] * 3)]
            for c, v in enumerate(values):
                self.precision_table.setItem(r, c, QTableWidgetItem(v))
        if comparison:
            self.precision_result.setText(tr('参考 {0:g} mm    →    实测 {1:.3f} mm\n绝对差 {2:.3f} mm    ·    返回A偏差 {3:.3f} mm').format(reference, comparison['measured_displacement_mm'], comparison['absolute_distance_error_mm'], comparison['return_to_A_distance_mm']))
        else:
            self.precision_result.setText(tr('等待有效的A1、B、A2数据。自由运动记录不生成静止位移误差。'))

    def refresh_records(self):
        if not hasattr(self, 'records_table'):
            return
        self.record_paths = sorted((ROOT / 'data').glob('**/measurements.csv'), key=lambda p: p.parent.name, reverse=True)
        self.records_table.setRowCount(len(self.record_paths))
        for r, csv_path in enumerate(self.record_paths):
            try:
                samples, report, _ = read_session(csv_path)
                valid = sum((s['valid'] for s in samples))
                n = len(samples)
                state = {'completed': tr('已完成'), 'recording': tr('记录中 / 未正常结束'), 'error': tr('异常结束')}.get(report.get('status'), tr('历史记录'))
                values = [csv_path.parent.name, tr('黑圆标记'), str(n), f'{valid / n:.1%}', state]
            except Exception:
                values = [csv_path.parent.name, '—', '—', '—', tr('空记录 / 待检查')]
            for c, v in enumerate(values):
                self.records_table.setItem(r, c, QTableWidgetItem(v))

    def toggle_batch_delete(self, checked=False):
        self.records_table.clearSelection()
        self.records_table.setSelectionMode(QAbstractItemView.MultiSelection if checked else QAbstractItemView.SingleSelection)
        self.batch_delete.setText(tr('退出批量') if checked else tr('批量删除'))
        self.status.setText(tr('点击多条记录选中，再点红色垃圾桶删除。') if checked else tr('单选记录模式'))

    def delete_records(self):
        if self.detecting or self.pending_start or self.stopping_record:
            self.show_error(tr('请先停止检测并完成保存，再删除记录。'))
            return
        rows = sorted({i.row() for i in self.records_table.selectionModel().selectedRows()})
        paths = [self.record_paths[i].parent for i in rows]
        if not paths:
            self.status.setText(tr('请先选择要删除的记录。'))
            return
        names = '\n'.join((p.name for p in paths[:8])) + ('\n…' if len(paths) > 8 else '')
        answer = QMessageBox.question(self, tr('删除实验记录'), tr('将 {0} 条记录移入软件回收目录？\n{1}\n\n可用“撤销删除”恢复本批次，不影响研究项目原始文件。').format(len(paths), names), QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            self.last_delete_batch = move_to_bin(ROOT, paths)
            self.undo_delete.setEnabled(True)
            if self.history_path in paths:
                self.history_samples = []
                self.history_report = {}
                self.history_path = None
                for line in self.curve_lines:
                    line.setData([], [])
            if getattr(self, 'replay_folder', None) in paths:
                self.pause_replay()
                self.replay_samples = []
                self.replay_times = []
                self.replay_view.reset()
                self.slider.setRange(0, 0)
                self.replay_name.setText(tr('记录已删除，可撤销恢复'))
                self.replay_xyz.setText('X：—  Y：—  Z：—')
                self.replay_curves.setEnabled(False)
            if self.last_session in paths:
                self.last_session = None
                self.live.clear()
                self.trajectory.reset()
                self.update_xyz(None)
            self.refresh_records()
            self.record_detail.setText(tr('记录已移入软件回收目录。'))
            self.note_edit.clear()
            self.update_precision()
            self.status.setText(tr('已删除 {0} 条记录，可撤销。').format(len(paths)))
        except Exception as error:
            self.show_error(str(error))

    def undo_records_delete(self):
        if not self.last_delete_batch:
            return
        try:
            restored = restore_batch(ROOT, self.last_delete_batch)
            self.last_delete_batch = None
            self.undo_delete.setEnabled(False)
            self.refresh_records()
            self.status.setText(tr('已恢复 {0} 条记录。').format(len(restored)))
        except Exception as error:
            self.show_error(str(error))

    def selected_record(self):
        r = self.records_table.currentRow()
        return self.record_paths[r].parent if 0 <= r < len(self.record_paths) else None

    def record_selected(self):
        path = self.selected_record()
        if not path:
            return
        note = path / 'notes.json'
        self.note_edit.setText(json.loads(note.read_text(encoding='utf-8')).get('note', '') if note.exists() else '')
        try:
            _, report, _ = read_session(path)
            sha = report.get('calibration_sha256', tr('未记录'))
            extra = tr(' · 含用户实验注释') if report.get('user_annotations') else ''
            self.record_detail.setText(tr('{0} · 标定 {1} · 主机读取时间戳{2}').format(path.name, sha[:16], extra))
        except Exception as e:
            self.record_detail.setText(str(e))

    def save_note(self):
        path = self.selected_record()
        if path:
            try:
                write_json(path / 'notes.json', {'note': self.note_edit.text()})
                self.status.setText(tr('备注已保存'))
            except OSError as e:
                self.show_error(str(e))

    def open_record_folder(self):
        path = self.selected_record()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def import_history(self):
        name, _ = QFileDialog.getOpenFileName(self, tr('选择测量CSV'), str(ROOT / 'data'), tr('测量数据 (*.csv)'))
        if name:
            self.load_history(Path(name))

    def load_history(self, path):
        try:
            self.history_samples, self.history_report, self.history_path = read_session(path)
            self.history_rotation = self.rotation_for_report(self.history_report)
            self.curve_source.setCurrentIndex(1)
            self.precision_source.setCurrentIndex(1)
            self.update_curves()
            self.update_precision()
            self.status.setText(tr('已加载历史记录：') + self.history_path.name)
        except Exception as e:
            self.show_error(str(e))

    def import_replay(self):
        if self.detecting or self.pending_start or self.stopping_record:
            return
        name, _ = QFileDialog.getOpenFileName(self, tr('选择回放CSV'), str(ROOT / 'data'), tr('测量数据 (*.csv)'))
        if name:
            self.load_replay(Path(name))

    def replay_record(self):
        if self.detecting or self.pending_start or self.stopping_record:
            QMessageBox.information(self, tr('请先停止检测'), tr('停止实时检测后再进入回放。'))
            return
        path = self.selected_record()
        if path:
            self.load_replay(path)
            self.navigate('replay')

    def load_replay(self, path):
        self.pause_replay()
        try:
            self.replay_samples, report, folder = read_session(path)
            self.replay_folder = folder
            self.replay_report = report
            self.replay_rotation = self.rotation_for_report(report)
            self.replay_view.set_coordinate_frame(self.replay_rotation, self.frame_metadata['name'])
            self.replay_times = [s['t'] for s in self.replay_samples]
            self.replay_curves.setEnabled(True)
            self.replay_name.setText(folder.name + tr(' · 历史实测'))
            warning = tr('坐标与轨迹回放；视频暂不联动。') if (folder / 'raw.avi').exists() else tr('此记录无连续影像，仅回放坐标与轨迹；缺失点不插值。')
            if report.get('environment_events'):
                warning += tr(' 相机移动后环境坐标已暂停；原始数据仍在CSV中。')
            if report.get('user_annotations'):
                warning += tr(' 含实验注释，请勿把未按要求执行的运动段作为性能结论。')
            if not report:
                warning += tr(' 该CSV缺少实验元数据。')
            self.replay_warning.setText(warning)
            self.replay_view.reset()
            self.slider.setRange(0, len(self.replay_samples) - 1)
            self.slider.setValue(0)
            self.seek_replay(0)
            self.load_history(path)
        except Exception as e:
            self.show_error(str(e))

    def toggle_play(self):
        if not self.replay_samples:
            return
        if self.playing:
            self.pause_replay()
            return
        if self.slider.value() >= len(self.replay_samples) - 1:
            self.slider.setValue(0)
        self.replay_base = self.replay_times[self.slider.value()]
        self.play_started = time.perf_counter()
        self.playing = True
        self.play_button.setText(tr('暂停'))

    def pause_replay(self):
        self.playing = False
        if hasattr(self, 'play_button'):
            self.play_button.setText(tr('播放'))

    def change_speed(self):
        if self.playing:
            self.replay_base = self.replay_times[self.slider.value()]
            self.play_started = time.perf_counter()

    def user_seek_replay(self, index):
        self.pause_replay()
        self.seek_replay(index)

    def seek_replay(self, index):
        if not self.replay_samples:
            return
        self.replay_index = index
        sample = self.replay_samples[index]
        self.replay_clock.setText(f"{sample['t']:.2f} / {self.replay_times[-1]:.2f} s")
        origin = first_origin(self.replay_samples)
        if self.replay_view.anchor is None and origin is not None:
            self.replay_view.anchor = origin * 1000
            self.replay_view.reset_view()
        if self.replay_view.denoise:
            ensure_filtered(self.replay_samples)
        displayed = self.replay_view.displayed_point(sample['filtered_xyz'] if self.replay_view.denoise else sample['xyz'])
        self.replay_xyz.setText('    '.join((f'{axis}：{displayed[i]:.2f}' for i, axis in enumerate('XYZ'))) + ' mm' if sample['valid'] and sample.get('environment_valid', True) else tr('X：—    Y：—    Z：—    该帧无有效测量'))
        self.replay_view.set_samples(self.replay_samples[:index + 1])
        if self.curve_source.currentIndex() == 1:
            self.update_curves()

    def show_error(self, text):
        logging.error(text)
        QMessageBox.warning(self, tr('操作提示'), text)

    def nativeEvent(self, event_type, message):
        guard = getattr(self, 'taskbar_guard', None)
        if guard is not None:
            guard.native_message(message)
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if not self.camera_setup.prepare_shutdown():
            event.ignore();self.status.setText(tr('正在结束相机配置任务，请稍候。'));QTimer.singleShot(200,self.close);return
        if self.env_worker:
            self.env_worker.stop()
            self.env_worker.wait(3000)
            if self.env_worker.isRunning():
                event.ignore()
                self.status.setText(tr('环境分析正在结束，请稍后再关闭。'))
                return
        if self.worker:
            self.worker.stop_event.set()
            self.worker.wait(3000)
            if self.worker.isRunning():
                event.ignore()
                self.status.setText(tr('正在等待相机释放并保存，请稍后再次关闭窗口。'))
                return
        self.pause_replay()
        if hasattr(self, 'taskbar_guard'):
            self.taskbar_guard.timer.stop()
        event.accept()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    (ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=ROOT / 'logs/application.log', level=logging.INFO, encoding='utf-8', format='%(asctime)s %(levelname)s %(message)s')
    configure_windows_identity()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName('Stereo Motion Measurement')
    app.setOrganizationName('Siri.y')
    app.setWindowIcon(application_icon())
    app.setFont(QFont('Microsoft YaHei', 10))
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    pg.setConfigOptions(antialias=True, foreground='#61718a')
    while True:
        set_language(settings().get('language', 'en'))
        window = Window()
        window.show()
        if getattr(app, 'language_reopen', False):
            window.navigate('settings')

        def exception_hook(typ, value, tb):
            logging.error('Unhandled', exc_info=(typ, value, tb))
            QMessageBox.critical(window, tr('软件异常'), tr('{0}\n详细信息已写入logs/application.log。').format(value))
        sys.excepthook = exception_hook
        if args.smoke_test:
            from release_smoke import run_smoke
            QTimer.singleShot(800, lambda: run_smoke(window, app))
        result = app.exec()
        if result != 42:
            break
        window.deleteLater()
        app.processEvents()
        app.language_reopen = True
    sys.exit(result)
if __name__ == '__main__':
    main()
