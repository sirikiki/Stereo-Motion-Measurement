"""Image observations, not diagnoses of camera hardware or true scene geometry."""
import cv2
import numpy as np
from i18n import tr

def observations(views):
    result=[]
    for im in views:
        gray=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY) if im.ndim==3 else im
        sample=gray[::max(1,gray.shape[0]//160),::max(1,gray.shape[1]//200)]
        lo,median,hi=np.percentile(sample,[5,50,95])
        smooth=cv2.GaussianBlur(sample.astype(np.float32),(5,5),1.2)
        structure=float(np.std(smooth)/max(float(np.std(sample)),1.))
        result.append(dict(median=float(median),range_p95_p05=float(hi-lo),
            spatial_structure_ratio=structure,
            dark_fraction=float(np.mean(sample<32)),saturated_fraction=float(np.mean(sample>=250))))
    return result

def enhancement_useful(stats):
    return any(x['range_p95_p05']>=8 and x['spatial_structure_ratio']>.25 and
        (x['median']<55 or x['range_p95_p05']<60 or x['dark_fraction']>.45) for x in stats)

def enhance_pair(views):
    # One bounded fallback only. Never replace the captured or displayed image.
    clahe=cv2.createCLAHE(clipLimit=2.,tileGridSize=(8,8))
    return [clahe.apply(cv2.cvtColor(v,cv2.COLOR_BGR2GRAY)) for v in views]

def hint(stats):
    if not stats:return ''
    messages=[]
    if any(x['median']<45 for x in stats):
        messages.append(tr('影像偏暗：建议增加均匀照明，避免只提高增益。'))
    if any(x['saturated_fraction']>.20 for x in stats):
        messages.append(tr('影像有较多高亮饱和区域：请避开直射光或反光。'))
    if any(x['range_p95_p05']<25 for x in stats):
        messages.append(tr('影像对比度偏低：请改善照明或使用棋盘格辅助。'))
    if len(stats)==2 and abs(stats[0]['median']-stats[1]['median'])>45:
        messages.append(tr('左右影像亮度差异较大：请检查两侧照明与曝光设置。'))
    return ' '.join(messages)
