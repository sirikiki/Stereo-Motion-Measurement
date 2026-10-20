from i18n import tr, set_language
'Spatially connected planar patches and temporally locked candidates.\n\nDesign references (independent NumPy/OpenCV implementation, no copied source):\nPCL OrganizedMultiPlaneSegmentation / plane coefficient comparators;\nFeng et al., ICRA 2014, organized local patches and region merging (ai4ce/peac).\nPlane labels are user declarations, not semantic or gravity measurements.\n'
import copy, uuid
import cv2
import numpy as np

def robust_fit(points, min_points=30):
    p = np.asarray(points, float)
    p = p[np.isfinite(p).all(axis=1)]
    if len(p) < min_points:
        return None
    weights = 1 / np.maximum(p[:, 2], 0.25) ** 4
    weights /= weights.max()
    base = weights.copy()
    for _ in range(4):
        center = np.average(p, axis=0, weights=weights)
        q = p - center
        eig, v = np.linalg.eigh((q * weights[:, None]).T @ q / weights.sum())
        normal = v[:, 0]
        residual = np.abs(q @ normal)
        scale = max(0.0005, 1.4826 * np.median(residual))
        weights = base * np.minimum(1, 1.5 * scale / np.maximum(residual, 1e-09))
    if eig[1] < 1e-07:
        return None
    limit = min(0.006, max(0.0025, 3 * scale))
    keep = residual < limit
    if keep.sum() < min_points or keep.mean() < 0.75:
        return None
    return dict(normal=normal, center=center, rms=float(np.sqrt(np.mean(residual[keep] ** 2))), keep=keep, curvature=float(eig[0] / max(eig[1], 1e-12)))

def project(points, P):
    h = np.c_[np.asarray(points), np.ones(len(points))] @ P.T
    return h[:, :2] / h[:, 2:]

def patch_geometry(fit, mask, points, P1, P2, step=3):
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 200:
        return None
    contour = cv2.approxPolyDP(contour, 0.012 * cv2.arcLength(contour, True), True).reshape(-1, 2).astype(float) * step * 2
    n = fit['normal']
    c = fit['center']
    offset = -float(n @ c)
    K = P1[:, :3]
    rays = np.c_[contour, np.ones(len(contour))] @ np.linalg.inv(K).T
    denominator = rays @ n
    if np.any(abs(denominator) < 1e-05):
        return None
    vertices = rays * (-offset / denominator)[:, None]
    if np.any(vertices[:, 2] < 0.15):
        return None
    right = np.array([1.0, 0, 0])
    right -= n * (right @ n)
    if np.linalg.norm(right) < 0.1:
        right = np.array([0.0, 1, 0])
        right -= n * (right @ n)
    right /= np.linalg.norm(right)
    other = np.cross(n, right)
    basis = np.vstack([right, other])
    local = (vertices - c) @ basis.T
    lo, hi = (local.min(axis=0), local.max(axis=0))
    q = (points - c) @ basis.T
    _, singular, _ = np.linalg.svd(q - q.mean(axis=0), full_matrices=False)
    if singular[1] / max(singular[0], 1e-09) < 0.15:
        return None
    area = float(abs(cv2.contourArea(local.astype(np.float32))))
    if min(hi - lo) < 0.07 or max(hi - lo) < 0.1 or area < 0.008:
        return None
    lines = []

    def clipped(a, b):
        samples = np.linspace(a, b, 70)
        inside = np.array([cv2.pointPolygonTest(local.astype(np.float32), tuple(x), False) >= 0 for x in samples])
        starts = np.flatnonzero(inside & ~np.r_[False, inside[:-1]])
        ends = np.flatnonzero(inside & ~np.r_[inside[1:], False])
        for start, end in zip(starts, ends):
            if end > start:
                lines.append([c + samples[start] @ basis, c + samples[end] @ basis])
    for t in np.linspace(0.03, 0.97, 6):
        x = lo[0] + t * (hi[0] - lo[0])
        y = lo[1] + t * (hi[1] - lo[1])
        clipped(np.array([x, lo[1]]), np.array([x, hi[1]]))
        clipped(np.array([lo[0], y]), np.array([hi[0], y]))
    if len(lines) < 4:
        return None
    lines = np.array(lines)
    return dict(id=uuid.uuid4().hex, normal=n.tolist(), center_m=c.tolist(), offset_m=offset, vertices_3d_m=vertices.tolist(), grid_3d_m=lines.tolist(), polygons=[project(vertices, P).tolist() for P in [P1, P2]], grids=[project(lines.reshape(-1, 3), P).reshape(-1, 2, 2).tolist() for P in [P1, P2]], quality=dict(inliers=len(points), rms_mm=fit['rms'] * 1000, area_m2=area, connected_support=True), score=float(area / (0.001 + fit['rms'])))

