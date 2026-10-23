"""Packed UVC capture profiles and validated pinhole stereo calibration.

No camera is opened by importing this module. All lengths are metres.
"""
import json
import hashlib
from i18n import tr
from pathlib import Path
import cv2
import numpy as np

PROFILE = {'camera_index': 0, 'camera_backend': 'AUTO', 'camera_width': 1280, 'camera_height': 480, 'camera_fps': 30, 'camera_format': 'DEFAULT', 'camera_layout': 'sbs', 'camera_swap': False}


def profile(config):
    p = {k: config.get(k, v) for k, v in PROFILE.items()}
    if p['camera_backend'] not in ('DSHOW', 'MSMF', 'AUTO') or p['camera_layout'] not in ('sbs', 'tb'):
        raise ValueError('Unsupported camera backend or layout.')
    for k, low, high in [('camera_index',0,20),('camera_width',320,8192),('camera_height',240,8192),('camera_fps',1,240)]:
        if isinstance(p[k],bool) or not isinstance(p[k], (int,float)) or not low <= p[k] <= high or int(p[k]) != p[k]:
            raise ValueError('Invalid camera setting: ' + k)
        p[k] = int(p[k])
    if p['camera_format'] not in ('MJPG','YUY2','DEFAULT') or not isinstance(p['camera_swap'],bool):
        raise ValueError('Invalid camera format or eye order.')
    if p['camera_width'] % 2 or p['camera_height'] % 2:
        raise ValueError('Use even frame dimensions.')
    return p

def as_bgr(frame):
    if not isinstance(frame,np.ndarray) or frame.dtype!=np.uint8:
        raise ValueError('Camera must return decoded 8-bit images; raw Bayer/YUV or 16-bit input is unsupported.')
    if frame.ndim==2:return cv2.cvtColor(frame,cv2.COLOR_GRAY2BGR)
    if frame.ndim==3 and frame.shape[2]==1:return cv2.cvtColor(frame[:,:,0],cv2.COLOR_GRAY2BGR)
    if frame.ndim==3 and frame.shape[2]==4:return cv2.cvtColor(frame,cv2.COLOR_BGRA2BGR)
    if frame.ndim!=3 or frame.shape[2]!=3:
        raise ValueError('Camera must return decoded gray, BGR or BGRA images.')
    return frame

def split_frame(frame, config):
    p = profile(config)
    frame=as_bgr(frame)
    axis = 1 if p['camera_layout'] == 'sbs' else 0
    if frame.shape[axis] % 2:
        raise ValueError('Packed stereo frame cannot be split equally.')
    views = list(np.split(frame,2,axis=axis))
    return views[::-1] if p['camera_swap'] else views

def open_device(config):
    p = profile(config)
    backend = {'DSHOW':cv2.CAP_DSHOW,'MSMF':cv2.CAP_MSMF,'AUTO':cv2.CAP_ANY}[p['camera_backend']]
    cap = cv2.VideoCapture(p['camera_index'],backend)
    if not cap.isOpened():
        cap.release()
        raise ValueError('Cannot open camera. Check device index, USB connection and other camera apps.')
    if p['camera_format'] != 'DEFAULT':
        cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*p['camera_format']))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,p['camera_width'])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT,p['camera_height'])
    cap.set(cv2.CAP_PROP_FPS,p['camera_fps'])
    return cap

def calibration_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def require_calibration_binding(config, path):
    if not config.get('calibration_profile') or not config.get('calibration_sha256'):
        raise ValueError(tr('此标定尚未与当前相机确认绑定。请在左侧相机配置→结果与导入中核对或创建标定；当前仅预览。'))
    if profile(config['calibration_profile']) != profile(config):
        raise ValueError(tr('相机配置与标定绑定配置不同，请重新核对标定。'))
    if calibration_digest(path) != config['calibration_sha256']:
        raise ValueError(tr('标定文件在启用后发生变化，请重新核对标定。'))

