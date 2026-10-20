from i18n import tr, set_language
'Plane selection in inverse depth, with explicit ROI validation and bounded overlays.'
import uuid
import cv2
import numpy as np
from plane_patches import project, polygon_overlap

def polygon_mask(points, shape=(480, 640)):
    p = np.asarray(points, float)
    if p.ndim != 2 or p.shape[1] != 2 or len(p) < 3 or (not np.isfinite(p).all()):
        raise ValueError(tr('至少需要三个有效点。'))
    if len(p) > 64:
        raise ValueError(tr('顶点过多，请用较少的点重新圈选。'))

    def cross(a, b, c):
        return float(np.cross(b - a, c - a))
    for i in range(len(p)):
        a, b = (p[i], p[(i + 1) % len(p)])
        if np.linalg.norm(a - b) < 2:
            raise ValueError(tr('存在重复或过近的顶点，请重新圈选。'))
        for j in range(i + 1, len(p)):
            if j == i + 1 or (i == 0 and j == len(p) - 1):
                continue
            c, d = (p[j], p[(j + 1) % len(p)])
            if cross(a, b, c) * cross(a, b, d) <= 0 and cross(c, d, a) * cross(c, d, b) <= 0:
                if max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0])) and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1])):
                    raise ValueError(tr('边线交叉，无法构成有效区域，请重新圈选。'))
    h, w = shape
    if (p < 0).any() or (p[:, 0] >= w * 2).any() or (p[:, 1] >= h * 2).any():
        raise ValueError(tr('圈选点超出影像。'))
    if abs(cv2.contourArea(p.astype(np.float32))) < 1600:
        raise ValueError(tr('圈选区域过小，请扩大到一块平整表面。'))
    mask = np.zeros(shape, np.uint8)
    cv2.fillPoly(mask, [np.rint(p / 2).astype(np.int32)], 1)
    return mask.astype(bool)

def fit_inverse(points, threshold, seed=44, check_spatial=True):
    p = np.asarray(points, float)
    p = p[np.isfinite(p).all(axis=1) & (p[:, 2] > 0.15)]
    if len(p) < 150:
        return None
    rng = np.random.default_rng(seed)
    sample = p if len(p) < 5000 else p[rng.choice(len(p), 5000, replace=False)]
    design = np.c_[sample[:, :2] / sample[:, 2, None], np.ones(len(sample))]
    target = 1 / sample[:, 2]
    best = None
    count = 0
    for _ in range(160):
        ids = rng.choice(len(sample), 3, replace=False)
        a = design[ids]
        if abs(np.linalg.det(a)) < 1e-05:
            continue
        model = np.linalg.solve(a, target[ids])
        mask = abs(design @ model - target) < threshold
        if mask.sum() > count:
            best = model
            count = int(mask.sum())
    if best is None or count < 150:
        return None
    a = np.c_[p[:, :2] / p[:, 2, None], np.ones(len(p))]
    b = 1 / p[:, 2]
    for _ in range(4):
        errors = abs(a @ best - b)
        keep = errors < threshold
        if keep.sum() < 150:
            return None
        weights = np.minimum(1, threshold * 0.45 / np.maximum(errors[keep], 1e-10))
        best = np.linalg.lstsq(a[keep] * np.sqrt(weights[:, None]), b[keep] * np.sqrt(weights), rcond=None)[0]
    keep = abs(a @ best - b) < threshold
    length = np.linalg.norm(best)
    if length < 1e-06:
        return None
    n = best / length
    offset = -1 / length
    center = p[keep].mean(axis=0)
    q = p[keep] - center
    eig = np.linalg.eigvalsh(q.T @ q / len(q))
    if eig[1] < 0.0001 or eig[1] / max(eig[2], 1e-09) < 0.012:
        return None
    normals = []
    for ids in np.array_split(rng.permutation(np.flatnonzero(keep)), 4):
        if len(ids) < 35:
            return None
        v = np.linalg.lstsq(a[ids], b[ids], rcond=None)[0]
        normals.append(v / np.linalg.norm(v))
    spread = float(np.max(np.degrees(np.arccos(np.clip(np.abs(np.array(normals) @ n), 0, 1)))))
    if spread > 1.2:
        return None
    spatial = []
    for axis in (0, 1) if check_spatial else ():
        split = np.median(a[keep, axis])
        for half in (a[:, axis] <= split, a[:, axis] > split):
            ids = keep & half
            if ids.sum() < 80 or np.linalg.cond(a[ids]) > 300:
                continue
            v = np.linalg.lstsq(a[ids], b[ids], rcond=None)[0]
            v /= np.linalg.norm(v)
            spatial.append(float(np.degrees(np.arccos(np.clip(abs(v @ n), 0, 1)))))
    spatial_spread = max(spatial, default=0.0)
    if spatial_spread > 2.0:
        return None
    center = center - n * (center @ n + offset)
    return dict(normal=n, offset=offset, center=center, model=best, keep=keep, rms=float(np.sqrt(np.mean((p[keep] @ n + offset) ** 2))), spread=spread, spatial_spread=spatial_spread)

