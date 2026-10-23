"""Optional stereo checkerboard direction reference; no assumed print scale."""
import cv2
import logging
import time
from image_quality import observations, enhancement_useful, enhance_pair, hint
import numpy as np
from i18n import tr
from plane_selection import make_geometry, eligible
from environment_raster import analysis_size, to_work, scale_xy


def valid_grid(points, pattern):
    cols, rows = pattern
    if min(pattern) < 3 or len(points) != cols * rows:
        return False
    grid = np.asarray(points, float).reshape(rows, cols, 2)
    if not np.isfinite(grid).all():
        return False
    spacing = np.median(np.r_[np.linalg.norm(np.diff(grid,axis=0),axis=2).ravel(), np.linalg.norm(np.diff(grid,axis=1),axis=2).ravel()])
    if spacing < 5:
        return False
    ideal = np.mgrid[0:cols,0:rows].T.reshape(-1,2).astype(np.float64)
    H, _ = cv2.findHomography(ideal, np.asarray(points,float), 0)
    if H is None:
        return False
    predicted = cv2.perspectiveTransform(ideal.reshape(-1,1,2),H).reshape(-1,2)
    return np.percentile(np.linalg.norm(predicted-points,axis=1),95) <= max(2.5,.10*spacing)


def detect(image, pattern=None):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # SB already produces subpixel corners. Do not refine them with cornerSubPix:
    # this increased depth scatter on the current wall-board capture.
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_ACCURACY
    if pattern is None:
        flags |= cv2.CALIB_CB_LARGER
    ok, c, meta = cv2.findChessboardCornersSBWithMeta(gray, pattern or (3,3), flags)
    if ok:
        shape = (meta.shape[1],meta.shape[0])
        points = np.asarray(c,np.float32).reshape(-1,2)
        if valid_grid(points,shape):
            return points,shape
    if pattern is not None:
        # Classic quadrilateral grouping handles strongly foreshortened boards
        # that SB misses. Only these integer-precision corners need refinement.
        ok,c=cv2.findChessboardCorners(gray,pattern,cv2.CALIB_CB_ADAPTIVE_THRESH|cv2.CALIB_CB_NORMALIZE_IMAGE)
        if ok:
            cv2.cornerSubPix(gray,c,(4,4),(-1,-1),(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_MAX_ITER,30,.005))
            points=c.reshape(-1,2)
            if valid_grid(points,pattern):
                return points,pattern
    return None