def _connected_candidates(xyz, kind, P1, P2, up=None, tile=10):
    step = 3
    cloud = xyz[::step, ::step]
    height, width = cloud.shape[:2]
    up = np.array([0.0, -1, 0]) if up is None else np.asarray(up, float)
    nodes = {}
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            q = cloud[y:y + tile, x:x + tile].reshape(-1, 3)
            q = q[np.isfinite(q).all(axis=1)]
            if len(q) < 35:
                continue
            fit = robust_fit(q)
            if fit is None or fit['rms'] > 0.003 or fit['curvature'] > 0.045:
                continue
            nodes[y // tile, x // tile] = fit
    consumed = set()
    regions = []
    for seed, first in nodes.items():
        if seed in consumed:
            continue
        todo = [seed]
        group = []
        consumed.add(seed)
        while todo:
            key = todo.pop()
            group.append(key)
            current = nodes[key]
            for delta in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                neighbor = (key[0] + delta[0], key[1] + delta[1])
                if neighbor in consumed or neighbor not in nodes:
                    continue
                candidate = nodes[neighbor]
                if abs(candidate['normal'] @ first['normal']) < np.cos(np.radians(10)):
                    continue
                if abs((candidate['center'] - first['center']) @ first['normal']) > 0.006:
                    continue
                if abs((candidate['center'] - current['center']) @ current['normal']) > 0.004:
                    continue
                consumed.add(neighbor)
                todo.append(neighbor)
        if len(group) >= 3:
            regions.append(group)
    output = []
    for group in regions:
        mask = np.zeros((height, width), bool)
        for y, x in group:
            mask[y * tile:(y + 1) * tile, x * tile:(x + 1) * tile] = True
        mask &= np.isfinite(cloud).all(axis=2)
        points = cloud[mask]
        if len(points) < 200:
            continue
        fit = robust_fit(points, 200)
        if fit is None or fit['rms'] > 0.0035:
            continue
        normal = fit['normal']
        alignment = abs(normal @ up)
        if kind == 'horizontal' and alignment < np.cos(np.radians(35)) or (kind == 'vertical' and alignment > np.sin(np.radians(35))):
            continue
        angles = [np.degrees(np.arccos(np.clip(abs(nodes[key]['normal'] @ normal), 0, 1))) for key in group]
        if np.percentile(angles, 90) > 12:
            continue
        residual = np.abs((cloud - fit['center']) @ normal)
        mask &= residual < 0.0045
        patch = patch_geometry(fit, mask, cloud[mask], P1, P2, step)
        if patch:
            patch['quality']['local_normal_p90_deg'] = float(np.percentile(angles, 90))
            output.append(patch)
    output.sort(key=lambda p: p['score'], reverse=True)
    selected = []
    for patch in output:
        if any((polygon_overlap(patch, p) > 0.35 and abs(np.array(patch['normal']) @ p['normal']) > np.cos(np.radians(5)) for p in selected)):
            continue
        selected.append(patch)
    return selected[:4]

def connected_candidates(xyz, kind, P1, P2, up=None):
    patches = _connected_candidates(xyz, kind, P1, P2, up, 10) + _connected_candidates(xyz, kind, P1, P2, up, 20)
    patches.sort(key=lambda p: p['score'], reverse=True)
    result = []
    for p in patches:
        if any((polygon_overlap(p, q) > 0.25 and abs(np.array(p['normal']) @ q['normal']) > np.cos(np.radians(5)) for q in result)):
            continue
        result.append(p)
    return result[:4]

def polygon_overlap(a, b):
    pa = cv2.convexHull(np.array(a['polygons'][0], np.float32))
    pb = cv2.convexHull(np.array(b['polygons'][0], np.float32))
    inter, _ = cv2.intersectConvexConvex(pa, pb)
    area_a = cv2.contourArea(pa)
    area_b = cv2.contourArea(pb)
    return float(inter / max(area_a + area_b - inter, 1.0))

class StableCandidates:
    """Keep fixed IDs and geometry after four consistent observations. Never smooth measured trajectories."""

    def __init__(self, P1, P2):
        self.P1 = P1
        self.P2 = P2
        self.tracks = []

    def reset(self):
        self.tracks = []

    def update(self, candidates, now):
        used = set()
        for candidate in candidates:
            match = None
            best = 0.5
            for i, track in enumerate(self.tracks):
                if i in used:
                    continue
                previous = track['locked'] or track['last']
                n = np.array(previous['normal'])
                nn = np.array(candidate['normal'])
                if abs(n @ nn) < np.cos(np.radians(2)):
                    continue
                if abs((np.array(candidate['center_m']) - previous['center_m']) @ n) > 0.006:
                    continue
                overlap = polygon_overlap(previous, candidate)
                if overlap > best:
                    match = i
                    best = overlap
            if match is None:
                self.tracks.append(dict(last=copy.deepcopy(candidate), history=[copy.deepcopy(candidate)], stamp=now, locked=None))
                used.add(len(self.tracks) - 1)
                continue
            track = self.tracks[match]
            used.add(match)
            track['stamp'] = now
            track['last'] = copy.deepcopy(candidate)
            if track['locked'] is not None:
                continue
            track['history'] = (track['history'] + [copy.deepcopy(candidate)])[-6:]
            history = track['history']
            if len(history) < 4:
                continue
            normals = np.array([p['normal'] for p in history])
            normals *= np.where(normals @ normals[0] < 0, -1, 1)[:, None]
            distances = np.degrees(np.arccos(np.clip(normals @ normals.T, -1, 1)))
            groups = [np.flatnonzero(row <= 0.7) for row in distances]
            ids = max(groups, key=len)
            if len(ids) < 4:
                continue
            history = [history[i] for i in ids]
            normals = normals[ids]
            mean = normals.mean(axis=0)
            mean /= np.linalg.norm(mean)
            spread = np.degrees(np.arccos(np.clip(normals @ mean, -1, 1)))
            if spread.max() > 0.7:
                continue
            patch = copy.deepcopy(min(history, key=lambda p: p['quality']['rms_mm']))
            center = np.array(patch['center_m'])
            patch['normal'] = mean.tolist()
            patch['offset_m'] = -float(mean @ center)
            for source, target in [('grid_3d_m', 'grids'), ('vertices_3d_m', 'polygons')]:
                q = np.array(patch.get(source, []), float)
                if not q.size:
                    continue
                shape = q.shape
                q = q.reshape(-1, 3)
                q -= ((q - center) @ mean)[:, None] * mean
                patch[source] = q.reshape(shape).tolist()
                patch[target] = [project(q, P).reshape((*shape[:-1], 2)).tolist() for P in [self.P1, self.P2]]
            patch['quality'].update(stable_frames=len(history), normal_spread_deg=float(spread.max()), direction_estimator='at least four agreeing fits among six recent frames; outlier frames excluded')
            track['locked'] = patch
        self.tracks = [t for t in self.tracks if now - t['stamp'] < 1.6]
        return [copy.deepcopy(t['locked']) for t in self.tracks if t['locked'] is not None and now - t['stamp'] < 0.9]

class PlaneVisibility:
    """A saved grid is visible by default. Missing ORB features is never evidence of occlusion."""

    def __init__(self, plane, views, K):
        self.plane = plane
        self.K = K
        self.visible = True
        self.hidden_since = None
        self.reference = cv2.resize(cv2.cvtColor(views[0], cv2.COLOR_BGR2GRAY), (640, 480))
        self.mask = np.zeros((480, 640), np.uint8)
        cv2.fillPoly(self.mask, [np.rint(np.array(plane['polygons'][0]) / 2).astype(np.int32)], 1)
        self.mask = self.mask.astype(bool)
        yy, xx = np.indices(self.mask.shape)
        rays = np.stack([(xx - K[0, 2]) / K[0, 0], (yy - K[1, 2]) / K[1, 1], np.ones_like(xx)], axis=2)
        n = np.array(plane['normal'])
        denom = rays @ n
        self.expected = -plane['offset_m'] / np.where(abs(denom) > 1e-06, denom, np.nan)

    def update(self, views, xyz, now):
        gray = cv2.resize(cv2.cvtColor(views[0], cv2.COLOR_BGR2GRAY), (640, 480))
        obscured = False
        seen = False
        if np.std(gray) < 1 and np.std(self.reference) > 8:
            obscured = True
        elif xyz is not None and self.mask.any():
            current = xyz[:, :, 2]
            valid = self.mask & np.isfinite(current) & np.isfinite(self.expected)
            if valid.sum() > self.mask.sum() * 0.35:
                delta = current[valid] - self.expected[valid]
                obscured = np.mean(delta < -0.035) > 0.6
                seen = np.mean(abs(delta) < 0.025) > 0.55
        if not obscured and self.mask.any():
            a = self.reference[self.mask].astype(float)
            b = gray[self.mask].astype(float)
            seen |= np.percentile(abs(a - np.median(a) - (b - np.median(b))), 80) < 10 and abs(np.std(a) - np.std(b)) < 10
        if obscured:
            self.visible = False
            self.hidden_since = now if self.hidden_since is None else self.hidden_since
        elif seen:
            self.visible = True
            self.hidden_since = None
        return self.visible
