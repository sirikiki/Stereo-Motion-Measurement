from i18n import tr, set_language
'Spatially connected planar patches and temporally locked candidates.\n\nDesign references (independent NumPy/OpenCV implementation, no copied source):\nPCL OrganizedMultiPlaneSegmentation / plane coefficient comparators;\nFeng et al., ICRA 2014, organized local patches and region merging (ai4ce/peac).\nPlane labels are user declarations, not semantic or gravity measurements.\n'
import copy, uuid
import cv2
import numpy as np

def project(points, P):
    h = np.c_[np.asarray(points), np.ones(len(points))] @ P.T
    return h[:, :2] / h[:, 2:]

def polygon_overlap(a, b):
    pa = cv2.convexHull(np.array(a['polygons'][0], np.float32))
    pb = cv2.convexHull(np.array(b['polygons'][0], np.float32))
    inter, _ = cv2.intersectConvexConvex(pa, pb)
    area_a = cv2.contourArea(pa)
    area_b = cv2.contourArea(pb)
    return float(inter / max(area_a + area_b - inter, 1.0))

class StableCandidates:
    """Keep fixed IDs and geometry after four consistent observations. Never smooth measured trajectories."""

    def __init__(self, P1, P2, retain_seconds=1.6, ready_seconds=0.9):
        self.P1 = P1
        self.P2 = P2
        self.retain_seconds = retain_seconds
        self.ready_seconds = ready_seconds
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
        self.tracks = [t for t in self.tracks if now - t['stamp'] < self.retain_seconds]
        return [copy.deepcopy(t['locked']) for t in self.tracks if t['locked'] is not None and now - t['stamp'] < self.ready_seconds]

from environment_raster import analysis_size, to_work, sample_image

class PlaneVisibility:
    """A saved grid is visible by default. Missing ORB features is never evidence of occlusion."""

    def __init__(self, plane, views, K):
        self.plane = plane
        self.K = K
        self.visible = True
        self.hidden_since = None
        self.size = analysis_size(views[0].shape[1::-1])
        self.reference = sample_image(cv2.cvtColor(views[0], cv2.COLOR_BGR2GRAY), self.size)
        self.mask = np.zeros(self.size[::-1], np.uint8)
        cv2.fillPoly(self.mask, [np.rint(to_work(plane['polygons'][0], self.size[::-1], views[0].shape[1::-1])).astype(np.int32)], 1)
        self.mask = self.mask.astype(bool)
        yy, xx = np.indices(self.mask.shape)
        rays = np.stack([(xx - K[0, 2]) / K[0, 0], (yy - K[1, 2]) / K[1, 1], np.ones_like(xx)], axis=2)
        n = np.array(plane['normal'])
        denom = rays @ n
        self.expected = -plane['offset_m'] / np.where(abs(denom) > 1e-06, denom, np.nan)

    def update(self, views, xyz, now):
        gray = sample_image(cv2.cvtColor(views[0], cv2.COLOR_BGR2GRAY), self.size)
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
