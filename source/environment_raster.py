"""Explicit full-view / analysis coordinates. Pixel centres use origin-preserving remap."""
import cv2
import numpy as np
from i18n import tr

def analysis_size(image_size):
    w,h=map(int,image_size)
    if min(w,h)<80: raise ValueError('Environment image is too small')
    # Computational budget, not a required camera mode. Never enlarge input.
    scale=min(1.,640/w,480/h)
    size = max(1,round(w*scale)),max(1,round(h*scale))
    if min(size)<64:
        raise ValueError(tr('图像宽高比过大，环境分析短边不足64像素；请在相机配置中选择更均衡的输出尺寸。'))
    return size

def scale_xy(shape,image_size):
    h,w=shape[:2]
    if min(h,w,*image_size)<=0: raise ValueError('Invalid raster dimensions')
    return np.asarray([w/image_size[0],h/image_size[1]],float)

def to_work(points,shape,image_size):
    return np.asarray(points,float)*scale_xy(shape,image_size)

def to_full(points,shape,image_size):
    return np.asarray(points,float)/scale_xy(shape,image_size)

def sample_image(image,size):
    if image.shape[1::-1]==tuple(size): return image.copy()
    y,x=np.indices(size[::-1],dtype=np.float32)
    # Unlike resize, remap samples u=x/s exactly, matching scaled P and K.
    x*=image.shape[1]/size[0];y*=image.shape[0]/size[1]
    return cv2.remap(image,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)

def area_limit(value,shape):
    return max(16,round(value*shape[0]*shape[1]/(640*480)))

def kernel(value,shape):
    k=max(3,round(value*min(shape[1]/640,shape[0]/480)))
    return k if k%2 else k+1

def checked_views(views,image_size):
    """Normalize supported camera output without guessing bit depth or cropping."""
    if len(views)!=2:
        raise ValueError(tr('环境分析需要两幅尺寸与标定一致的8位相机影像。'))
    result=[]
    for im in views:
        if not isinstance(im,np.ndarray) or im.dtype!=np.uint8 or im.shape[1::-1]!=tuple(image_size):
            raise ValueError(tr('环境分析需要两幅尺寸与标定一致的8位相机影像。'))
        if im.ndim==2: im=cv2.cvtColor(im,cv2.COLOR_GRAY2BGR)
        elif im.ndim==3 and im.shape[2]==1: im=cv2.cvtColor(im[:,:,0],cv2.COLOR_GRAY2BGR)
        elif im.ndim==3 and im.shape[2]==4: im=cv2.cvtColor(im,cv2.COLOR_BGRA2BGR)
        elif im.ndim!=3 or im.shape[2]!=3:
            raise ValueError(tr('环境分析需要两幅尺寸与标定一致的8位相机影像。'))
        result.append(im)
    return result
