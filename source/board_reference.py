from i18n import tr, set_language
'Known checkerboard as an optional direction reference, not camera recalibration.'
import cv2
import numpy as np
from scipy.optimize import least_squares
from plane_selection import make_geometry, eligible
PATTERN = (9, 6)
SQUARE_M = 0.025

def corners(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    ok, c = cv2.findChessboardCorners(small, PATTERN, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK)
    if not ok:
        return None
    c = np.asarray(c, np.float32).reshape(-1, 1, 2) * 2
    cv2.cornerSubPix(gray, c, (7, 7), (-1, -1), (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001))
    return c.reshape(-1, 2)

def from_corners(left, right, P1, P2, kind):
    l = np.asarray(left, float).reshape(-1, 2)
    r = np.asarray(right, float).reshape(-1, 2)
    if len(l) != 54 or len(r) != 54:
        raise ValueError(tr('需要完整的9×6内角点棋盘格。'))
    if not np.isfinite(l).all() or not np.isfinite(r).all():
        raise ValueError(tr('棋盘格角点无效。'))
    if np.dot(l[-1] - l[0], r[-1] - r[0]) < 0:
        r = r[::-1].copy()
    if np.percentile(abs(l[:, 1] - r[:, 1]), 95) > 1.5:
        raise ValueError(tr('棋盘格左右对应误差过大，请检查清晰度及标定适用性。'))
    q = cv2.triangulatePoints(P1, P2, l.T, r.T)
    xyz = (q[:3] / q[3]).T
    if not np.isfinite(xyz).all() or np.any(xyz[:, 2] < 0.15) or np.any(xyz[:, 2] > 3.0):
        raise ValueError(tr('棋盘格深度无效或超出辅助范围。'))
    obj = np.zeros((54, 3))
    obj[:, :2] = np.mgrid[0:9, 0:6].T.reshape(-1, 2) * SQUARE_M
    ids = np.arange(54).reshape(6, 9)[1:5, 1:8].ravel()
    xyz = xyz[ids]
    obj = obj[ids]
    l = l[ids]
    r = r[ids]
    center = xyz.mean(axis=0)
    a = obj - obj.mean(axis=0)
    b = xyz - center
    u, _, vt = np.linalg.svd(a.T @ b)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T

    def residual(params):
        rot = cv2.Rodrigues(params[:3])[0]
        points = a @ rot.T + params[3:]
        values = []
        for P, uv in [(P1, l), (P2, r)]:
            proj = np.c_[points, np.ones(len(points))] @ P.T
            values.extend((proj[:, :2] / proj[:, 2, None] - uv).ravel())
        return np.asarray(values)
    initial = np.r_[cv2.Rodrigues(rotation)[0].ravel(), center]
    solution = least_squares(residual, initial, max_nfev=40)
    if not solution.success:
        raise ValueError(tr('棋盘格方向拟合未收敛，请保持静止后重试。'))
    rotation = cv2.Rodrigues(solution.x[:3])[0]
    center = solution.x[3:]
    fitted = a @ rotation.T + center
    n = rotation[:, 2]
    if not eligible(n, kind):
        raise ValueError(tr('棋盘格方向与所选类别不符。请把纸贴平在实际水平或竖直表面上。'))
    shape = xyz.reshape(4, 7, 3)
    edges = np.r_[np.linalg.norm(np.diff(shape, axis=0), axis=2).ravel(), np.linalg.norm(np.diff(shape, axis=1), axis=2).ravel()]
    if abs(np.median(edges) / SQUARE_M - 1) > 0.05:
        raise ValueError(tr('棋盘格尺寸与25毫米规格不符，或标定参数不适用。'))
    plane_rms = float(np.sqrt(np.mean(((xyz - center) @ n) ** 2)))
    if plane_rms > 0.002:
        raise ValueError(tr('棋盘格不够平整或深度不可靠，请贴平并检查清晰度。'))
    errors = []
    for P, uv in [(P1, l), (P2, r)]:
        proj = np.c_[fitted, np.ones(28)] @ P.T
        proj = proj[:, :2] / proj[:, 2, None]
        errors.extend(np.linalg.norm(proj - uv, axis=1))
    reprojection = float(np.sqrt(np.mean(np.square(errors))))
    if reprojection > 1.2:
        raise ValueError(tr('棋盘格几何验证未通过，请检查纸张平整度、打印尺寸与清晰度。'))
    spread = []
    for ids in [np.arange(14), np.arange(14, 28), np.arange(28).reshape(4, 7)[:, :3].ravel(), np.arange(28).reshape(4, 7)[:, 4:].ravel()]:
        part = xyz[ids]
        _, _, v = np.linalg.svd(part - part.mean(axis=0), full_matrices=False)
        spread.append(float(np.degrees(np.arccos(np.clip(abs(v[-1] @ n), 0, 1)))))
    if max(spread) > 1.5:
        raise ValueError(tr('棋盘格各区域方向不一致，请贴平纸张或调整观察距离。'))
    polygon = l[[0, 6, 27, 21]]
    mask = np.zeros((480, 640), np.uint8)
    cv2.fillPoly(mask, [np.rint(polygon / 2).astype(np.int32)], 1)
    fit = dict(normal=n, offset=-float(n @ center), center=center, rms=plane_rms, keep=np.ones(28, bool), spread=max(spread), spatial_spread=max(spread))
    plane = make_geometry(fit, mask.astype(bool), P1, P2, polygon=polygon)
    if plane is None:
        raise ValueError(tr('棋盘格观察角度过于倾斜或可见范围过小。'))
    plane['quality'].update(method='stereo_checkerboard_metric_validation', source='checkerboard_9x6_25mm', reprojection_rms_px=reprojection, square_mm=25.0, corner_count=28, corners_detected=54, fit_region='central_7x4')
    return plane

class BoardReference:

    def __init__(self, P1, P2):
        self.P1 = P1
        self.P2 = P2
        self.message = tr('请将25毫米棋盘格贴平，确保左右影像完整可见。')

    def candidates(self, views, kind):
        pair = [corners(v) for v in views]
        if any((c is None for c in pair)):
            self.message = tr('未看到完整棋盘格：需要10×7方格、25毫米边长；请贴平并让两侧完整可见。')
            return []
        try:
            plane = from_corners(*pair, self.P1, self.P2, kind)
            self.message = tr('棋盘格方向验证通过，正在等待多帧稳定。')
            return [plane]
        except ValueError as error:
            self.message = str(error)
            return []