def validate_physical(p):
    # Validate before OpenCV allocates maps or receives malformed imported data.
    if str(p.get('camera_model','pinhole')).lower() not in ('pinhole','opencv') or str(p.get('distortion_model','plumb_bob')).lower() not in ('plumb_bob','rational_polynomial','opencv'):
        raise ValueError('Only the OpenCV pinhole model is supported; fisheye calibration is not supported.')
    if str(p.get('units','')) != 'm':
        raise ValueError('Calibration length unit must be metres (m).')
    size=np.asarray(p.get('image_size',[]),float).reshape(-1)
    if size.size!=2 or not np.isfinite(size).all() or np.any(size<160) or np.any(size>8192) or np.any(size!=np.rint(size)) or np.any(size%2):
        raise ValueError('Calibration requires an even per-eye image_size [width, height].')
    p['image_size']=size.astype(np.int32)
    for side in ('left','right'):
        k=np.asarray(p.get('K_'+side,[]),float)
        d=np.asarray(p.get('D_'+side,[]),float)
        if k.shape!=(3,3) or not np.isfinite(k).all() or min(k[0,0],k[1,1])<=0 or not np.allclose(k[2],[0,0,1]) or abs(k[0,1])+abs(k[1,0])>1e-9:
            raise ValueError('Invalid camera intrinsic matrix.')
        if d.size not in (4,5,8,12,14) or d.ndim>2 or (d.ndim==2 and min(d.shape)!=1) or not np.isfinite(d).all():
            raise ValueError('Unsupported pinhole distortion coefficients.')
        p['K_'+side]=k;p['D_'+side]=d.reshape(-1,1)
    r=np.asarray(p.get('R',[]),float);t=np.asarray(p.get('T',[]),float).reshape(-1)
    if r.shape!=(3,3) or not np.isfinite(r).all() or not np.allclose(r.T@r,np.eye(3),atol=1e-3) or not np.isclose(np.linalg.det(r),1,atol=1e-3):
        raise ValueError('Invalid rotation: R')
    if t.size!=3 or not np.isfinite(t).all() or not .001<np.linalg.norm(t)<2:
        raise ValueError('Invalid baseline or length units (supported: 1 mm to 2 m).')
    p['R']=r;p['T']=t.reshape(3,1)
    return p

def rectification(p):
    validate_physical(p)
    size = tuple(int(x) for x in p['image_size'])
    r = cv2.stereoRectify(p['K_left'],p['D_left'],p['K_right'],p['D_right'],size,p['R'],p['T'],flags=cv2.CALIB_ZERO_DISPARITY,alpha=0)
    p.update(zip(('R1','R2','P1','P2','Q'),r[:5]))
    return p

