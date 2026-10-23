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
from camera_setup import open_device, split_frame, profile, require_calibration_binding, as_bgr

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
        self.metric_ready = False
        self.calibration_error = ""
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
            geometry = None
            try:
                if not self.config.get('calibration'):
                    raise ValueError(tr('尚未选择标定，请打开左侧相机配置。'))
                require_calibration_binding(self.config, calibration)
                geometry = StereoGeometry(calibration, self.config.get('depth_min_m', 0.2), self.config.get('depth_max_m', 3.0))
            except Exception as error:
                self.calibration_error = str(error)
            cap = open_device(self.config)
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
                        if camera_count == 0:
                            self.commands.put(('start', None))
                            break
                        if not self.metric_ready:
                            raise ValueError(self.calibration_error or 'Calibration is not ready.')
                        require_calibration_binding(self.config, calibration)
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
                raw_views = split_frame(raw, self.config)
                if geometry is not None and raw_views[0].shape[1::-1] != geometry.size:
                    if writer:
                        raise ValueError('Frame dimensions changed during measurement; recording stopped.')
                    self.calibration_error = 'Actual per-eye frame size differs from calibration. Raw preview only.'
                self.metric_ready = geometry is not None and raw_views[0].shape[1::-1] == geometry.size
                if self.metric_ready: self.calibration_error = ''
                views = geometry.rectify_views(raw_views) if self.metric_ready else raw_views
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
                            video = cv2.VideoWriter(str(self.path / 'raw.avi'), cv2.VideoWriter_fourcc(*'MJPG'), max(1, cap.get(cv2.CAP_PROP_FPS) or self.config.get('camera_fps', 60)), raw.shape[1::-1])
                            if not video.isOpened():
                                raise OSError(tr('无法创建视频文件。'))
                        video.write(as_bgr(raw))
                    session_count += 1
                    good += int(xyz is not None)
                    sample = dict(t=now - session_start, xyz=np.full(3, np.nan) if xyz is None else xyz, filtered_xyz=filtering.step(ns / 1000000000.0, np.full(3, np.nan) if xyz is None else xyz, xyz is not None), valid=xyz is not None, environment_valid=environment_valid, phase=label, attempt=attempt, reason=quality['reason'])
                    with self.lock:
                        self.samples.append(sample)
                camera_count += 1
                if now - last_publish >= 1 / 30:
                    with self.lock:
                        self.packet = dict(metric_ready=self.metric_ready, calibration_error=self.calibration_error, views=views, host_read_complete_ns=ns, detections=detections, sample=sample, recording=writer is not None, capture_hz=camera_count / max(now - camera_start, 0.001), valid_hz=good / max(now - session_start, 0.001) if writer else 0, valid_ratio=good / session_count if session_count else 0)
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