def from_corners(left, right, P1, P2, kind, pattern=(9, 6), square_mm=None, image_shape=None, right_pattern=None):
    if image_shape is None: raise ValueError('Actual image_shape is required')
    image_size = image_shape[:2][::-1]
    work_size = analysis_size(image_size)
    pixel_scale = scale_xy(work_size[::-1], image_size)
    cols, rows = pattern
    l = np.asarray(left, float).reshape(rows, cols, 2)
    rc, rr = right_pattern or pattern
    grid = np.asarray(right, float).reshape(rr, rc, 2)
    options = []
    for g in (grid, grid.transpose(1, 0, 2)):
        if g.shape != l.shape:
            continue
        for q in (g, g[::-1], g[:, ::-1], g[::-1, ::-1]):
            if any(np.dot(np.diff(l, axis=axis).mean(axis=(0, 1)), np.diff(q, axis=axis).mean(axis=(0, 1))) <= 0 for axis in (0, 1)):
                continue
            a, b = l.reshape(-1, 2), q.reshape(-1, 2)
            epi = abs(a[:, 1] - b[:, 1])
            disparity = a[:, 0] - b[:, 0]
            if np.median(disparity) > .5:
                options.append((float(np.mean(epi)), b.copy()))
    if not options:
        raise ValueError(tr('两侧棋盘格形状或方向不一致，请让同一块棋盘格完整可见。'))
    l = l.reshape(-1, 2)
    _, r = min(options, key=lambda x: x[0])
    q = cv2.triangulatePoints(P1, P2, l.T, r.T)
    with np.errstate(divide='ignore', invalid='ignore'):
        xyz = (q[:3] / q[3]).T
    valid = np.isfinite(xyz).all(axis=1) & (xyz[:, 2] > .15) & (xyz[:, 2] < 5) & (abs(l[:, 1] - r[:, 1]) * pixel_scale[1] <= 1.5)
    reprojection = np.full(len(l), np.inf)
    normalized_reprojection = np.full(len(l), np.inf)
    if np.isfinite(xyz).any():
        errors = []
        normalized_errors = []
        for P, uv in ((P1,l),(P2,r)):
            h=np.c_[xyz,np.ones(len(xyz))] @ P.T
            with np.errstate(divide='ignore', invalid='ignore'):
                errors.append(np.linalg.norm(h[:,:2]/h[:,2,None]-uv,axis=1))
                normalized_errors.append(np.linalg.norm((h[:,:2]/h[:,2,None]-uv)*pixel_scale,axis=1))
        reprojection = np.maximum(*errors)
        normalized_reprojection = np.maximum(*normalized_errors)
    valid &= normalized_reprojection <= .8
    if valid.sum() < max(9, .85 * len(l)):
        logging.warning('Board stereo geometry: corners=%d valid=%d epipolar_median=%.3f epipolar_p95=%.3f disparity_median=%.3f', len(l),valid.sum(),np.median(abs(l[:,1]-r[:,1])),np.percentile(abs(l[:,1]-r[:,1]),95),np.median(l[:,0]-r[:,0]))
        raise ValueError(tr('左右角点对应或深度不可靠，请检查清晰度、遮挡和相机标定。'))
    points = xyz[valid]
    # Robust all-board fit: tolerate isolated corner noise without assuming rigidity.
    weights = np.ones(len(points))
    for _ in range(6):
        center = np.average(points, axis=0, weights=weights)
        _, s, vt = np.linalg.svd((points - center) * np.sqrt(weights[:, None]), full_matrices=False)
        n = vt[-1]
        residual = abs((points - center) @ n)
        sigma = max(.0003, 1.4826 * np.median(residual))
        weights = np.minimum(1., 1.5 * sigma / np.maximum(residual, 1e-10))
    if s[1] < .008 or s[1] / s[0] < .06:
        raise ValueError(tr('棋盘格有效跨度太小或视角过斜，请靠近或调整角度。'))
    rms = float(np.sqrt(np.mean(residual ** 2)))
    # Test spatial halves, not random subsets: bending must not be hidden by averaging.
    ij = np.mgrid[0:rows, 0:cols].reshape(2, -1).T[valid]
    angles = []
    for axis in (0, 1):
        for half in (ij[:, axis] <= np.median(ij[:, axis]), ij[:, axis] >= np.median(ij[:, axis])):
            p = points[half]
            if len(p) >= 6:
                _, sv, v = np.linalg.svd(p - p.mean(axis=0), full_matrices=False)
                if sv[1] > .005:
                    angles.append(float(np.degrees(np.arccos(np.clip(abs(v[-1] @ n), 0, 1)))))
    spread = max(angles, default=0.)
    if rms > .003 or spread > 3.:
        raise ValueError(tr('已识别棋盘格，但弯曲或深度误差过大，不能可靠代表基准面；请压平纸张。'))
    if not eligible(n, kind):
        raise ValueError(tr('棋盘格方向与所选类别不符。请把纸贴平在实际水平或竖直表面上。'))
    shape = xyz.reshape(rows, cols, 3)
    edges = np.r_[np.linalg.norm(np.diff(shape, axis=0), axis=2).ravel(), np.linalg.norm(np.diff(shape, axis=1), axis=2).ravel()]
    edge = float(np.nanmedian(edges)) * 1000
    supplied = np.asarray(square_mm if isinstance(square_mm, (list, tuple)) else [square_mm or 0, square_mm or 0], float)
    measured = np.array([np.nanmedian(np.linalg.norm(np.diff(shape,axis=1),axis=2)), np.nanmedian(np.linalg.norm(np.diff(shape,axis=0),axis=2))]) * 1000
    if np.any(supplied) and (np.any(supplied <= 0) or np.any(abs(measured / supplied - 1) > .12)):
        raise ValueError(tr('实测格长与输入值相差超过12%，请检查毫米单位和相机标定；未知格长可留空。'))
    polygon = cv2.convexHull(l[valid].astype(np.float32)).reshape(-1, 2)
    mask = np.zeros(analysis_size(image_shape[:2][::-1])[::-1], np.uint8)
    cv2.fillPoly(mask, [np.rint(to_work(polygon, mask.shape, image_shape[:2][::-1])).astype(np.int32)], 1)
    fit = dict(normal=n, offset=-float(n @ center), center=center, rms=rms, keep=valid, spread=spread, spatial_spread=spread)
    plane = make_geometry(fit, mask.astype(bool), P1, P2, polygon=polygon, image_size=image_shape[:2][::-1])
    if plane is None:
        raise ValueError(tr('棋盘格可见区域太小，请让棋盘格在两侧影像中占据更大面积。'))
    plane['quality'].update(method='stereo_checkerboard_robust_all_corners', source='checkerboard', pattern_inner=list(pattern), square_mm_input=square_mm, square_mm_measured=edge, corner_count=int(valid.sum()), corners_detected=len(l), fit_region='all_detected_corners', epipolar_median_px=float(np.median(abs(l[:,1]-r[:,1]))), epipolar_p95_px=float(np.percentile(abs(l[:,1]-r[:,1]),95)), reprojection_max_px=float(np.max(reprojection[valid])), approximate_direction=bool(spread > 1.5 or rms > .0015))
    extend_board_grid(plane, l, valid, pattern, P1, P2)
    plane['quality'].update(analysis_size=list(work_size),
        epipolar_p95_analysis_px=float(np.percentile(abs(l[:,1]-r[:,1])*pixel_scale[1],95)),
        reprojection_max_analysis_px=float(np.max(normalized_reprojection[valid])))
    return plane


