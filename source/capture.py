from i18n import tr, set_language
'Persistent preview with independently controlled recording sessions.'
import queue
import threading
import time
from collections import deque
import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from storage import ROOT, SessionWriter
from marker_tracking import detect_black_circle
from stereo_reconstruction import StereoGeometry
from filtering import OneEuro

class CaptureWorker(QThread):
    camera_opened = Signal()
    opened = Signal(str)
    recording_stopped = Signal(str)
    failed = Signal(str)
    phase_finished = Signal(str)

    def __init__(self, config, auto_record=True):
        super().__init__()
        self.config = dict(config)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.hints = [None, None]
        self.packet = None
        self.samples = deque()
        self.phase = None
        self.attempts = {}
        self.path = None
        self.error = None
        self.commands = queue.Queue()
        if auto_record:
            self.commands.put('start')

    def request_start(self, config=None):
        self.commands.put(('start', dict(config) if config else None))

    def request_stop(self):
        self.commands.put('stop')

    def invalidate_environment(self, details):
        self.commands.put(('invalidate_environment', dict(details)))

    def set_hint(self, side, x, y):
        with self.lock:
            self.hints[side] = (x, y)

    def begin_phase(self, name):
        with self.lock:
            self.attempts[name] = self.attempts.get(name, 0) + 1
            self.phase = (name, time.perf_counter(), self.attempts[name])

    def take(self):
        with self.lock:
            samples = list(self.samples)
            self.samples.clear()
            packet = self.packet
            self.packet = None
        return (packet, samples)

    def run(self):
        cap = writer = video = None
        calibration = ROOT / self.config['calibration']
        session_start = 0
        session_count = good = 0
        environment_valid = True
        try:
            geometry = StereoGeometry(calibration, self.config.get('depth_min_m', 0.2), self.config.get('depth_max_m', 3.0))
            cap = cv2.VideoCapture(self.config['camera_index'], cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError(tr('无法打开相机。请检查设备编号、USB连接，并关闭其他相机程序。'))
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2560)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 960)
            cap.set(cv2.CAP_PROP_FPS, 60)
            self.camera_opened.emit()
            camera_start = time.perf_counter()
            camera_count = 0
            last_publish = 0
            previous_sizes = [None, None]
            previous_hints = [None, None]
            misses = [0, 0]
            while not self.stop_event.is_set():
                while not self.commands.empty():
                    command = self.commands.get_nowait()
                    if isinstance(command, tuple) and command[0] == 'invalidate_environment':
                        if writer:
                            environment_valid = False
                            writer.append(('environment_event', dict(command[1], effective_ns=time.perf_counter_ns(), action='stop' if self.config.get('stop_on_camera_motion', True) else 'continue_raw')))
                        continue
                    if isinstance(command, tuple):
                        command, updated = command
                        if updated:
                            self.config.update(updated)
                    if command == 'start' and writer is None:
                        writer = SessionWriter(self.config, calibration)
                        self.path = writer.path
                        filtering = OneEuro()
                        environment_valid = True
                        geometry.min_depth = self.config.get('depth_min_m', 0.2)
                        geometry.max_depth = self.config.get('depth_max_m', 3.0)
                        session_start = time.perf_counter()
                        session_count = good = 0
                        diagnostic_count = 0
                        next_diagnostic = 0
                        with self.lock:
                            self.phase = None
                            self.attempts = {}
                        self.opened.emit(str(self.path))
                    elif command == 'stop' and writer is not None:
                        writer.close()
                        if writer.error:
                            raise OSError(writer.error)
                        writer = None
                        if video is not None:
                            video.release()
                            video = None
                        with self.lock:
                            self.phase = None
                        self.recording_stopped.emit(str(self.path))
                ok, raw = cap.read()
                ns = time.perf_counter_ns()
                now = ns / 1000000000.0
                if not ok:
                    if writer:
                        writer.append(dict(host_read_complete_ns=ns, phase='FREE', attempt=1, phase_elapsed_s=now - session_start, valid=0, x_m='', y_m='', z_m='', left_u_px='', left_v_px='', right_u_px='', right_v_px='', vertical_error_px='', reason='camera_read_failed'))
                    raise RuntimeError(tr('相机读取中断，已关闭相机并保留已有数据。'))
                views = geometry.rectify(raw)
                with self.lock:
                    hints = list(self.hints)
                    phase = self.phase
                detections = []
                for side, (im, hint) in enumerate(zip(views, hints)):
                    if hint != previous_hints[side]:
                        previous_sizes[side] = None
                        misses[side] = 0
                    detection = detect_black_circle(im, hint, previous_sizes[side])
                    if detection is None and hint is not None:
                        detection = detect_black_circle(im, hint, previous_sizes[side], search_radius=480)
                    if detection:
                        previous_sizes[side] = detection['radius_px']
                        misses[side] = 0
                    else:
                        misses[side] += 1
                        if misses[side] > 15:
                            previous_sizes[side] = None
                    detections.append(detection)
                centers = [d['center'] if d else None for d in detections]
                with self.lock:
                    for side, d in enumerate(detections):
                        if d and self.hints[side] == hints[side]:
                            self.hints[side] = d['center']
                    previous_hints = list(self.hints)
                sample = None
                if writer:
                    xyz, quality = geometry.triangulate(*centers)
                    diagnostic_image = ''
                    if self.config.get('diagnostic_images', True) and now - session_start >= next_diagnostic:
                        image_name = f'pair_{ns}.png'
                        writer.save_image(image_name, raw)
                        diagnostic_image = 'diagnostic_images/' + image_name
                        diagnostic_count += 1
                        next_diagnostic = now - session_start + 2
                    label, elapsed, attempt = ('FREE', now - session_start, 1)
                    if phase:
                        label, stamp, attempt = phase
                        elapsed = now - stamp
                        if elapsed > 5:
                            with self.lock:
                                if self.phase == phase:
                                    self.phase = None
                            self.phase_finished.emit(label)
                            label, elapsed, attempt = ('FREE', now - session_start, 1)
                    writer.append(dict(host_read_complete_ns=ns, phase=label, attempt=attempt, phase_elapsed_s=elapsed, valid=int(xyz is not None), environment_valid=int(environment_valid), x_m='' if xyz is None else xyz[0], y_m='' if xyz is None else xyz[1], z_m='' if xyz is None else xyz[2], left_u_px='' if centers[0] is None else centers[0][0], left_v_px='' if centers[0] is None else centers[0][1], right_u_px='' if centers[1] is None else centers[1][0], right_v_px='' if centers[1] is None else centers[1][1], vertical_error_px=quality.get('vertical_error_px', ''), reason=quality['reason'], disparity_px=quality.get('disparity_px', ''), left_radius_px=detections[0]['radius_px'] if detections[0] else '', right_radius_px=detections[1]['radius_px'] if detections[1] else '', left_aspect_ratio=detections[0]['aspect_ratio'] if detections[0] else '', right_aspect_ratio=detections[1]['aspect_ratio'] if detections[1] else '', diagnostic_image=diagnostic_image))
                    if self.config['record_video']:
                        if video is None:
                            video = cv2.VideoWriter(str(self.path / 'raw.avi'), cv2.VideoWriter_fourcc(*'MJPG'), 60, (2560, 960))
                            if not video.isOpened():
                                raise OSError(tr('无法创建视频文件。'))
                        video.write(raw)
                    session_count += 1
                    good += int(xyz is not None)
                    sample = dict(t=now - session_start, xyz=np.full(3, np.nan) if xyz is None else xyz, filtered_xyz=filtering.step(ns / 1000000000.0, np.full(3, np.nan) if xyz is None else xyz, xyz is not None), valid=xyz is not None, environment_valid=environment_valid, phase=label, attempt=attempt, reason=quality['reason'])
                    with self.lock:
                        self.samples.append(sample)
                camera_count += 1
                if now - last_publish >= 1 / 30:
                    with self.lock:
                        self.packet = dict(views=views, host_read_complete_ns=ns, detections=detections, sample=sample, recording=writer is not None, capture_hz=camera_count / max(now - camera_start, 0.001), valid_hz=good / max(now - session_start, 0.001) if writer else 0, valid_ratio=good / session_count if session_count else 0)
                    last_publish = now
        except Exception as error:
            self.error = str(error)
        finally:
            if cap is not None:
                cap.release()
            if video is not None:
                video.release()
            if writer is not None:
                try:
                    writer.close(self.error)
                except Exception as error:
                    self.error = tr('{0} 保存错误：{1}').format(self.error or '', error)
            if self.error:
                self.failed.emit(self.error)
