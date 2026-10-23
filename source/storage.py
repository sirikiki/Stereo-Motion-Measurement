from i18n import tr, set_language
'Portable session storage and historical CSV compatibility. Coordinates stay in metres.'
import csv
import hashlib
import json
import queue
import shutil
import sys
import threading
import logging
import math
from datetime import datetime
from pathlib import Path
import numpy as np
from filtering import OneEuro, PARAMETERS
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
FIELDS = ['host_read_complete_ns', 'phase', 'attempt', 'phase_elapsed_s', 'valid', 'x_m', 'y_m', 'z_m', 'left_u_px', 'left_v_px', 'right_u_px', 'right_v_px', 'vertical_error_px', 'reason', 'disparity_px', 'left_radius_px', 'right_radius_px', 'left_aspect_ratio', 'right_aspect_ratio', 'diagnostic_image']
FIELDS += ['relative_camera_x_m', 'relative_camera_y_m', 'relative_camera_z_m', 'display_absolute_x_m', 'display_absolute_y_m', 'display_absolute_z_m', 'display_relative_x_m', 'display_relative_y_m', 'display_relative_z_m']
FIELDS += ['filtered_camera_x_m', 'filtered_camera_y_m', 'filtered_camera_z_m']
FIELDS += ['environment_valid']
DEFAULTS = {'language': 'en', 'camera_index': 0, 'trail_seconds': 10, 'record_video': False, 'reference_mm': 100, 'calibration': '', 'diagnostic_images': False, 'display_alignment': 'camera', 'depth_min_m': 0.2, 'depth_max_m': 3.0, 'stop_on_camera_motion': True}

from camera_setup import PROFILE
DEFAULTS.update(PROFILE, calibration_profile=None, calibration_sha256=None, camera_actual=None)

def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)