class BoardReference:
    def __init__(self, P1, P2):
        self.P1, self.P2 = P1, P2
        self.options = {}
        self.message = ''
        self.last_error = None
        self.cached_pattern = None
        self.search_index = 0
        self.last_elapsed = 0.

    def configure(self, options):
        self.options = dict(options or {})
        self.cached_pattern = None
        self.search_index = 0

    def candidates(self, views, kind):
        started = time.perf_counter()
        stats = observations(views)
        preprocessing = 'original'
        try:
            if self.options.get('invalid'):
                raise ValueError(tr('请输入有效的方格数量与毫米尺寸，或留空。'))
            cols, rows = self.options.get('cols', 0), self.options.get('rows', 0)
            if bool(cols) != bool(rows):
                raise ValueError(tr('请同时填写方格列数和行数，或把两项都清空以自动识别。'))
            if (cols and cols < 4) or (rows and rows < 4):
                raise ValueError(tr('方格列数和行数至少为4；0表示自动。'))
            size = self.options.get('square_mm')
            if isinstance(size, (list, tuple)) and (len(size) != 2 or any(v <= 0 for v in size)):
                raise ValueError(tr('请同时填写单格宽和高，或把两项都清空。'))
            pattern = (cols - 1, rows - 1) if cols and rows else None
            pair = [detect(v, pattern or self.cached_pattern) for v in views]
            def compatible(pair):
                return all(p is not None for p in pair) and sorted(pair[0][1]) == sorted(pair[1][1])
            if pattern is None and not compatible(pair):
                # First retry the shape detected in either camera, then one bounded
                # fallback per update. No long enumeration blocking cancellation.
                proposals = [p[1] for p in pair if p is not None]
                common = [(9,6),(8,6),(7,5),(6,4),(5,4),(4,3),(11,8),(12,8),(10,7)]
                proposals.insert(0, common[self.search_index % len(common)])
                self.search_index += 1
                for candidate in dict.fromkeys(proposals):
                    retry = [detect(v,candidate) for v in views]
                    if compatible(retry):
                        pair=retry
                        break
            if not compatible(pair) and enhancement_useful(stats):
                # One extra specification per update, not one extra pass per search.
                candidate = pattern or self.cached_pattern or next((p[1] for p in pair if p is not None), None)
                if candidate is None:
                    candidate = common[(self.search_index-1) % len(common)]
                enhanced = [detect(v,candidate) for v in enhance_pair(views)]
                if compatible(enhanced):
                    pair = enhanced
                    preprocessing = 'bounded_clahe_fallback'
            if any(p is None for p in pair):
                raise ValueError(tr('未识别完整棋盘格；保持静止、改善光照，或填写方格列数和行数。'))
            if not compatible(pair):
                raise ValueError(tr('两侧棋盘格形状或方向不一致，请让同一块棋盘格完整可见。'))
            plane = from_corners(pair[0][0], pair[1][0], self.P1, self.P2, kind, pair[0][1], self.options.get('square_mm') or None, views[0].shape[:2], pair[1][1])
            plane['quality'].update(image_observations=stats, detection_preprocessing=preprocessing)
            if pattern is None:
                self.cached_pattern = pair[0][1]
            self.message = tr('已识别{0}×{1}内角点，正在验证多帧稳定；轻微弯曲时使用整体近似方向。').format(*pair[0][1])
            self.last_error = None
            return [plane]
        except (ValueError, cv2.error) as error:
            self.message = str(error)
            advice = hint(stats)
            if advice: self.message += ' ' + advice
            if self.message != self.last_error:
                logging.warning('Checkerboard reference (%s, options=%s): %s', kind, self.options, self.message)
                self.last_error = self.message
            return []

        finally:
            self.last_elapsed = time.perf_counter() - started