def validate(p):
    validate_physical(p)
    if str(p.get('camera_model','pinhole')).lower() not in ('pinhole','opencv') or str(p.get('distortion_model','plumb_bob')).lower() not in ('plumb_bob','rational_polynomial','opencv'):
        raise ValueError('Only the OpenCV pinhole model is supported; fisheye calibration is not supported.')
    required = {'K_left':(3,3),'K_right':(3,3),'R':(3,3),'R1':(3,3),'R2':(3,3),'P1':(3,4),'P2':(3,4),'Q':(4,4)}
    if str(p.get('units','')) != 'm':
        raise ValueError('Calibration length unit must be metres (m).')
    size = np.asarray(p.get('image_size',[])).reshape(-1)
    if size.size != 2 or not np.isfinite(size).all() or np.any(size < 160) or np.any(size > 8192) or np.any(size != np.rint(size)) or np.any(size % 2):
        raise ValueError('Calibration requires an even per-eye image_size [width, height].')
    for k, shape in required.items():
        a = np.asarray(p.get(k,[]),float)
        if a.shape != shape or not np.isfinite(a).all():
            raise ValueError('Missing/invalid calibration matrix: '+k)
    for side in ('left','right'):
        d = np.asarray(p.get('D_'+side,[]),float)
        if d.size not in (4,5,8,12,14) or not np.isfinite(d).all():
            raise ValueError('Unsupported pinhole distortion coefficients.')
        k = p['K_'+side]
        if min(k[0,0],k[1,1]) <= 0 or not np.allclose(k[2],[0,0,1]):
            raise ValueError('Invalid camera intrinsic matrix.')
    for k in ('R','R1','R2'):
        if not np.allclose(p[k].T@p[k],np.eye(3),atol=1e-3) or np.linalg.det(p[k]) < .99:
            raise ValueError('Invalid rotation: '+k)
    t = np.asarray(p.get('T',[]),float).reshape(-1)
    if t.size != 3 or not np.isfinite(t).all() or not .001 < np.linalg.norm(t) < 2:
        raise ValueError('Invalid baseline or length units (supported: 1 mm to 2 m).')
    if abs(p['P2'][1,3]) > abs(p['P2'][0,3]) or p['P2'][0,3] >= 0:
        raise ValueError('Requires a horizontal baseline with left/right order. Swap eyes and recalibrate if reversed.')
    # Every downstream depth gate assumes canonical horizontal zero-disparity rectification.
    a,b=np.asarray(p['P1']),np.asarray(p['P2'])
    if not np.allclose(a[:,3],0) or not np.allclose(a[:,:3],b[:,:3]) or not np.allclose(a[2],[0,0,1,0]) or min(a[0,0],a[1,1])<=0 or abs(a[0,1])+abs(a[1,0])>1e-8 or not np.allclose(b[1:,3],0):
        raise ValueError('Inconsistent horizontal rectification matrices. Re-import the original K, D, R, T parameters.')
    if not np.allclose(p['R2']@p['R'],p['R1'],atol=1e-3) or not np.allclose(b[:,:3]@(p['R2']@p['T']).ravel(),b[:,3],rtol=1e-4,atol=1e-5):
        raise ValueError('Rectification does not match the physical stereo calibration.')
    for xyz in (np.array([.02,.03,1.]),np.array([-.1,.07,2.])):
        uv=[m@np.r_[xyz,1.] for m in (a,b)];uv=[v[:2]/v[2] for v in uv]
        q=p['Q']@np.array([*uv[0],uv[0][0]-uv[1][0],1.])
        if abs(q[3])<1e-12 or not np.allclose(q[:3]/q[3],xyz,rtol=1e-4,atol=1e-5):
            raise ValueError('Disparity-to-depth matrix does not match stereo projections.')
    return p

