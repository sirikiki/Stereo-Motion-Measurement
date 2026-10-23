from i18n import tr, set_language
'Right-handed display rotation only. Raw camera coordinates and distances are unchanged.'
import numpy as np
CAMERA_TO_SCENE = np.array([[-1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, -1.0, 0.0]])
SUBJECT_FROM_OBSERVER = np.diag([-1.0, -1.0, 1.0])

def first_origin(samples):
    return next((np.array(s['xyz'], dtype=float).copy() for s in samples if s['valid'] and np.isfinite(s['xyz']).all()), None)

def to_scene(points, rotation=None):
    return np.asarray(points, dtype=float) @ (CAMERA_TO_SCENE if rotation is None else rotation).T