def extend_board_grid(plane, pixels, valid, pattern, P1, P2):
    """Extrapolated display border only; fitted corners and quality remain measured."""
    cols, rows = pattern
    ideal = np.mgrid[0:cols,0:rows].T.reshape(-1,2).astype(float)
    H,_ = cv2.findHomography(ideal[valid], pixels[valid], 0)
    if H is None:
        return
    boundary=np.array([[-1,-1],[cols,-1],[cols,rows],[-1,rows]],float)
    lines=[]
    for x in range(-1,cols+1): lines.append([[x,-1],[x,rows]])
    for y in range(-1,rows+1): lines.append([[-1,y],[cols,y]])
    uv=cv2.perspectiveTransform(np.concatenate([boundary,np.array(lines).reshape(-1,2)]).reshape(-1,1,2),H).reshape(-1,2)
    rays=np.c_[uv,np.ones(len(uv))] @ np.linalg.inv(P1[:,:3]).T
    n=np.array(plane['normal']);denom=rays@n
    if np.any(abs(denom)<1e-6):return
    xyz=rays*(-plane['offset_m']/denom)[:,None]
    if not np.isfinite(xyz).all() or np.any(xyz[:,2]<=0):return
    from plane_patches import project
    plane.update(vertices_3d_m=xyz[:4].tolist(),grid_3d_m=xyz[4:].reshape(-1,2,3).tolist(),polygons=[project(xyz[:4],P).tolist() for P in (P1,P2)],grids=[project(xyz[4:],P).reshape(-1,2,2).tolist() for P in (P1,P2)],draw_boundary=True)
    plane['quality']['display_border']='one cell extrapolated from inner corners; not additional measured support'


class BoardCandidateGate:
    """Bridge brief detection gaps only with stationary stereo image evidence."""
    def __init__(self,P1,P2):
        from plane_patches import StableCandidates
        self.stable=StableCandidates(P1,P2,retain_seconds=5.,ready_seconds=4.)
        self.reset()

    def reset(self):
        self.stable.reset()
        self.anchor=None
        self.points=None
        self.ready=[]
        self.last_good=None
        self.held=False

    def stationary(self,views):
        if self.anchor is None or self.points is None:return False
        for before,im,p in zip(self.anchor,views,self.points):
            if p is None or len(p)<12:return False
            gray=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY)
            if gray.shape!=before.shape:return False
            scale=scale_xy(analysis_size(gray.shape[::-1])[::-1],gray.shape[::-1])
            window=max(5,int(round(10.5/min(scale)))|1)
            patch=max(3,int(round(5.5/min(scale)))|1)
            q,ok,_=cv2.calcOpticalFlowPyrLK(before,gray,p,None,winSize=(window,window),maxLevel=2)
            if q is None:return False
            back,ok2,_=cv2.calcOpticalFlowPyrLK(gray,before,q,None,winSize=(window,window),maxLevel=2)
            if back is None:return False
            good=ok.ravel().astype(bool)&ok2.ravel().astype(bool)&(np.linalg.norm((back-p)*scale,axis=2).ravel()<.5)
            if good.mean()<.85:return False
            displacement=np.linalg.norm((q[good]-p[good])*scale,axis=2)
            if np.percentile(displacement,90)>.75:return False
            # Exposure/occlusion differences must not masquerade as stationary features.
            a=p[good].reshape(-1,2);b=q[good].reshape(-1,2)
            residual=[]
            for x,y in zip(a,b):
                pa=cv2.getRectSubPix(before,(patch,patch),tuple(x));pb=cv2.getRectSubPix(gray,(patch,patch),tuple(y))
                aa=pa.astype(float);bb=pb.astype(float)
                residual.append(np.mean(abs((aa-aa.mean())-(bb-bb.mean()))))
            if np.median(residual)>12:return False
        return True

    def update(self,found,views,now):
        import copy
        self.held=False
        if found:
            # A moved board starts a new sequence; never re-use the previous lock.
            if self.anchor is not None and not self.stationary(views):self.reset()
            previous_id=self.ready[0]['id'] if self.ready else None
            ready=self.stable.update(found,now)
            self.ready=sorted(ready,key=lambda p:(p['id']!=previous_id,p['quality']['rms_mm']))[:1]
            self.anchor=[cv2.cvtColor(v,cv2.COLOR_BGR2GRAY) for v in views]
            self.points=[]
            for side,gray in enumerate(self.anchor):
                mask=np.zeros_like(gray)
                cv2.fillPoly(mask,[np.rint(found[0]['polygons'][side]).astype(np.int32)],255)
                scale=scale_xy(analysis_size(gray.shape[::-1])[::-1],gray.shape[::-1])
                self.points.append(cv2.goodFeaturesToTrack(gray,80,.01,max(3,3.5/min(scale)),mask=mask))
            self.last_good=now
            return copy.deepcopy(self.ready)
        if self.last_good is not None and now-self.last_good<=4. and self.stationary(views):
            self.held=True
            return copy.deepcopy(self.ready)
        self.reset()
        return []