def load_calibration(path, units='m'):
    if units not in ('m','mm'): raise ValueError('Choose m or mm for translation units.')
    path = Path(path)
    if path.suffix.lower() == '.npz':
        with np.load(path,allow_pickle=False) as z:
            p = {k:z[k].copy() for k in z.files}
    else:
        fs = cv2.FileStorage(path.read_text(encoding='utf-8-sig'),cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        try:
            if not fs.isOpened(): raise ValueError('Cannot read calibration file.')
            aliases = dict(K_left=('K_left','M1','K1'),D_left=('D_left','D1'),K_right=('K_right','M2','K2'),D_right=('D_right','D2'),R=('R',),T=('T',))
            p = {}
            for key, names in aliases.items():
                for name in names:
                    node = fs.getNode(name)
                    if not node.empty(): p[key]=node.mat(); break
            node=fs.getNode('image_size')
            if not node.empty():
                p['image_size'] = np.array([node.at(i).real() for i in range(node.size())]) if node.isSeq() else node.mat().ravel()
            else:
                p['image_size']=np.array([fs.getNode('image_width').real(),fs.getNode('image_height').real()])
            missing=[key for key in aliases if key not in p]
            if missing: raise ValueError('Missing calibration parameters: '+', '.join(missing))
            p['T'] = np.asarray(p['T'],float) * (.001 if units == 'mm' else 1)
            p['units']=np.array('m')
            for key in ('camera_model','distortion_model'):
                node=fs.getNode(key)
                if not node.empty():p[key]=np.array(node.string())
        finally: fs.release()
    # Recompute derived rectification from original physical parameters to avoid inconsistent imports.
    p = rectification(p)
    return validate(p)

def object_grid(cols, rows, width_mm, height_mm):
    if not np.isfinite([cols,rows,width_mm,height_mm]).all() or int(cols)!=cols or int(rows)!=rows or not 4 <= cols <= 50 or not 4 <= rows <= 50 or min(width_mm,height_mm) <= 0:
        raise ValueError('Enter square counts and measured cell width/height in mm.')
    obj = np.zeros(((cols-1)*(rows-1),3),np.float32)
    obj[:,:2]=np.mgrid[0:cols-1,0:rows-1].T.reshape(-1,2)*[width_mm/1000,height_mm/1000]
    return obj

def find_pair(views, pattern):
    corners=[]
    for image in views:
        # Raw calibration images can have substantial lens distortion. Do not apply
        # the planar homography gate used for already-rectified environment views.
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
        ok,c=cv2.findChessboardCornersSB(gray,pattern,cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_ACCURACY)
        if not ok:
            ok,c=cv2.findChessboardCorners(gray,pattern,cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
            if ok:cv2.cornerSubPix(gray,c,(4,4),(-1,-1),(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS,40,.001))
        if not ok or c is None or len(c)!=pattern[0]*pattern[1]:
            raise ValueError('Complete board not found in both views. Check square counts, focus, lighting and board edges.')
        c=np.asarray(c,np.float32).reshape(-1,1,2)
        if not np.isfinite(c).all() or (c<2).any() or (c[:,:,0]>gray.shape[1]-3).any() or (c[:,:,1]>gray.shape[0]-3).any():
            raise ValueError('Board corners are too close to the image boundary. Keep the entire board visible.')
        corners.append(c)
    # Align both grid axes, not a single diagonal (which becomes ambiguous near 45°).
    cols,rows=pattern
    l=corners[0].reshape(rows,cols,2);r=corners[1].reshape(rows,cols,2)
    def directions(g):
        a=np.array([np.diff(g,axis=k).mean(axis=(0,1)) for k in (0,1)])
        return a/np.maximum(np.linalg.norm(a,axis=1,keepdims=True),1e-9)
    ld=directions(l);options=[]
    for g in (r,r.transpose(1,0,2)):
        if g.shape!=l.shape:continue
        for q in (g,g[::-1],g[:,::-1],g[::-1,::-1]):
            options.append((float(np.sum(ld*directions(q))),q))
    score,best=max(options,key=lambda a:a[0])
    if score<1.5:raise ValueError('Stereo board orientation is inconsistent. Keep the same complete board visible in both eyes.')
    corners[1]=np.ascontiguousarray(best.reshape(-1,1,2))
    return corners

def length_pairs(n):
    # Bound memory for large grids. Include long spans as well as local edges.
    if n<=128:
        ia,ib=np.triu_indices(n,1)
    else:
        rng=np.random.default_rng(0)
        edges=np.vstack([np.c_[np.arange(n-1),np.arange(1,n)],
                         np.c_[np.zeros(n-1,dtype=int),np.arange(1,n)],
                         rng.integers(0,n,size=(4096,2))])
        edges=np.unique(np.sort(edges[edges[:,0]!=edges[:,1]],axis=1),axis=0)
        ia,ib=edges.T
    return ia,ib


def solve_calibration(pairs, size, obj, progress=lambda s:None):
    if len(pairs)<30: raise ValueError('Collect at least 30 diverse accepted stereo pairs.')
    # Every fifth observation is withheld from parameter fitting.
    hold=[start+(2*(start//5))%5 for start in range(0,len(pairs)-4,5)]
    train=[i for i in range(len(pairs)) if i not in hold]
    objects=[obj for _ in train]
    left=[pairs[i][0] for i in train]; right=[pairs[i][1] for i in train]
    progress('Solving left and right camera intrinsics…')
    a=cv2.calibrateCamera(objects,left,size,None,None)
    b=cv2.calibrateCamera(objects,right,size,None,None)
    normals=np.array([cv2.Rodrigues(r)[0][:,2] for r in a[3]])
    angular_span=float(np.degrees(np.arccos(np.clip((normals@normals.T).min(),-1,1))))
    if angular_span < 15:
        raise ValueError('Insufficient board tilt diversity. Add left/right and up/down tilts (not only translation).')
    progress('Solving stereo geometry and checking held-out images…')
    s=cv2.stereoCalibrate(objects,left,right,a[1],a[2],b[1],b[2],size,flags=cv2.CALIB_FIX_INTRINSIC,criteria=(cv2.TERM_CRITERIA_COUNT+cv2.TERM_CRITERIA_EPS,100,1e-7))
    p=dict(K_left=s[1],D_left=s[2],K_right=s[3],D_right=s[4],R=s[5],T=s[6],image_size=np.array(size),units=np.array('m'))
    validate(rectification(p))
    epi=[]; length_errors=[]; per_view=[]
    ia,ib=length_pairs(len(obj))
    reference_lengths=np.linalg.norm(obj[ia]-obj[ib],axis=1)
    for i in hold:
        uv=[rectify_points(pairs[i][j],p,side,j+1) for j,side in enumerate(('left','right'))]
        epi.extend(abs(uv[0][:,1]-uv[1][:,1]).tolist())
        h=cv2.triangulatePoints(p['P1'],p['P2'],uv[0].T,uv[1].T); xyz=(h[:3]/h[3]).T
        if not np.isfinite(xyz).all() or (xyz[:,2]<=0).any(): raise ValueError('Held-out stereo depth is invalid. Check eye order and corner correspondence.')
        # Use every unordered pair, rather than distances from one arbitrarily
        # ordered corner. The result must be invariant to a 180-degree grid order.
        distances=np.linalg.norm(xyz[ia]-xyz[ib],axis=1)
        errors=abs(distances-reference_lengths)/reference_lengths
        length_errors.extend(errors.tolist())
        per_view.append(dict(accepted_pair=i+1,epipolar_p95_px=float(np.percentile(abs(uv[0][:,1]-uv[1][:,1]),95)),length_error_p95_percent=float(np.percentile(errors,95)*100)))
    report=dict(train_pairs=len(train),heldout_pairs=len(hold),board_normal_span_degrees=angular_span,left_rms_px=float(a[0]),right_rms_px=float(b[0]),stereo_rms_px=float(s[0]),heldout_epipolar_p95_px=float(np.percentile(epi,95)),heldout_length_error_p95_percent=float(np.percentile(length_errors,95)*100),baseline_mm=float(np.linalg.norm(p['T'])*1000),independent_accuracy_verified=False)
    report['passed']=max(a[0],b[0],s[0])<=1.5 and report['heldout_epipolar_p95_px']<=2 and report['heldout_length_error_p95_percent']<=5
    report['passed']=bool(report['passed'] and all(v['epipolar_p95_px']<=2 and v['length_error_p95_percent']<=5 for v in per_view))
    report['heldout_indices']=[i+1 for i in hold]
    report['length_pair_count']=len(ia)
    report['heldout_views']=per_view
    report['length_check']='all corner pairs for <=128 points; bounded deterministic local/long-span sample for larger grids'
    return p, report

def rectify_points(points,p,side,index):
    args=(np.asarray(points,np.float64),p['K_'+side],p['D_'+side])
    options=dict(R=p['R'+str(index)],P=p['P'+str(index)],criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS,50,1e-10))
    if hasattr(cv2,'undistortPointsIter'):
        return cv2.undistortPointsIter(*args,**options).reshape(-1,2)
    return cv2.undistortPoints(*args,**options).reshape(-1,2)
