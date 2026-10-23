from i18n import tr, set_language
'Optional stereo plane references. Occlusion never invalidates a stored frame.\n\nPure geometry is separate from the asynchronous Qt worker for offline checking.\nAll points/normals use the rectified first-camera frame in metres.\n'
import copy, hashlib, queue, threading, time, uuid
from collections import deque
import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from coordinates import CAMERA_TO_SCENE
from stereo_reconstruction import StereoGeometry
from plane_patches import StableCandidates, PlaneVisibility
from plane_selection import automatic_planes, manual_plane, select_hint, fit_inverse
from board_reference import BoardReference
from image_quality import observations, hint as image_quality_hint
from environment_raster import analysis_size, scale_xy, to_work, to_full, sample_image, checked_views

def depth_median(history, K):
    """Exact per-pixel finite median, without per-empty-pixel warning handling."""
    z = np.stack([a[:, :, 2] for a in history])
    count = np.isfinite(z).sum(axis=0)
    ordered = np.sort(np.where(np.isfinite(z), z, np.inf), axis=0)
    low = np.maximum((count - 1) // 2, 0)
    high = np.minimum(count // 2, len(history) - 1)
    middle = (np.take_along_axis(ordered, low[None], axis=0)[0] + np.take_along_axis(ordered, high[None], axis=0)[0]) * 0.5
    middle[count < 3] = np.nan
    yy, xx = np.indices(middle.shape)
    return np.stack([(xx - K[0, 2]) * middle / K[0, 0], (yy - K[1, 2]) * middle / K[1, 1], middle], axis=2)

def unit(v):
    v = np.asarray(v, dtype=float)
    length = np.linalg.norm(v)
    if length < 1e-08:
        raise ValueError(tr('无法唯一确定方向，请选择另一块基准面。'))
    return v / length

def reference_rotation(planes):
    up = unit(planes['horizontal']['normal']) if 'horizontal' in planes else CAMERA_TO_SCENE[2].copy()
    if up @ CAMERA_TO_SCENE[2] < 0:
        up = -up
    if 'vertical' in planes:
        normal = unit(planes['vertical']['normal'])
        if 'horizontal' in planes and abs(normal @ up) > np.sin(np.radians(10)):
            raise ValueError(tr('两个面偏离垂直关系超过10°，请重选；原基准已保留。'))
        front = unit(normal - normal @ up * up)
        if front @ CAMERA_TO_SCENE[1] < 0:
            front = -front
        right = unit(np.cross(front, up))
    else:
        right = unit(CAMERA_TO_SCENE[0] - CAMERA_TO_SCENE[0] @ up * up)
        front = unit(np.cross(up, right))
    return np.vstack([right, front, up])

def project(points, P):
    a = np.c_[np.asarray(points), np.ones(len(points))] @ P.T
    return a[:, :2] / a[:, 2:]

def fit_plane(points, seed=2026, threshold=0.006):
    points = np.asarray(points, dtype=float)
    points = points[np.isfinite(points).all(axis=1)]
    if len(points) < 200:
        return None
    rng = np.random.default_rng(seed)
    best = np.zeros(len(points), bool)
    for _ in range(150):
        a, b, c = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length < 1e-08:
            continue
        normal /= length
        mask = np.abs((points - a) @ normal) < threshold
        if mask.sum() > best.sum():
            best = mask
    if best.sum() < 200:
        return None
    for _ in range(2):
        q = points[best]
        center = np.mean(q, axis=0)
        _, singular, V = np.linalg.svd(q - center, full_matrices=False)
        normal = V[-1]
        best = np.abs((points - center) @ normal) < threshold
    q = points[best]
    if len(q) < 200:
        return None
    center = np.mean(q, axis=0)
    _, singular, V = np.linalg.svd(q - center, full_matrices=False)
    normal = unit(V[-1])
    if singular[1] / max(singular[0], 1e-09) < 0.12:
        return None
    rms = float(np.sqrt(np.mean(((q - center) @ normal) ** 2)))
    if rms > 0.008:
        return None
    return dict(normal=normal, center=center, mask=best, rms_m=rms, count=int(best.sum()))

def plane_geometry(fit, points, P1, P2):
    n = fit['normal']
    center = fit['center']
    q = points[fit['mask']]
    tangent = unit(np.cross(n, [0, 0, 1])) if abs(n[2]) < 0.95 else unit(np.cross(n, [0, 1, 0]))
    other = unit(np.cross(n, tangent))
    basis = np.vstack([tangent, other])
    local = (q - center) @ basis.T
    low, high = np.percentile(local, [5, 95], axis=0)
    if np.min(high - low) < 0.04:
        return None
    lines = []
    for fraction in np.linspace(0, 1, 8):
        x = low[0] + fraction * (high[0] - low[0])
        y = low[1] + fraction * (high[1] - low[1])
        lines.extend([[center + x * tangent + low[1] * other, center + x * tangent + high[1] * other], [center + low[0] * tangent + y * other, center + high[0] * tangent + y * other]])
    lines = np.array(lines)
    corners = center + np.array([[low[0], low[1]], [high[0], low[1]], [high[0], high[1]], [low[0], high[1]]]) @ basis
    return dict(id=uuid.uuid4().hex, normal=n.tolist(), center_m=center.tolist(), offset_m=float(-n @ center), grid_3d_m=lines.tolist(), polygons=[project(corners, p).tolist() for p in [P1, P2]], grids=[project(lines.reshape(-1, 3), p).reshape(-1, 2, 2).tolist() for p in [P1, P2]], quality=dict(inliers=fit['count'], rms_mm=fit['rms_m'] * 1000))

class StereoPlanes:

    def __init__(self, geometry):
        self.geometry = geometry
        p1,p2=geometry.parameters['P1'],geometry.parameters['P2']
        if (min(p1[0,0],p1[1,1]) <= 0 or
            not np.allclose(p1[2,:3],[0,0,1],atol=1e-8) or
            not np.allclose([p1[0,1],p1[1,0]],0,atol=1e-8) or
            not np.allclose(p1[:,:3],p2[:,:3],atol=1e-6) or
            not np.allclose(p1[:,3],0,atol=1e-8) or
            not np.allclose(p2[1:,3],0,atol=1e-8) or p2[0,3]>=0):
            raise ValueError(tr('环境深度需要水平、零视差校正参数，请在相机配置中重新导入或生成标定。'))
        self.size = analysis_size(geometry.size)
        self.scale = scale_xy(self.size[::-1], geometry.size)
        self.matcher = cv2.StereoSGBM_create(minDisparity=0, numDisparities=192, blockSize=5, P1=200, P2=800, disp12MaxDiff=1, preFilterCap=31, uniquenessRatio=12, speckleWindowSize=80, speckleRange=2, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.right_matcher = cv2.StereoSGBM_create(minDisparity=-192, numDisparities=192, blockSize=5, P1=200, P2=800, disp12MaxDiff=1, preFilterCap=31, uniquenessRatio=12, speckleWindowSize=80, speckleRange=2, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.K = self.geometry.parameters['P1'][:, :3].copy()
        self.K[:2] *= self.scale[:, None]
        self.baseline = float(abs(self.geometry.parameters['P2'][0, 3] / self.geometry.parameters['P2'][0, 0]))
        self.far_matcher = cv2.StereoSGBM_create(minDisparity=0, numDisparities=64, blockSize=9, P1=648, P2=2592, uniquenessRatio=12, speckleWindowSize=80, speckleRange=1, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.far_right = cv2.StereoSGBM_create(minDisparity=-64, numDisparities=64, blockSize=9, P1=648, P2=2592, uniquenessRatio=12, speckleWindowSize=80, speckleRange=1, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.normalized_matcher = cv2.StereoSGBM_create(minDisparity=0, numDisparities=192, blockSize=15, P1=1800, P2=7200, uniquenessRatio=12, speckleWindowSize=80, speckleRange=1, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        self.normalized_right = cv2.StereoSGBM_create(minDisparity=-192, numDisparities=192, blockSize=15, P1=1800, P2=7200, uniquenessRatio=12, speckleWindowSize=80, speckleRange=1, mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
        for m in (self.matcher, self.right_matcher, self.far_matcher, self.far_right, self.normalized_matcher, self.normalized_right):
            # Range follows f*B/Z, bounded by image width for SGBM.
            near = 0.6 if m in (self.far_matcher, self.far_right) else 0.2
            desired = int(np.ceil((self.K[0, 0]*self.baseline/near + 16)/16))*16
            count = max(16, min(desired, ((self.size[0] - 32) // 16) * 16))
            m.setNumDisparities(count)
            if m.getMinDisparity() < 0: m.setMinDisparity(-count)
        self.depth_limits_m = (0.2, 5.0)
        self.sampling_info = dict(image_size=list(geometry.size), analysis_size=list(self.size),
            scale_xy=self.scale.tolist(), focal_px=float(self.K[0,0]), baseline_m=self.baseline,
            disparity_count=self.matcher.getNumDisparities(),
            nearest_searchable_m=float(self.K[0,0]*self.baseline/(self.matcher.getNumDisparities()-1)),
            depth_limits_m=list(self.depth_limits_m), sampling='origin_preserving_remap')
        import logging
        logging.info('Environment sampling: %s', self.sampling_info)
        self.alternative_xyz = None
        self.depth_history = deque(maxlen=5)
        self.primary_history = deque(maxlen=5)
        self.previous_gray = None
        self.cached_candidates = {}
        self.enhance = True
        self.selection_history = deque(maxlen=6)
        self.selection_kind = None

    def depth(self, views):
        views = checked_views(views, self.geometry.size)
        self.alternative_xyz = None
        gray = [sample_image(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), self.size) for im in views]
        left = self.matcher.compute(*gray).astype(float) / 16
        right = self.right_matcher.compute(gray[1], gray[0]).astype(float) / 16
        yy, xx = np.indices(left.shape)
        xr = np.rint(xx - left).astype(int)
        inside = (xr >= 0) & (xr < left.shape[1])
        matching = right[yy, np.clip(xr, 0, left.shape[1] - 1)]
        valid = (left > 1) & inside & (np.abs(left + matching) < 1.5)
        far = self.far_matcher.compute(*gray).astype(float) / 16
        far_right = self.far_right.compute(gray[1], gray[0]).astype(float) / 16
        fxr = np.rint(xx - far).astype(int)
        far_valid = (far > 2) & (fxr >= 0) & (fxr < self.size[0]) & (abs(far + far_right[yy, np.clip(fxr, 0, self.size[0] - 1)]) < 1)
        use = ~valid & far_valid
        left[use] = far[use]
        valid |= use
        z = self.K[0, 0] * self.baseline / np.maximum(left, 0.001)
        valid &= (z >= 0.2) & (z <= 5.0)
        xyz = np.stack([(xx - self.K[0, 2]) * z / self.K[0, 0], (yy - self.K[1, 2]) * z / self.K[1, 1], z], axis=2)
        xyz[~valid] = np.nan
        self.cached_candidates = {}
        if not self.enhance:
            return (gray, xyz)

        def normalize(im):
            a = im.astype(np.float32)
            mean = cv2.boxFilter(a, -1, (31, 31))
            std = np.sqrt(np.maximum(cv2.boxFilter(a * a, -1, (31, 31)) - mean * mean, 0))
            return np.clip(128 + 32 * (a - mean) / np.maximum(std, 8), 0, 255).astype(np.uint8)
        ng = [normalize(im) for im in gray]
        nd = self.normalized_matcher.compute(*ng).astype(float) / 16
        nr = self.normalized_right.compute(ng[1], ng[0]).astype(float) / 16
        nx = np.rint(xx - nd).astype(int)
        nv = (nd > 2) & (nx >= 0) & (nx < self.size[0]) & (abs(nd + nr[yy, np.clip(nx, 0, self.size[0] - 1)]) < 1)
        nz = self.K[0, 0] * self.baseline / np.maximum(nd, 0.001)
        nv &= (nz >= 0.2) & (nz <= 5)
        self.alternative_xyz = np.stack([(xx - self.K[0, 2]) * nz / self.K[0, 0], (yy - self.K[1, 2]) * nz / self.K[1, 1], nz], axis=2)
        self.alternative_xyz[~nv] = np.nan
        if self.previous_gray is not None and any(np.mean(cv2.absdiff(current, previous) > 20) > 0.12 for current, previous in zip(gray, self.previous_gray)):
            self.depth_history.clear()
            self.primary_history.clear()
            self.selection_history.clear()
        self.previous_gray = [im.copy() for im in gray]
        self.primary_history.append(xyz.copy())
        self.depth_history.append(self.alternative_xyz.copy())
        if len(self.depth_history) >= 3:
            self.alternative_xyz = depth_median(self.depth_history, self.K)
        return (gray, xyz)

    def candidates(self, xyz, kind):
        if kind in self.cached_candidates:
            return copy.deepcopy(self.cached_candidates[kind])
        result = automatic_planes(xyz, kind, self.geometry.parameters['P1'], self.geometry.parameters['P2'], image_size=self.geometry.size)
        if not result and self.alternative_xyz is not None:
            result = automatic_planes(self.alternative_xyz, kind, self.geometry.parameters['P1'], self.geometry.parameters['P2'], image_size=self.geometry.size)
            for p in result:
                p['quality']['depth_source'] = 'local_brightness_normalized_stereo'
        self.cached_candidates[kind] = copy.deepcopy(result)
        return result

    def observe_candidates(self, xyz, kind):
        if self.selection_kind != kind:
            self.selection_history.clear()
            self.selection_kind = kind
        result = self.candidates(xyz, kind)
        self.selection_history.append(copy.deepcopy(result))
        return result

    def manual_candidate(self, polygon, side, kind, cancel=None, progress=None):

        def update(message):
            if cancel is not None and cancel.is_set():
                raise InterruptedError(tr('已取消，原基准保留。'))
            if progress:
                progress(message)
        update(tr('正在检查取消状态…'))
        P1, P2 = (self.geometry.parameters['P1'], self.geometry.parameters['P2'])
        tracker = StableCandidates(P1, P2)
        locked = []
        if self.selection_kind == kind:
            update(tr('正在匹配圈选位置与已计算候选…'))
            for index, choices in enumerate(self.selection_history):
                try:
                    plane = select_hint(copy.deepcopy(choices), polygon, side, self.size[::-1], self.geometry.size)
                    locked = tracker.update([plane], index * 0.4)
                except ValueError:
                    locked = tracker.update([], index * 0.4)
            if locked:
                return locked[0]
        if len(self.primary_history) < 4:
            raise ValueError(tr('请保持场景不动几秒，积累足够观测后再圈选。'))
        update(tr('正在局部搜索圈选区域…'))
        source = self.primary_history
        try:
            plane = manual_plane(source[-1], polygon, side, kind, P1, P2, image_size=self.geometry.size)
        except ValueError:
            update(tr('正在检查亮度增强后的局部深度…'))
            source = self.depth_history
            plane = manual_plane(source[-1], polygon, side, kind, P1, P2, image_size=self.geometry.size)
            plane['quality']['depth_source'] = 'local_brightness_normalized_stereo'
        mask = np.zeros(source[-1].shape[:2], np.uint8)
        cv2.fillPoly(mask, [np.rint(to_work(plane['polygons'][0], self.size[::-1], self.geometry.size)).astype(np.int32)], 1)
        tracker.reset()
        locked = []
        threshold = 0.45 / (abs(P2[0, 3]) * self.scale[0])
        for index, xyz in enumerate(source):
            update(tr('正在核对局部方向 ') + str(index + 1) + '/' + str(len(source)) + '…')
            points = xyz[mask.astype(bool)]
            points = points[np.isfinite(points).all(axis=1)]
            n = np.array(plane['normal'])
            offset = plane['offset_m']
            points = points[abs(points @ n + offset) < np.maximum(0.003, points[:, 2] * points[:, 2] * threshold * 2)]
            if len(points) > 1500:
                points = points[np.linspace(0, len(points) - 1, 1500).astype(int)]
            fit = fit_inverse(points, threshold, seed=44 + index)
            if fit is None:
                locked = tracker.update([], index * 0.4)
                continue
            candidate = copy.deepcopy(plane)
            candidate.update(normal=fit['normal'].tolist(), center_m=fit['center'].tolist(), offset_m=float(fit['offset']))
            locked = tracker.update([candidate], index * 0.4)
        update(tr('正在确认结果…'))
        if not locked:
            raise ValueError(tr('圈选附近没有稳定平面。可扩大有细节的区域，或开启棋盘格辅助；原基准未改变。'))
        return locked[0]

class MotionMonitor:

    def __init__(self, K, image_size, work_size):
        self.K = K
        self.image_size, self.work_size = image_size, work_size
        self.orb = cv2.ORB_create(nfeatures=2400)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.reset()

    def reset(self):
        self.points = np.empty((0, 3))
        self.uv = np.empty((0, 2))
        self.descriptors = None
        self.last_pose = None
        self.streak = 0

    def anchor(self, gray, xyz):
        self.reset()
        keys, desc = self.orb.detectAndCompute(gray, None)
        if desc is None:
            return
        selected = []
        points = []
        uv = []
        for i, key in enumerate(keys):
            x, y = np.rint(key.pt).astype(int)
            patch = xyz[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 3)
            patch = patch[np.isfinite(patch).all(axis=1)]
            if len(patch) < 4:
                continue
            points.append(np.median(patch, axis=0))
            uv.append(key.pt)
            selected.append(i)
        if len(selected) < 30:
            return
        self.points = np.array(points)
        self.uv = np.array(uv)
        self.descriptors = desc[selected].copy()

    def evaluate_pose(self, rvec, tvec, inliers, errors, coverage):
        reliable = inliers >= 30 and errors <= 1.5 and (coverage >= 3)
        if not reliable:
            self.streak = 0
            return False
        rotation = cv2.Rodrigues(np.asarray(rvec))[0]
        translation = np.asarray(tvec).reshape(3)
        angle = np.degrees(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1)))
        moved = np.linalg.norm(translation) > 0.01 or angle > 1
        if not moved:
            self.streak = 0
            self.last_pose = None
            return False
        consistent = self.last_pose is None or (np.linalg.norm(translation - self.last_pose[1]) < 0.02 and np.degrees(np.arccos(np.clip((np.trace(rotation @ self.last_pose[0].T) - 1) / 2, -1, 1))) < 2)
        self.streak = self.streak + 1 if consistent else 1
        self.last_pose = (rotation, translation)
        return self.streak >= 3

    def check(self, gray):
        empty = dict(moved=False, reference_uv=np.empty((0, 2)), current_uv=np.empty((0, 2)), reliable=False)
        if self.descriptors is None:
            return empty
        keys, desc = self.orb.detectAndCompute(gray, None)
        if desc is None:
            self.streak = 0
            return empty
        pairs = self.matcher.knnMatch(self.descriptors, desc, k=2)
        good = [a for pair in pairs if len(pair) == 2 for a, b in [pair] if a.distance < 0.72 * b.distance]
        reverse = {m.queryIdx: m.trainIdx for m in self.matcher.match(desc, self.descriptors)}
        good = [m for m in good if reverse.get(m.trainIdx) == m.queryIdx]
        matched = dict(empty, reference_uv=to_full(self.uv[[m.queryIdx for m in good]], self.work_size[::-1], self.image_size), current_uv=to_full(np.array([keys[m.trainIdx].pt for m in good]).reshape(-1, 2), self.work_size[::-1], self.image_size))
        if len(good) < 30:
            self.streak = 0
            return matched
        stable = np.linalg.norm(to_work(matched['reference_uv'] - matched['current_uv'], self.work_size[::-1], self.image_size), axis=1) < 1.5
        stable_uv = to_work(matched['reference_uv'][stable], self.work_size[::-1], self.image_size)
        stable_tiles = set(((int(x >= gray.shape[1] / 2), int(y >= gray.shape[0] / 2)) for x, y in stable_uv))
        if stable.sum() >= 30 and len(stable_tiles) >= 3 and np.all(np.ptp(stable_uv, axis=0) >= np.array(gray.shape[::-1]) * 0.35):
            self.streak = 0
            self.last_pose = None
            return dict(matched, reliable=True, inliers=int(stable.sum()), translation_m=0.0, rotation_deg=0.0, rms_px=0.0, evidence='distributed background stationary within 1.5 analysis pixels')
        indexes = np.array([m.queryIdx for m in good])
        image = np.array([keys[m.trainIdx].pt for m in good], np.float64)
        ok, rvec, tvec, inliers = cv2.solvePnPRansac(self.points[indexes], image, self.K, np.zeros(5), iterationsCount=120, reprojectionError=2.0, confidence=0.999, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok or inliers is None:
            self.streak = 0
            return matched
        ids = inliers.ravel()
        ref = self.uv[indexes[ids]]
        cur = image[ids]
        pixels = cv2.projectPoints(self.points[indexes[ids]], rvec, tvec, self.K, np.zeros(5))[0].reshape(-1, 2)
        error = float(np.sqrt(np.mean(np.sum((pixels - cur) ** 2, axis=1))))
        tiles = set(((int(x >= gray.shape[1] / 2), int(y >= gray.shape[0] / 2)) for x, y in ref))
        spread = np.ptp(ref, axis=0)
        coverage = len(tiles) if spread[0] >= gray.shape[1] * 0.35 and spread[1] >= gray.shape[0] * 0.35 else 0
        moved = self.evaluate_pose(rvec, tvec, len(ids), error, coverage)
        return dict(matched, moved=moved, reliable=len(ids) >= 30 and error <= 1.5 and (coverage >= 3), translation_m=float(np.linalg.norm(tvec)), rotation_deg=float(np.degrees(np.linalg.norm(rvec))), inliers=len(ids), rms_px=error)

class EnvironmentWorker(QThread):
    updated = Signal(object)
    motion_detected = Signal(object)
    manual_result = Signal(object)
    manual_progress = Signal(str)

    def __init__(self, parameter_file):
        super().__init__()
        self.parameter_file = parameter_file
        self.stop_event = threading.Event()
        self.condition = threading.Condition()
        self.commands = queue.Queue()
        self.latest = None
        self.selecting = None
        self.page_active = False
        self.planes = {}
        self.candidate_cache = {}
        self.state_lock = threading.Lock()
        self._snapshot = None
        self.monitor = None
        self.visible_since = {}
        self.lock_selection = False
        self.manual_mode = False
        self.board_mode = False
        self.manual_cancel = threading.Event()

    def submit(self, views, ns):
        with self.condition:
            self.latest = ([im.copy() for im in views], ns)
            self.condition.notify()

    def command(self, name, value=None):
        if name == 'confirm':
            value = dict(candidate_id=value, requested_at=time.perf_counter())
        if name == 'manual':
            self.manual_cancel.clear()
        self.commands.put((name, value))
        with self.condition:
            self.condition.notify()

    def cancel_manual(self):
        self.manual_cancel.set()

    def snapshot(self):
        with self.state_lock:
            return copy.deepcopy(self._snapshot)

    def stop(self):
        self.manual_cancel.set()
        self.stop_event.set()
        with self.condition:
            self.condition.notify()

    def publish_snapshot(self):
        snapshot = None
        if self.planes:
            snapshot = dict(id=uuid.uuid4().hex, name=tr('环境基准：') + (tr('水平＋竖直') if len(self.planes) == 2 else tr('水平') if 'horizontal' in self.planes else tr('竖直')), planes=copy.deepcopy(self.planes), camera_to_display=reference_rotation(self.planes).tolist(), calibration_sha256=hashlib.sha256(self.parameter_file.read_bytes()).hexdigest(), convention='camera optical origin; orthonormal rotation only; user-declared level/vertical', movement_thresholds=dict(translation_m=0.01, rotation_deg=1, inliers=30, consistent_checks=3))
        with self.state_lock:
            self._snapshot = snapshot
        return snapshot

    def run(self):
        try:
            geometry = StereoGeometry(self.parameter_file)
            stereo = StereoPlanes(geometry)
            self.monitor = MotionMonitor(stereo.K, geometry.size, stereo.size)
        except Exception as error:
            self.updated.emit(dict(planes={}, selecting=None, overlays=[], candidates=[], snapshot=None, message=tr('环境分析初始化失败：') + str(error)))
            return
        stable = StableCandidates(geometry.parameters['P1'], geometry.parameters['P2'])
        visibility = {}
        board = BoardReference(geometry.parameters['P1'], geometry.parameters['P2'])
        from board_reference import BoardCandidateGate
        board_gate = BoardCandidateGate(geometry.parameters['P1'], geometry.parameters['P2'])
        last_work = 0
        last_depth = 0
        candidates = []
        message = tr('点击水平或竖直选项，等待稳定候选后选择红色网格。')

        def emit_state():
            overlays = [dict(plane=p, color='#3274df' if self.selecting and self.selecting != k else '#14996b') for k, p in self.planes.items() if k not in visibility or visibility[k].visible]
            overlays.extend((dict(plane=p, color='#dc4b56') for p in candidates))
            self.updated.emit(dict(planes=copy.deepcopy(self.planes), selecting=self.selecting, overlays=copy.deepcopy(overlays), candidates=copy.deepcopy(candidates), snapshot=self.snapshot(), message=message))
        while not self.stop_event.is_set():
            changed = False
            while not self.commands.empty():
                name, value = self.commands.get_nowait()
                if name == 'page':
                    self.page_active = bool(value)
                elif name == 'board_options':
                    if self.lock_selection:
                        continue
                    board.configure(value)
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    changed = True
                elif name == 'board':
                    if self.lock_selection:
                        continue
                    self.board_mode = bool(value)
                    self.selecting = None
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    changed = True
                    message = tr('棋盘格辅助：把棋盘格贴在目标表面，再选择水平或竖直；留空自动识别。') if self.board_mode else tr('自然平面：选择类别后等待候选，或手动圈出大致位置。')
                elif name == 'mode':
                    if self.lock_selection:
                        continue
                    self.manual_mode = bool(value)
                    self.selecting = None
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    changed = True
                    message = tr('手动模式：先选择水平/竖直类别，再在同一幅影像右键逐点圈选，点回首点闭合。') if self.manual_mode else tr('自动模式：选择类别后等待稳定候选。')
                elif name == 'select':
                    if self.lock_selection and value is not None:
                        continue
                    self.selecting = None if self.selecting == value else value
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    changed = True
                    message = tr('保持场景静止，等待多帧稳定候选；鼠标移到轮廓上预览，左键确认。') if self.selecting else tr('已取消选择，原基准保留。')
                    if self.manual_mode and self.selecting:
                        message = tr('右键圈选基准面的大致位置，至少3点后点回首点闭合；Backspace撤销一点，Esc重画。')
                elif name == 'clear':
                    if self.lock_selection:
                        continue
                    self.planes = {}
                    self.selecting = None
                    candidates = []
                    self.candidate_cache = {}
                    visibility.clear()
                    stable.reset()
                    board_gate.reset()
                    self.monitor.reset()
                    self.publish_snapshot()
                    changed = True
                    message = tr('已清空，使用相机参考。')
                elif name == 'confirm':
                    if self.lock_selection:
                        continue
                    candidate_id = value['candidate_id'] if isinstance(value, dict) else value
                    requested_at = value['requested_at'] if isinstance(value, dict) else time.perf_counter()
                    candidate = self.candidate_cache.get(candidate_id)
                    if candidate is None or not self.selecting or candidate['kind'] != self.selecting or (requested_at - candidate['stamp'] > 2):
                        message = tr('候选已过期，请重新点击当前网格。')
                        changed = True
                        continue
                    proposed = copy.deepcopy(self.planes)
                    proposed[self.selecting] = copy.deepcopy(candidate['plane'])
                    try:
                        reference_rotation(proposed)
                    except ValueError as error:
                        message = str(error)
                        changed = True
                        continue
                    first = not self.planes
                    try:
                        next_visibility = PlaneVisibility(candidate['plane'], candidate['views'], stereo.K)
                        next_monitor = self.monitor
                        if first:
                            anchor_xyz = candidate['xyz']
                            if anchor_xyz is None:
                                stereo.enhance = False
                                _, anchor_xyz = stereo.depth(candidate['views'])
                            next_monitor = MotionMonitor(stereo.K, geometry.size, stereo.size)
                            next_monitor.anchor(candidate['gray'], anchor_xyz)
                    except Exception as error:
                        message = tr('环境分析失败（已保存方向仍保留）：') + str(error)
                        changed = True
                        continue
                    self.planes = proposed
                    self.monitor = next_monitor
                    visibility[self.selecting] = next_visibility
                    self.publish_snapshot()
                    self.selecting = None
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    changed = True
                    message = tr('基准已保存，遮挡不会清除。相机移动后请重新设置。')
                elif name == 'manual':
                    if self.lock_selection or not self.manual_mode or (not self.selecting):
                        self.manual_result.emit(dict(ok=False, message=tr('当前不能圈选，请重新选择水平或竖直类别。')))
                        continue
                    try:
                        views = value['views']
                        self.manual_progress.emit(tr('正在检查圈选位置…'))
                        if self.board_mode:
                            plane = select_hint([copy.deepcopy(v['plane']) for v in self.candidate_cache.values()], value['points'], value['side'], stereo.size[::-1], geometry.size)
                            stereo.enhance = False
                            gray_pair, xyz = stereo.depth(views)
                        else:
                            if len(stereo.primary_history) < 4:
                                stereo.enhance = True
                                gray_pair, xyz = stereo.depth(views)
                            else:
                                gray_pair = [sample_image(cv2.cvtColor(v, cv2.COLOR_BGR2GRAY), stereo.size) for v in views]
                                xyz = stereo.primary_history[-1]
                            plane = stereo.manual_candidate(value['points'], value['side'], self.selecting, self.manual_cancel, self.manual_progress.emit)
                        if self.manual_cancel.is_set():
                            raise InterruptedError(tr('已取消，原基准保留。'))
                        proposed = copy.deepcopy(self.planes)
                        proposed[self.selecting] = plane
                        reference_rotation(proposed)
                        first = not self.planes
                        next_visibility = PlaneVisibility(plane, views, stereo.K)
                        next_monitor = self.monitor
                        if first:
                            next_monitor = MotionMonitor(stereo.K, geometry.size, stereo.size)
                            next_monitor.anchor(gray_pair[0], xyz)
                        self.planes = proposed
                        self.monitor = next_monitor
                        visibility[self.selecting] = next_visibility
                        self.publish_snapshot()
                        self.selecting = None
                        candidates = []
                        stable.reset()
                        board_gate.reset()
                        message = tr('手动基准已确认并保存。')
                        self.manual_result.emit(dict(ok=True, message=message))
                    except Exception as error:
                        message = str(error)
                        self.manual_result.emit(dict(ok=False, message=message, cancelled=isinstance(error, InterruptedError)))
                    changed = True
            with self.condition:
                current = self.latest
                self.latest = None
                if current is None and (not changed):
                    self.condition.wait(0.12)
                    continue
            now = time.perf_counter()
            if current is not None and (self.page_active or self.planes) and (now - last_work >= 0.35):
                last_work = now
                views, ns = current
                try:
                    views = checked_views(views, geometry.size)
                    gray = sample_image(cv2.cvtColor(views[0], cv2.COLOR_BGR2GRAY), stereo.size)
                    motion = self.monitor.check(gray) if self.planes else {}
                    if motion.get('moved'):
                        details = {k: v for k, v in motion.items() if k not in ['reference_uv', 'current_uv']}
                        details.update(observed_frame_ns=ns, confirmed_ns=time.perf_counter_ns(), reason='camera_motion_confirmed')
                        self.planes = {}
                        self.selecting = None
                        candidates = []
                        self.candidate_cache = {}
                        visibility.clear()
                        stable.reset()
                        board_gate.reset()
                        self.monitor.reset()
                        self.publish_snapshot()
                        self.motion_detected.emit(details)
                        message = tr('相机移动已确认，环境基准已清除。')
                        changed = True
                    xyz = None
                    if self.selecting:
                        if self.board_mode:
                            gray_pair = [sample_image(cv2.cvtColor(v, cv2.COLOR_BGR2GRAY), stereo.size) for v in views]
                            found = board.candidates(views, self.selecting)
                        else:
                            stereo.enhance = True
                            gray_pair, xyz = stereo.depth(views)
                            last_depth = now
                            found = stereo.observe_candidates(xyz, self.selecting)
                        ready = board_gate.update(found, views, time.perf_counter()) if self.board_mode else stable.update(found, time.perf_counter())
                        self.candidate_cache = {p['id']: dict(plane=p, gray=gray_pair[0], xyz=xyz, views=views, stamp=time.perf_counter(), kind=self.selecting) for p in ready}
                        candidates = ready if not self.manual_mode else []
                        if self.board_mode:
                            message = tr('棋盘格方向已稳定：左键选择网格；手动模式请圈住棋盘格位置。') if ready else (tr('正在复核棋盘格，短暂漏检不会立即清除稳定候选。') if board_gate.held else board.message)
                            if ready:
                                shape = ready[0]['quality'].get('pattern_inner', [])
                                if len(shape) == 2:
                                    message += tr(' 方格{0}×{1}；内角点{2}×{3}。').format(shape[0]+1, shape[1]+1, *shape)
                            if board_gate.held and ready:
                                message += tr(' 暂时保留：图像跟踪确认位置未变，最长4秒。')
                            message += tr(' 本次检测{0:.2f}秒。').format(board.last_elapsed)
                        elif self.manual_mode:
                            message = tr('右键圈出大致位置，点回首点闭合；已在后台分析，确认后可取消。')
                        else:
                            message = tr('移动鼠标预览，左键确认。') if ready else tr('正在寻找稳定平面；弱纹理时可开启棋盘格辅助。')
                        if not self.board_mode and not found:
                            advice = image_quality_hint(observations(views))
                            if advice: message += ' ' + advice
                    elif self.page_active and self.planes and (now - last_depth > 1.2):
                        stereo.enhance = False
                        gray_pair, xyz = stereo.depth(views)
                        last_depth = now
                    invisible = []
                    for kind, tracker in visibility.items():
                        if self.page_active:
                            tracker.update(views, xyz, now)
                        if not tracker.visible and tracker.hidden_since is not None and (now - tracker.hidden_since > 3):
                            invisible.append(tr('水平') if kind == 'horizontal' else tr('竖直'))
                    if invisible and (not self.selecting):
                        message = '、'.join(invisible) + tr('基准面暂不可见；方向关系仍保留。')
                    elif self.planes and (not self.selecting):
                        message = tr('基准已保存；遮挡不影响方向关系。') + (tr(' 背景匹配不足，当前不能可靠判断相机移动。') if not motion.get('reliable') else '')
                    emit_state()
                except Exception as error:
                    candidates = []
                    self.candidate_cache = {}
                    stable.reset()
                    board_gate.reset()
                    stereo.primary_history.clear()
                    stereo.depth_history.clear()
                    stereo.selection_history.clear()
                    stereo.cached_candidates.clear()
                    stereo.alternative_xyz = None
                    stereo.previous_gray = None
                    message = tr('环境分析失败（已保存方向仍保留）：') + str(error)
                    emit_state()
            elif changed:
                emit_state()
