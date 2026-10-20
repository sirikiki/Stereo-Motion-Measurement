from i18n import tr, set_language
'Rectification and quality-gated point triangulation in meters.'
from pathlib import Path
import cv2
import numpy as np

class StereoGeometry:

    def __init__(self, parameter_file, min_depth=0.2, max_depth=3.0):
        self.min_depth = min_depth
        self.max_depth = max_depth
        with np.load(Path(parameter_file), allow_pickle=False) as data:
            self.parameters = {key: data[key].copy() for key in data.files}
        p = self.parameters
        if str(p['units']) != 'm':
            raise ValueError('Calibration must use meters')
        self.size = tuple((int(x) for x in p['image_size']))
        self.maps = {}
        for side, rotation, projection in [('left', 'R1', 'P1'), ('right', 'R2', 'P2')]:
            self.maps[side] = cv2.initUndistortRectifyMap(p['K_' + side], p['D_' + side], p[rotation], p[projection], self.size, cv2.CV_32FC1)

    def rectify(self, combined):
        width, height = self.size
        if combined.shape[:2] != (height, width * 2):
            raise ValueError('Frame size differs from calibration')
        left, right = np.split(combined, 2, axis=1)
        return [cv2.remap(image, *self.maps[side], cv2.INTER_LINEAR) for side, image in [('left', left), ('right', right)]]

    def triangulate(self, left, right):
        if left is None or right is None:
            return (None, {'reason': 'marker_not_detected_in_both_views'})
        left, right = (np.asarray(left, np.float64).reshape(2), np.asarray(right, np.float64).reshape(2))
        if not np.all(np.isfinite(np.r_[left, right])):
            return (None, {'reason': 'invalid_image_points'})
        vertical_error = float(abs(left[1] - right[1]))
        quality = {'vertical_error_px': vertical_error, 'disparity_px': float(left[0] - right[0])}
        if vertical_error > 3.0:
            return (None, dict(quality, reason='epipolar_mismatch'))
        p = self.parameters
        h = cv2.triangulatePoints(p['P1'], p['P2'], left.reshape(2, 1), right.reshape(2, 1)).ravel()
        if abs(h[3]) < 1e-12:
            return (None, dict(quality, reason='degenerate_triangulation'))
        xyz = h[:3] / h[3]
        if not np.all(np.isfinite(xyz)) or not self.min_depth <= xyz[2] <= self.max_depth:
            return (None, dict(quality, reason='depth_outside_test_workspace'))
        residuals = []
        for projection, observed in [(p['P1'], left), (p['P2'], right)]:
            projected = projection @ np.r_[xyz, 1.0]
            residuals.append(float(np.linalg.norm(projected[:2] / projected[2] - observed)))
        quality['reprojection_max_px'] = max(residuals)
        if max(residuals) > 2.0:
            return (None, dict(quality, reason='reprojection_mismatch'))
        return (xyz, dict(quality, reason='ok'))