def settings(path=None):
    value = dict(DEFAULTS)
    p = Path(path) if path else ROOT / 'config/settings.json'
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding='utf-8'))
            if not isinstance(loaded, dict):
                raise ValueError(tr('设置必须为对象'))
            bounds = {'camera_index': (0, 20), 'trail_seconds': (2, 120), 'reference_mm': (10, 500), 'depth_min_m': (0.1, 2), 'depth_max_m': (0.3, 5)}
            for key, (low, high) in bounds.items():
                n = loaded.get(key, value[key])
                if isinstance(n, bool) or not isinstance(n, (int, float)) or (not math.isfinite(n)) or (not low <= n <= high):
                    raise ValueError(tr('无效设置：{0}').format(key))
                if key in ['camera_index', 'trail_seconds'] and n != int(n):
                    raise ValueError(tr('{0}需要整数').format(key))
            if loaded.get('depth_min_m', 0.2) >= loaded.get('depth_max_m', 3):
                raise ValueError(tr('最近距离必须小于最远距离'))
            if not isinstance(loaded.get('record_video', False), bool):
                raise ValueError(tr('录像设置无效'))
            if not isinstance(loaded.get('diagnostic_images', True), bool):
                raise ValueError(tr('诊断图像设置无效'))
            if not isinstance(loaded.get('stop_on_camera_motion', True), bool):
                raise ValueError(tr('相机移动设置无效'))
            if loaded.get('display_alignment', 'camera') not in ['camera', 'hand_estimate']:
                raise ValueError(tr('显示坐标设置无效'))
            if not isinstance(loaded.get('calibration', value['calibration']), str):
                raise ValueError(tr('标定路径无效'))
            from camera_setup import profile
            profile(loaded)
            value.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        except (ValueError, TypeError) as error:
            backup = p.with_name('settings.invalid_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
            shutil.copy2(p, backup)
            logging.warning('Invalid settings retained at %s: %s', backup, error)
            value['_load_warning'] = tr('设置文件异常，已保留备份并采用默认值。')
    return value

def read_session(path):
    path = Path(path)
    csv_path = path if path.suffix.lower() == '.csv' else path / 'measurements.csv'
    with csv_path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError(tr('该记录没有测量数据。'))
    required = {'host_read_complete_ns', 'valid', 'x_m', 'y_m', 'z_m'}
    if not required.issubset(rows[0]):
        raise ValueError(tr('CSV缺少时间戳、有效状态或XYZ字段。'))
    start = int(rows[0]['host_read_complete_ns'])
    result = []
    last = -1
    filtering = OneEuro()
    for row in rows:
        timestamp = int(row['host_read_complete_ns'])
        if timestamp < last:
            raise ValueError(tr('时间戳倒退，无法可靠回放。'))
        last = timestamp
        xyz = np.array([float(row[k]) if row[k] else np.nan for k in ['x_m', 'y_m', 'z_m']])
        valid = str(row['valid']).lower() in ('1', 'true') and bool(np.isfinite(xyz).all())
        result.append({'t': (timestamp - start) / 1000000000.0, 'xyz': xyz if valid else np.full(3, np.nan), 'filtered_xyz': filtering.step(timestamp / 1000000000.0, xyz, valid), 'valid': valid, 'environment_valid': row.get('environment_valid', '1') not in ['0', 'false', 'False'], 'phase': row.get('phase', 'FREE'), 'attempt': int(row.get('attempt') or 1), 'reason': row.get('reason', '')})
    report_path = csv_path.parent / 'report.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    annotations_path = csv_path.parent / 'user_annotations.json'
    if annotations_path.exists():
        report['user_annotations'] = json.loads(annotations_path.read_text(encoding='utf-8'))
    return (result, report, csv_path.parent)

def statistics(samples, reference_mm=100):
    groups = {}
    for name in ['A1', 'B', 'A2']:
        rows = [s for s in samples if s['phase'] == name]
        if not rows:
            continue
        latest = max((s['attempt'] for s in rows))
        rows = [s for s in rows if s['attempt'] == latest]
        pts = np.array([s['xyz'] for s in rows if s['valid'] and s.get('environment_valid', True)])
        n = len(pts)
        duration = rows[-1]['t'] - rows[0]['t'] if rows else 0
        groups[name] = {'valid': n, 'total': len(rows), 'attempt': latest, 'duration_s': duration, 'accepted': n >= 20 and n / len(rows) >= 0.8 and (duration >= 4.5)}
        if n:
            groups[name].update(median_xyz_m=np.median(pts, axis=0).tolist(), std_xyz_mm=(np.std(pts, axis=0) * 1000).tolist())
    comparison = None
    if all((groups.get(k, {}).get('accepted') for k in ['A1', 'B', 'A2'])):
        a, b, a2 = [np.array(groups[k]['median_xyz_m']) for k in ['A1', 'B', 'A2']]
        d = float(np.linalg.norm(b - a) * 1000)
        comparison = {'measured_displacement_mm': d, 'reference_displacement_mm': reference_mm, 'absolute_distance_error_mm': abs(d - reference_mm), 'return_to_A_distance_mm': float(np.linalg.norm(a2 - a) * 1000)}
    return (groups, comparison)

class SessionWriter:
    """Bounded queue: disk failures/backpressure abort capture instead of silently dropping rows."""

    def __init__(self, config, calibration, root=None):
        base = Path(root) if root else ROOT / 'data/sessions'
        self.path = base / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.path.mkdir(parents=True)
        shutil.copy2(calibration, self.path / 'calibration.npz')
        self.report = {'status': 'recording', 'created_at': datetime.now().astimezone().isoformat(), 'units': 'm', 'coordinate_frame': 'rectified_first_view_camera', 'timestamp_source': 'host read completion; not exposure time', 'marker': 'black planar circle image centroid approximation; not verified sphere centre', 'calibration_sha256': hashlib.sha256(Path(calibration).read_bytes()).hexdigest(), 'settings': dict(config), 'recorded_rows': 0, 'valid_rows': 0, 'reference_displacement_mm': config['reference_mm'], 'environment_reference': config.get('environment_reference'), 'environment_events': []}
        from coordinates import CAMERA_TO_SCENE
        self.rotation = np.array(config.get('display_frame_metadata', {}).get('camera_to_display', CAMERA_TO_SCENE), dtype=float)
        self.origin = None
        self.filter = OneEuro()
        self.report['denoise'] = dict(PARAMETERS, raw_preserved=True, limitation='filtered estimate, not measured ground truth or intentional-motion classification')
        self.report['position_reference'] = {'policy': 'first valid measurement after start; fixed for entire session', 'origin_camera_m': None, 'origin_host_read_complete_ns': None, 'camera_to_display': self.rotation.tolist(), 'absolute_definition': 'rectified first camera optical origin; not surveyed laboratory position', 'relative_definition': 'R @ (camera_xyz - first_valid_camera_xyz)'}
        write_json(self.path / 'report.json', self.report)
        self.q = queue.Queue(maxsize=512)
        self.error = None
        self.thread = threading.Thread(target=self._run, name='session-writer', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with (self.path / 'measurements.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                while True:
                    row = self.q.get()
                    if row is None:
                        break
                    if isinstance(row, tuple):
                        if row[0] == 'environment_event':
                            self.report['environment_events'].append(row[1])
                            write_json(self.path / 'report.json', self.report)
                            continue
                        _, name, image = row
                        import cv2
                        ok, encoded = cv2.imencode('.png', image)
                        if not ok:
                            raise OSError(tr('诊断图像编码失败'))
                        folder = self.path / 'diagnostic_images'
                        folder.mkdir(exist_ok=True)
                        encoded.tofile(str(folder / name))
                        continue
                    row = dict(row)
                    is_valid = bool(row.get('valid'))
                    raw_point = np.array([row[k] for k in ['x_m', 'y_m', 'z_m']], dtype=float) if is_valid else np.full(3, np.nan)
                    filtered = self.filter.step(int(row['host_read_complete_ns']) / 1000000000.0, raw_point, is_valid)
                    for i, axis in enumerate('xyz'):
                        row[f'filtered_camera_{axis}_m'] = float(filtered[i]) if np.isfinite(filtered[i]) else ''
                    if row.get('valid'):
                        xyz = np.array([row[k] for k in ['x_m', 'y_m', 'z_m']], dtype=float)
                        if np.isfinite(xyz).all():
                            if self.origin is None:
                                self.origin = xyz.copy()
                                self.report['position_reference'].update(origin_camera_m=xyz.tolist(), origin_host_read_complete_ns=row['host_read_complete_ns'])
                                write_json(self.path / 'report.json', self.report)
                            relative = xyz - self.origin
                            values = [('relative_camera', relative)]
                            if row.get('environment_valid', 1):
                                values.extend([('display_absolute', self.rotation @ xyz), ('display_relative', self.rotation @ relative)])
                            for prefix, point in values:
                                row.update({f'{prefix}_{axis}_m': point[i] for i, axis in enumerate('xyz')})
                    writer.writerow(row)
                    self.report['recorded_rows'] += 1
                    self.report['valid_rows'] += int(row['valid'])
                    if self.report['recorded_rows'] % 30 == 0:
                        f.flush()
                f.flush()
        except Exception as error:
            self.error = str(error)

    def append(self, row):
        if self.error:
            raise OSError(self.error)
        try:
            self.q.put(row, timeout=0.25)
        except queue.Full:
            raise OSError(tr('磁盘写入队列已满，已停止采集以避免静默丢失记录。'))

    def save_image(self, name, image):
        self.append(('image', name, image.copy()))

    def close(self, error=None):
        while self.thread.is_alive():
            try:
                self.q.put(None, timeout=0.2)
                break
            except queue.Full:
                if self.error:
                    break
        self.thread.join()
        error = error or self.error
        self.report['status'] = 'error' if error else 'completed'
        self.report['error'] = error
        self.report['ended_at'] = datetime.now().astimezone().isoformat()
        if self.report['recorded_rows']:
            samples, _, _ = read_session(self.path)
            groups, comparison = statistics(samples, self.report['reference_displacement_mm'])
            self.report.update(static_results=groups, reference_comparison=comparison)
        write_json(self.path / 'report.json', self.report)
        if error:
            (self.path / 'error.txt').write_text(error, encoding='utf-8')
