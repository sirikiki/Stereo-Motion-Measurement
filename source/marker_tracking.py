from i18n import tr, set_language
'Track a dark circular marker on a light card near a user-selected seed.'
import math
import cv2
import numpy as np

def detect_black_circle(image, hint, expected_radius=None, search_radius=None):
    if hint is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hx, hy = (float(x) for x in hint)
    radius = int(search_radius or max(220, min(480, 3.2 * (expected_radius or 0))))
    x0, y0 = (max(0, int(hx - radius)), max(0, int(hy - radius)))
    x1, y1 = (min(image.shape[1], int(hx + radius)), min(image.shape[0], int(hy + radius)))
    if x1 <= x0 or y1 <= y0:
        return None
    region = gray[y0:y1, x0:x1]
    threshold, _ = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = float(np.clip(threshold, 45, 135))
    mask = cv2.inRange(region, 0, threshold)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    choices = []
    for contour in contours:
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)
        x, y, w, h = cv2.boundingRect(contour)
        if not 45 < area < 300000 or perimeter == 0 or (not 0.5 < w / h < 2.0):
            continue
        if x <= 0 or y <= 0 or x + w >= region.shape[1] or (y + h >= region.shape[0]):
            continue
        circularity = 4 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.45:
            continue
        moments = cv2.moments(contour)
        cx, cy = (moments['m10'] / moments['m00'] + x0, moments['m01'] / moments['m00'] + y0)
        distance = math.hypot(cx - hx, cy - hy)
        if distance > 100:
            continue
        r = math.sqrt(area / math.pi)
        if expected_radius and (not 0.45 < r / expected_radius < 2.2):
            continue
        bx0, by0 = (max(0, int(cx - 2 * r)), max(0, int(cy - 2 * r)))
        bx1, by1 = (min(gray.shape[1], int(cx + 2 * r) + 1), min(gray.shape[0], int(cy + 2 * r) + 1))
        yy, xx = np.ogrid[by0:by1, bx0:bx1]
        radial = (xx - cx) ** 2 + (yy - cy) ** 2
        patch = gray[by0:by1, bx0:bx1]
        inside = patch[radial < (0.65 * r) ** 2]
        ring = patch[(radial > (1.25 * r) ** 2) & (radial < (1.8 * r) ** 2)]
        if not len(inside) or not len(ring):
            continue
        contrast = float(np.median(ring) - np.median(inside))
        if contrast < 25:
            continue
        score = distance / 100 + 0.3 * (1 - circularity) + (0.2 * abs(math.log(r / expected_radius)) if expected_radius else 0)
        choices.append((score, {'center': (cx, cy), 'area_px': area, 'radius_px': r, 'circularity': circularity, 'contrast': contrast, 'aspect_ratio': w / h}))
    return min(choices, key=lambda x: x[0])[1] if choices else None