def eligible(normal, kind, up=None):
    up = np.array([0.0, -1, 0]) if up is None else np.asarray(up)
    dot = abs(np.asarray(normal) @ up)
    return dot >= np.cos(np.radians(40)) if kind == 'horizontal' else dot <= np.sin(np.radians(40))

def make_geometry(fit, mask, P1, P2, polygon=None, side=0, manual=False):
    n = fit['normal']
    offset = fit['offset']
    center = fit['center']
    K = (P2 if side else P1)[:, :3]
    if polygon is None:
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contour = cv2.convexHull(max(contours, key=cv2.contourArea))
        if cv2.contourArea(contour) < 400:
            return None
        polygon = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True).reshape(-1, 2) * 2
    polygon = np.asarray(polygon, float)
    display_mask = np.zeros(mask.shape, np.uint8)
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        cv2.drawContours(display_mask, [cv2.convexHull(max(contours, key=cv2.contourArea))], -1, 1, cv2.FILLED)
    display_mask = cv2.morphologyEx(display_mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    display_mask = cv2.dilate(display_mask, np.ones((7, 7), np.uint8)).astype(bool)
    camera_center = np.array([-P2[0, 3] / P2[0, 0], 0, 0]) if side else np.zeros(3)
    rays = np.c_[polygon, np.ones(len(polygon))] @ np.linalg.inv(K).T
    denom = rays @ n
    if np.any(abs(denom) < 1e-05):
        return None
    vertices = camera_center + rays * ((-offset - camera_center @ n) / denom)[:, None]
    if np.any(vertices[:, 2] < 0.15):
        return None
    if manual:
        mask = np.zeros(mask.shape, np.uint8)
        cv2.fillPoly(mask, [np.rint(project(vertices, P1) / 2).astype(np.int32)], 1)
        mask = mask.astype(bool)
    tangent = np.array([1.0, 0, 0])
    tangent -= n * (tangent @ n)
    if np.linalg.norm(tangent) < 0.1:
        tangent = np.cross(n, [0, 0, 1])
    tangent /= np.linalg.norm(tangent)
    other = np.cross(n, tangent)
    basis = np.vstack([tangent, other])
    local = (vertices - center) @ basis.T
    lo, hi = (local.min(axis=0), local.max(axis=0))
    if min(hi - lo) < 0.06:
        return None
    lines = []
    for axis in [0, 1]:
        for fraction in np.linspace(0.02, 0.98, 8):
            a = lo.copy()
            b = hi.copy()
            a[axis] = b[axis] = lo[axis] + fraction * (hi[axis] - lo[axis])
            points = center + np.linspace(a, b, 160) @ basis
            uv = project(points, P1) / 2
            xy = np.rint(uv).astype(int)
            inside = (xy[:, 0] >= 0) & (xy[:, 0] < mask.shape[1]) & (xy[:, 1] >= 0) & (xy[:, 1] < mask.shape[0])
            inside[inside] &= display_mask[xy[inside, 1], xy[inside, 0]]
            starts = np.flatnonzero(inside & ~np.r_[False, inside[:-1]])
            ends = np.flatnonzero(inside & ~np.r_[inside[1:], False])
            for start, end in zip(starts, ends):
                if end - start >= 2:
                    lines.append([points[start], points[end]])
    if len(lines) < 4:
        return None
    lines = np.array(lines)
    return dict(id=uuid.uuid4().hex, normal=n.tolist(), offset_m=float(offset), center_m=center.tolist(), vertices_3d_m=vertices.tolist(), grid_3d_m=lines.tolist(), polygons=[project(vertices, P).tolist() for P in [P1, P2]], grids=[project(lines.reshape(-1, 3), P).reshape(-1, 2, 2).tolist() for P in [P1, P2]], draw_boundary=manual, quality=dict(rms_mm=fit['rms'] * 1000, inliers=int(fit['keep'].sum()), normal_spread_deg=fit['spread'], spatial_normal_spread_deg=fit.get('spatial_spread', 0.0), method='inverse_depth_spatially_checked_fit', manual=manual), score=float(mask.sum() / (1 + fit['rms'] * 1000)))

def automatic_planes(xyz, kind, P1, P2, search_local=True):
    cloud = xyz[::3, ::3]
    valid = np.isfinite(cloud).all(axis=2)
    points = cloud[valid]
    threshold = 0.45 / (abs(P2[0, 3]) * 0.5)
    result = []
    for attempt in range(6):
        if len(points) < 200:
            break
        fit = fit_inverse(points, threshold, seed=44 + attempt, check_spatial=False)
        if fit is None:
            continue
        points = points[~fit['keep']]
        if not eligible(fit['normal'], kind):
            continue
        all_valid = np.isfinite(xyz).all(axis=2)
        a = xyz[:, :, 0] / xyz[:, :, 2]
        b = xyz[:, :, 1] / xyz[:, :, 2]
        residual = abs(a * fit['model'][0] + b * fit['model'][1] + fit['model'][2] - 1 / xyz[:, :, 2])
        supported = all_valid & (residual < threshold)
        mask = cv2.morphologyEx(supported.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
        bad = (all_valid & (residual > threshold * 3)).astype(np.uint8)
        _, bad_labels, bad_stats, _ = cv2.connectedComponentsWithStats(bad, 8)
        for index in range(1, len(bad_stats)):
            if bad_stats[index, cv2.CC_STAT_AREA] >= 150:
                mask[bad_labels == index] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for label in np.argsort(stats[1:, cv2.CC_STAT_AREA])[::-1][:3] + 1:
            if stats[label, cv2.CC_STAT_AREA] < 1200:
                continue
            component = labels == label
            local_points = xyz[component & supported][::3]
            local_fit = fit_inverse(local_points, threshold, seed=70 + attempt)
            if local_fit is None:
                continue
            if not eligible(local_fit['normal'], kind):
                continue
            local_residual = abs(a * local_fit['model'][0] + b * local_fit['model'][1] + local_fit['model'][2] - 1 / xyz[:, :, 2])
            actual_support = component & all_valid & (local_residual < threshold)
            sy, sx = np.nonzero(actual_support)
            if len(sx) < 750 or np.ptp(sx) < 45 or np.ptp(sy) < 25:
                continue
            p = make_geometry(local_fit, component, P1, P2)
            if p:
                p['quality']['measured_support_pixels'] = int(len(sx))
                p['score'] = float(len(sx) / (1 + local_fit['rms'] * 1000 + local_fit['spatial_spread']))
                result.append(p)
    result.sort(key=lambda p: p['score'], reverse=True)
    unique = []
    for p in result:
        if any((abs(np.array(p['normal']) @ q['normal']) > np.cos(np.radians(3)) and polygon_overlap(p, q) > 0.15 and (abs(np.array(p['center_m']) @ q['normal'] + q['offset_m']) < 0.015) for q in unique)):
            continue
        unique.append(p)
    if not unique and search_local:
        for y0, y1 in [(xyz.shape[0] // 2, xyz.shape[0]), (xyz.shape[0] * 2 // 3, xyz.shape[0])]:
            region = xyz.copy()
            region[:y0] = np.nan
            region[y1:] = np.nan
            unique.extend(automatic_planes(region, kind, P1, P2, False))
            if unique:
                break
    return unique[:4]

def manual_plane(xyz, polygon, side, kind, P1, P2):
    """Use the polygon as a bounded search hint, never as a coplanarity claim."""
    selected = polygon_mask(polygon, xyz.shape[:2])
    nearby = cv2.dilate(selected.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (97, 97))).astype(bool)
    valid = np.isfinite(xyz).all(axis=2)
    search = nearby.copy()
    if side:
        search = np.zeros_like(valid)
        uv = project(xyz[valid], P2) / 2
        xy = np.rint(uv).astype(int)
        inside = (xy[:, 0] >= 0) & (xy[:, 0] < nearby.shape[1]) & (xy[:, 1] >= 0) & (xy[:, 1] < nearby.shape[0])
        chosen = np.zeros(len(xy), bool)
        chosen[inside] = nearby[xy[inside, 1], xy[inside, 0]]
        search[valid] = chosen
    local = xyz.copy()
    local[~search] = np.nan
    candidates = automatic_planes(local, kind, P1, P2)
    candidates += automatic_planes(xyz, kind, P1, P2)
    return select_hint(candidates, polygon, side, xyz.shape[:2])

def select_hint(candidates, polygon, side, shape=(480, 640)):
    selected = polygon_mask(polygon, shape)
    nearby = cv2.dilate(selected.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (97, 97))).astype(bool)
    ranked = []
    for plane in candidates:
        mask = np.zeros(selected.shape, np.uint8)
        cv2.fillPoly(mask, [np.rint(np.asarray(plane['polygons'][side]) / 2).astype(np.int32)], 1)
        overlap = int(np.count_nonzero(mask.astype(bool) & selected))
        near = int(np.count_nonzero(mask.astype(bool) & nearby))
        if overlap < 150 and near < 600:
            continue
        score = overlap * 4 + near * 0.25
        ranked.append((score, plane))
    if not ranked:
        raise ValueError(tr('圈选区域及附近未找到可靠的所选平面。请调整大致位置、扩大范围或改善照明后重试。'))
    plane = max(ranked, key=lambda item: item[0])[1]
    plane['quality']['manual'] = True
    plane['quality']['selection_method'] = 'hint_guided_automatic_search'
    plane['hint_polygon'] = polygon
    plane['hint_side'] = int(side)
    plane['search_margin_pixels'] = 96
    return plane
