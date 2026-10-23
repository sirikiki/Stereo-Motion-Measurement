"""User-operated capture and calibration; background work never touches Qt widgets."""
import json
import threading
import time
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal, QTimer, Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,
    QSpinBox,QDoubleSpinBox,QComboBox,QCheckBox,QTabWidget,QWidget,QPlainTextEdit,QFileDialog,QMessageBox,QInputDialog,QScrollArea,QGridLayout,QSplitter,QProgressBar,QSizePolicy)
import i18n
from storage import ROOT, write_json
from widgets import CameraView
from camera_setup import PROFILE, profile, split_frame, open_device, load_calibration, object_grid, find_pair, solve_calibration, calibration_digest

def txt(zh,en):
    return zh if i18n._language=='zh_CN' else en

class Job(QThread):
    result=Signal(object)
    def __init__(self, function, parent):
        super().__init__(parent); self.function=function
    def run(self):
        try: self.result.emit((True,self.function()))
        except Exception as e: self.result.emit((False,str(e)))
        finally:self.function=None

class Preview(QThread):
    failed=Signal(str)
    def __init__(self, config, parent):
        super().__init__(parent); self.config=config; self.stop=threading.Event(); self.lock=threading.Lock(); self.latest=None
    def run(self):
        cap=None
        try:
            cap=open_device(self.config); start=time.perf_counter(); count=0
            while not self.stop.is_set():
                ok,raw=cap.read()
                if not ok: raise ValueError('Camera read failed.')
                views=split_frame(raw,self.config); count+=1
                f=cap.get(cv2.CAP_PROP_FOURCC);f=int(f) if np.isfinite(f) else 0
                fourcc=''.join(chr((f>>(8*i))&255) for i in range(4)).strip('\x00') or 'unknown'
                fps=cap.get(cv2.CAP_PROP_FPS);fps=float(fps) if np.isfinite(fps) and fps>0 else 0.
                info=dict(width=raw.shape[1],height=raw.shape[0],fps_reported=fps,fps_observed=count/max(time.perf_counter()-start,.001),format=fourcc)
                with self.lock: self.latest=(raw,views,info,time.perf_counter())
        except Exception as e: self.failed.emit(str(e))
        finally:
            if cap is not None: cap.release()

class CameraSetupPage(QWidget):
    applied=Signal(object)
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config=dict(config); self.original=profile(config); self.result_config=None
        self.preview=None; self.jobs=[]; self.busy=False; self.pairs=[]; self.descriptors=[]; self.raw_size=None
        self.session=None; self.attempt=0; self.proposal=None; self.verified=None; self.imported=None
        self.can_access=lambda:True
        self.setObjectName("cameraSetupPage")
        self.setStyleSheet('QWidget#cameraSetupPage { background: transparent; }')
        box=QVBoxLayout(self);box.setContentsMargins(0,0,0,0);box.setSpacing(10)
        self.splitter=QSplitter(Qt.Vertical);self.splitter.setChildrenCollapsible(False)
        self.tabs=QTabWidget();self.tabs.setMinimumHeight(170);self.splitter.addWidget(self.tabs)
        self.tabs.setStyleSheet('QTabWidget::pane { border: 1px solid #dce3ed; border-radius: 10px; background: white; } QTabBar::tab { padding: 8px 12px; border: none; color: #61718a; } QTabBar::tab:selected { color: #245cb0; background: #e5eefc; border-radius: 6px; }')
        setup=QWidget(); form=QGridLayout(setup);form.setHorizontalSpacing(14);form.setVerticalSpacing(8);self.fields={}
        labels={'camera_index':txt('设备编号（0–20）','Device index (0–20)'), 'camera_backend':txt('采集接口','Capture backend'),'camera_width':txt('拼接总宽度 px','Packed frame width px'),'camera_height':txt('拼接总高度 px','Packed frame height px'),'camera_fps':txt('请求帧率','Requested FPS'),'camera_format':txt('像素格式','Pixel format'),'camera_layout':txt('画面排列','Packed layout'),'camera_swap':txt('交换左右镜头','Swap left/right eyes')}
        for k,v in self.original.items():
            if k=='camera_swap': w=QCheckBox(); w.setChecked(v)
            elif isinstance(v,int):
                w=QSpinBox(); limits={'camera_index':(0,20),'camera_width':(320,8192),'camera_height':(240,8192),'camera_fps':(1,240)}; w.setRange(*limits[k]);w.setValue(v)
            else:
                w=QComboBox(); choices={'camera_backend':['DSHOW','MSMF','AUTO'],'camera_format':['MJPG','YUY2','DEFAULT'],'camera_layout':['sbs','tb']}[k]
                for c in choices: w.addItem({'sbs':txt('左右拼接','Side by side'),'tb':txt('上下拼接','Top / bottom')}.get(c,c),c)
                w.setCurrentIndex(choices.index(v))
            self.fields[k]=w
            i=list(self.original).index(k);row,col=divmod(i,3);field=QWidget();fl=QHBoxLayout(field);fl.setContentsMargins(0,0,0,0);fl.setSpacing(6)
            title=QLabel(labels[k]);title.setWordWrap(True);fl.addWidget(title);fl.addWidget(w,1);form.addWidget(field,row,col)
        quick=QLabel(txt('首次使用：①预览并确认左右画面；②没有本机标定就采集并计算，已有本机文件则导入（二选一）；③启用后保存配置，返回监视。随附标定不能直接用于其他相机。仅支持单个UVC设备输出同步左右/上下拼接、普通针孔镜头、水平基线。同步能力需由设备确认。',
            'First use: preview both eyes → capture and compute OR import calibration for this rig → activate and save → Monitor. Bundled calibration is not universal. Supports one UVC device with synchronized side-by-side/top-bottom views, pinhole lenses and a horizontal baseline; confirm synchronization with the device supplier.'))
        quick.setWordWrap(True);quick.setMinimumHeight(quick.fontMetrics().lineSpacing()*4);form.addWidget(quick,4,0,1,3)
        self.devices=QLabel(txt('设备编号是采集接口的索引；可先试0或1。点击预览后才会访问相机。','The device number is the backend index; try 0 or 1 first. The camera is opened only when you click preview.'));self.devices.setWordWrap(True);form.addWidget(self.devices,3,0,1,2)
        self.scan_btn=QPushButton(txt('搜索可用设备（逐个测试0–9）','Find devices (test indices 0–9)'));self.scan_btn.clicked.connect(self.scan);form.addWidget(self.scan_btn,3,2)
        help_text={
            'camera_backend':txt('DSHOW和MSMF是Windows采集后端，不是USB插口类型。AUTO由OpenCV选择后端，不会自动识别双目排列或标定。','DSHOW/MSMF are Windows capture backends, not USB connector types. AUTO lets OpenCV choose a backend; it does not identify stereo layout or calibration.'),
            'camera_format':txt('MJPG是压缩视频，通常节约USB带宽；YUY2通常占用更多带宽。DEFAULT保留驱动格式。以实际预览返回为准。','MJPG is compressed and usually uses less USB bandwidth; YUY2 usually needs more bandwidth. DEFAULT keeps the driver format. Check the actual preview mode.'),
            'camera_width':txt('填写两眼拼接后的总尺寸，不是单眼尺寸。驱动可能返回其他模式；保存以实际画面尺寸为准。','Enter the total packed size, not per-eye size. Drivers may return a different mode; saving uses the actual frame dimensions.')}
        for key,text in help_text.items():self.fields[key].setToolTip(text)
        self.fields['camera_height'].setToolTip(help_text['camera_width'])
        self.add_tab(setup,txt('1 · 相机连接' ,'1 · Camera connection'))
        board=QWidget();bl=QVBoxLayout(board)
        instructions=QLabel(txt('固定相机与对焦，将棋盘格贴平在硬板上。按下方指引移动棋盘格，摆稳后左键采集；两眼都需看到完整棋盘格。角度和位置是建议，软件不测量你的摆放角度。','Fix the rig and focus; mount the board flat on a rigid backing. Follow the guide, hold still and capture. Keep the whole board visible in both eyes. Poses are suggestions, not measured angles.'));instructions.setWordWrap(True);instructions.setObjectName('muted');bl.addWidget(instructions)
        r=QHBoxLayout();r.setSpacing(10)
        self.cols=QSpinBox();self.rows=QSpinBox()
        for w,v in [(self.cols,10),(self.rows,7)]:w.setRange(4,50);w.setValue(v);w.setFixedWidth(66)
        self.cellw=QDoubleSpinBox();self.cellh=QDoubleSpinBox()
        for w in (self.cellw,self.cellh):w.setRange(0,500);w.setDecimals(2);w.setValue(0);w.setSpecialValueText(txt('请实测','Measure'));w.setFixedWidth(95)
        self.board_fields=[self.cols,self.rows,self.cellw,self.cellh]
        for title,items in [(txt('方格数（列 × 行）','Squares (cols × rows)'),(self.cols,self.rows)),(txt('单格尺寸（宽 × 高，mm）','Cell size (width × height, mm)'),(self.cellw,self.cellh))]:
            group=QWidget();line=QHBoxLayout(group);line.setContentsMargins(0,0,0,0);line.setSpacing(5)
            caption=QLabel(title);caption.setWordWrap(True);line.addWidget(caption);line.addWidget(items[0]);line.addWidget(QLabel('×'));line.addWidget(items[1]);r.addWidget(group)
        r.addStretch();bl.addLayout(r)
        self.cols.setToolTip(txt('包括最外圈的黑白方格数量。例如10×7方格对应9×6内角点。','Count all squares, including the outer row: 10×7 squares = 9×6 inner corners.'))
        self.rows.setToolTip(self.cols.toolTip())
        for w in (self.cellw,self.cellh):w.setToolTip(txt('请实测一个方格的宽和高，不是整张纸尺寸。','Measure one cell, not the whole sheet.'))
        self.guide_title=QLabel();self.guide_title.setStyleSheet('font-weight: 600; color: #245cb0;')
        self.guide=QLabel();self.guide.setWordWrap(True)
        self.guide_box=QWidget();self.guide_box.setStyleSheet('background: #edf3fc; border-radius: 8px;')
        gl=QVBoxLayout(self.guide_box);gl.setContentsMargins(10,7,10,7);gl.setSpacing(3)
        heading=QHBoxLayout();self.guide_title.setWordWrap(True);heading.addWidget(self.guide_title,1)
        self.count=QLabel();heading.addWidget(self.count)
        self.progress=QProgressBar();self.progress.setRange(0,30);self.progress.setTextVisible(False);self.progress.setFixedSize(70,6);heading.addWidget(self.progress)
        gl.addLayout(heading);gl.addWidget(self.guide);bl.addWidget(self.guide_box)
        r=QHBoxLayout();self.take_btn=self.add_button(r,txt('采集一组','Capture pair'),self.take_pair);self.solve_btn=self.add_button(r,txt('计算并检查','Compute and check'),self.compute);self.reset_btn=self.add_button(r,txt('新建采集批次','New capture batch'),self.reset_batch);bl.addLayout(r)
        bl.setAlignment(Qt.AlignTop);bl.setContentsMargins(10,8,10,8);bl.setSpacing(7)
        self.update_guide()
        self.add_tab(board,txt('2 · 采集标定图','2 · Capture calibration'))
        results=QWidget();rl=QVBoxLayout(results);self.report=QPlainTextEdit();self.report.setReadOnly(True);self.report.setMinimumHeight(65);self.report.setMaximumHeight(115);rl.addWidget(self.report)
        self.report.setPlainText(txt('当前标定：','Current calibration: ')+(config.get('calibration') or txt('未设置（仅预览）','Not set (preview only)')))
        r=QHBoxLayout();self.import_btn=self.add_button(r,txt('导入已有标定','Import calibration'),self.import_file);self.use_btn=self.add_button(r,txt('确认并启用标定','Confirm and activate'),self.use_result);self.current_btn=self.add_button(r,txt('读取当前标定以重新核对','Review current calibration'),self.review_current);rl.addLayout(r)
        note=QLabel(txt('导入支持本软件NPZ和OpenCV YAML/XML（K_left/K_right或M1/M2、D1/D2、R、T和单眼尺寸）。仅支持针孔模型及水平基线。重投影/留出检查不等于独立精度验证；启用后请用未参与标定的已知长度验证。','Import: app NPZ or OpenCV YAML/XML (K_left/K_right or M1/M2, D1/D2, R, T and per-eye dimensions). Pinhole model and horizontal baseline only. Reprojection/held-out checks are not independent accuracy verification; verify a separate known length after activation.'));note.setWordWrap(True);rl.addWidget(note)
        self.add_tab(results,txt('3 · 结果与导入','3 · Results and import'))
        cameras=QWidget();vr=QHBoxLayout(cameras);vr.setContentsMargins(0,0,0,0);vr.setSpacing(12)
        self.views=[CameraView(txt('左相机','Left eye')),CameraView(txt('右相机','Right eye'))]
        for title,w in zip((txt('左相机','Left eye'),txt('右相机','Right eye')),self.views):
            frame=QWidget();frame.setObjectName('card');fl=QVBoxLayout(frame);fl.setContentsMargins(10,8,10,10);fl.setSpacing(5);fl.addWidget(QLabel(title))
            w.setMinimumHeight(135);w.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding);fl.addWidget(w,1);vr.addWidget(frame,1)
        self.splitter.addWidget(cameras);self.splitter.setStretchFactor(0,0);self.splitter.setStretchFactor(1,1);self.splitter.setSizes([330,415]);box.addWidget(self.splitter,1)
        self.status=QLabel(txt('尚未开启预览。','Preview is closed.'));self.status.setWordWrap(True);self.status.setObjectName('muted');box.addWidget(self.status)
        r=QHBoxLayout();self.preview_btn=self.add_button(r,txt('开启原始预览','Open raw preview'),self.toggle_preview);self.save_btn=self.add_button(r,txt('保存相机配置','Save camera setup'),self.save);r.addStretch();box.addLayout(r)
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(100);self.refresh()

    def update_guide(self):
        n=len(self.pairs);self.progress.setValue(min(n,30));self.count.setText(txt('已接受 ','Accepted ')+f'{n} / 30 '+txt('组（至少）','pairs (minimum)'))
        if n>=30:
            self.guide_title.setText(txt('已达到最低组数 · 可以计算并检查','Minimum reached · ready to compute and check'))
            self.guide.setText(txt('可继续补拍不同位置和倾角；若检查提示覆盖不足，请补拍缺少的区域。30组不保证质量一定通过。','You may add more varied views. If coverage is insufficient, capture the missing regions. Thirty pairs do not guarantee a pass.'));return
        positions=[('中央','centre'),('左上区域','upper-left area'),('右上区域','upper-right area'),('左下区域','lower-left area'),('右下区域','lower-right area')]
        # Six groups of five views: repeat image coverage at different distances/tilts.
        poses=[('中等距离，板宽约占单眼画面35%–45%，基本正对相机。','Medium distance; board width about 35–45% of one view, roughly facing the rig.'),
               ('稍远一些，板宽约占25%–35%，左右倾斜约15°–20°。','Slightly farther; board width about 25–35%; yaw left or right by about 15–20°.'),
               ('稍近一些，板宽约占45%–55%，上下倾斜约15°–20°。','Slightly nearer; board width about 45–55%; tilt up or down by about 15–20°.'),
               ('中等距离，向相反的左右方向倾斜约20°–30°。','Medium distance; yaw about 20–30° in the opposite direction.'),
               ('中等距离，向相反的上下方向倾斜约20°–30°。','Medium distance; tilt about 20–30° up/down in the opposite direction.'),
               ('改变远近，并轻微组合上下/左右倾斜；可在画面内旋转约10°–15°。','Vary distance and combine gentle yaw/pitch; add about 10–15° of in-plane rotation.')]
        zh,en=positions[n%5];pz,pe=poses[n//5]
        self.guide_title.setText(txt(f'第{n+1} / 30组建议 · 棋盘格中心放在{zh}',f'Pose {n+1} / 30 · board centre in the {en}'))
        self.guide.setText(txt(pz+' 四角和白边不要出画面；位置以两眼完整可见为准。',pe+' Keep all corners and the white border visible in both eyes; move inward if needed.'))

    def has_work(self):
        return self.preview is not None or self.busy or any(j.isRunning() for j in self.jobs)

    def stop_preview(self):
        if self.preview:self.preview.stop.set();self.preview_btn.setEnabled(False)

    def prepare_shutdown(self):
        self.stop_preview()
        return not self.has_work()

    def add_tab(self,widget,title):
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QScrollArea.NoFrame);scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }");scroll.viewport().setAutoFillBackground(False);scroll.setWidget(widget);widget.setAutoFillBackground(False);self.tabs.addTab(scroll,title)

    def add_button(self,row,text,fn):
        b=QPushButton(text);b.clicked.connect(fn);row.addWidget(b);return b
    def values(self):
        d={}
        for k,w in self.fields.items(): d[k]=w.isChecked() if isinstance(w,QCheckBox) else w.currentData() if isinstance(w,QComboBox) else w.value()
        return profile(d)
    def message(self,s):
        if i18n._language=='zh_CN':
            replacements={
                'Complete board not found in both views. Check square counts, focus, lighting and board edges.':'左右画面未同时找到完整棋盘格。请检查方格数量、焦点、光照和边缘是否完整。',
                'Pose too similar. Move the board across the frame, change distance or tilt.':'姿态与已保存图像过于相似。请改变棋盘格位置、距离或倾角。',
                'Board is too small in the image. Move it closer.':'棋盘格在画面中过小，请适当靠近。',
                'Insufficient board tilt diversity. Add left/right and up/down tilts (not only translation).':'棋盘格倾角变化不足。请增加向左右和上下倾斜的姿态，不要只平移。',
                'Cannot open camera. Check device index, USB connection and other camera apps.':'无法打开相机，请检查设备编号、USB连接，并关闭占用相机的其他程序。',
                'Camera read failed.':'相机读取失败。请检查连接和采集模式。',
                'Stereo board orientation is inconsistent. Keep the same complete board visible in both eyes.':'左右棋盘格方向不一致。请确保同一张完整棋盘格同时出现在两幅图中。',
                'Requires a horizontal baseline with left/right order. Swap eyes and recalibrate if reversed.':'当前仅支持水平基线且左右镜头顺序正确的双目。顺序反了请交换镜头并重新标定。',
                'Held-out stereo depth is invalid. Check eye order and corner correspondence.':'留出图像深度检查失败，请检查左右镜头顺序和角点对应。'}
            for en,zh in replacements.items():s=s.replace(en,zh)
        self.status.setText(s)
    def refresh(self):
        running=self.preview is not None
        available=self.can_access()
        for w in self.fields.values():w.setEnabled(available and not running and self.session is None and not self.busy)
        for w in self.board_fields:w.setEnabled(available and self.session is None and not self.busy)
        self.scan_btn.setEnabled(available and not running and not self.busy and self.session is None)
        self.take_btn.setEnabled(available and running and not self.busy)
        self.solve_btn.setEnabled(available and len(self.pairs)>=30 and not self.busy)
        self.use_btn.setEnabled(available and self.proposal is not None and not self.busy)
        self.current_btn.setEnabled(available and not self.busy and bool(self.config.get('calibration')));self.import_btn.setEnabled(available and not self.busy);self.reset_btn.setEnabled(not self.busy)
        self.save_btn.setEnabled(available and not self.busy)
        self.preview_btn.setEnabled(available and not self.busy and (not running or not self.preview.stop.is_set()))
    def scan(self):
        if not self.can_access():return
        p=self.values()
        def work():
            found=[]
            for index in range(10):
                cap=None
                try:
                    cap=open_device(dict(p,camera_index=index));ok,raw=cap.read()
                    if ok:found.append((index,raw.shape[1],raw.shape[0]))
                except Exception:pass
                finally:
                    if cap is not None:cap.release()
            return found
        def done(found):
            if not found:self.message(txt('未找到设备。可手动输入10–20或尝试其他采集接口。','No device found. Try indices 10–20 manually or another backend.'));return
            items=[f'#{i} · {w} × {h}' for i,w,h in found]
            choice,ok=QInputDialog.getItem(self,txt('选择设备','Select device'),txt('请再预览确认是双目相机，而非笔记本摄像头。','Preview to confirm this is the stereo device, not a laptop webcam.'),items,0,False)
            if ok:self.fields['camera_index'].setValue(found[items.index(choice)][0])
        self.message(txt('后台搜索中，部分驱动可能响应较慢…','Searching in the background; some camera drivers may respond slowly…'));self.run_job(work,done)
    def run_job(self,fn,done):
        self.busy=True;self.refresh();j=Job(fn,self);self.jobs.append(j)
        def result(value):
            self.busy=False
            try:
                if value[0]: done(value[1])
                else:self.message(txt('失败：','Failed: ')+value[1])
            except Exception as error:self.message(txt('失败：','Failed: ')+str(error))
            self.refresh()
        j.result.connect(result);j.finished.connect(self.refresh);j.start()
    def toggle_preview(self):
        if not self.can_access():self.message(txt('请先关闭主界面的相机预览。','Close the main camera preview first.'));return
        if self.preview:
            self.preview.stop.set();self.preview_btn.setEnabled(False);return
        try:p=self.values()
        except Exception as e:self.message(str(e));return
        self.preview=Preview(p,self);self.preview.failed.connect(self.message)
        self.preview.finished.connect(self.preview_done);self.preview.start();self.preview_btn.setText(txt('停止预览','Stop preview'));self.refresh()
    def preview_done(self):
        self.preview.deleteLater();self.preview=None;self.preview_btn.setEnabled(True);self.preview_btn.setText(txt('开启原始预览','Open raw preview'));self.refresh()
    def current(self):
        if self.preview:
            with self.preview.lock:
                if self.preview.latest and time.perf_counter()-self.preview.latest[3]<1:
                    raw,views,info,stamp=self.preview.latest
                    return raw.copy(),[v.copy() for v in views],dict(info),stamp
        raise ValueError(txt('没有新鲜画面，请先开启预览。','No fresh frame. Open preview first.'))
    def poll(self):
        self.refresh()
        if not self.can_access():
            self.message(txt('主相机正在使用中；请先关闭顶部相机预览，再配置或采集。','The main camera is in use. Close the top camera preview before setup or capture.'));return
        if not self.preview:return
        try:raw,views,info,_=self.current()
        except ValueError:return
        self.verified=dict(profile=self.values(),actual=info)
        for w,im in zip(self.views,views):w.set_frame(im)
        self.preview_btn.setToolTip(f"{info['width']} × {info['height']} · {info['format']} · {info['fps_observed']:.1f} FPS observed")
        # Do not overwrite capture/calibration feedback with the preview status.
        if not self.busy and self.tabs.currentIndex()==0:
            self.message(txt('实际返回：','Actual: ')+f"{info['width']} × {info['height']} · {info['format']} · {info['fps_observed']:.1f} FPS · "+txt('单眼 ','per eye ')+f'{views[0].shape[1]} × {views[0].shape[0]}')
    def reset_batch(self):
        self.pairs=[];self.descriptors=[];self.session=None;self.raw_size=None;self.proposal=None;self.imported=None;self.update_guide();self.message(txt('新批次已准备，旧批次原图仍保留。','New batch ready. Previous batch images are retained.'));self.refresh()
    def take_pair(self):
        if min(self.cellw.value(),self.cellh.value())<=0:
            self.message(txt('请先填写实测单格宽、高（毫米），不是整张纸尺寸。','Enter the measured cell width and height in mm, not the sheet size.'));return
        try:raw,views,info,_=self.current()
        except Exception as e:self.message(str(e));return
        shape=views[0].shape[1::-1]
        if self.raw_size and self.raw_size!=shape:self.message(txt('分辨率发生变化，请新建批次。','Resolution changed. Start a new batch.'));return
        if self.session is None:
            self.session=ROOT/'data/calibration_runs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f');self.session.mkdir(parents=True)
            write_json(self.session/'setup.json',dict(profile=self.values(),actual=info,squares=[self.cols.value(),self.rows.value()],cell_mm=[self.cellw.value(),self.cellh.value()]))
        self.attempt+=1;stem=self.session/f'{self.attempt:04d}';pattern=(self.cols.value()-1,self.rows.value()-1)
        previous=list(self.descriptors)
        def work():
            ok,encoded=cv2.imencode('.png',raw)
            if not ok:raise OSError('Cannot encode original image.')
            encoded.tofile(str(stem)+'.png')
            try:
                cs=find_pair(views,pattern)
                c=cs[0].reshape(-1,2);norm=c/np.array(shape);descriptor=norm[[0,pattern[0]-1,-1,-pattern[0]]].ravel()
                if previous and min(np.linalg.norm(descriptor-d) for d in previous)<.10:raise ValueError('Pose too similar. Move the board across the frame, change distance or tilt.')
                if np.min(np.ptp(c,axis=0))<60:raise ValueError('Board is too small in the image. Move it closer.')
                write_json(Path(str(stem)+'.json'),dict(accepted=True,corners=[a.reshape(-1,2).tolist() for a in cs]))
                return cs,descriptor
            except Exception as e:
                write_json(Path(str(stem)+'.json'),dict(accepted=False,reason=str(e)));raise
        def done(value):
            cs,d=value;self.pairs.append(cs);self.descriptors.append(d);self.raw_size=shape;self.proposal=None;self.imported=None
            self.update_guide();self.message(txt('已保存。请改变棋盘格位置、距离或倾角，再静止采集。','Saved. Change board position, distance or tilt; hold still before the next capture.'))
        self.message(txt('正在检查并保存这一组…','Checking and saving this pair…'));self.run_job(work,done)
    def compute(self):
        self.proposal=None;self.imported=None;self.refresh()
        if len(self.pairs)<30:return
        ds=np.array(self.descriptors).reshape(-1,4,2);centres=ds.mean(axis=1)
        areas=np.array([abs(cv2.contourArea(d.astype(np.float32))) for d in ds])
        if np.ptp(centres[:,0])<.30 or np.ptp(centres[:,1])<.25 or areas.max()/max(areas.min(),1e-8)<1.5:
            self.message(txt('覆盖不足：请补拍四周位置，以及更近和更远的棋盘格。','Insufficient coverage: add positions around the frame and nearer/farther distances.'));return
        obj=object_grid(self.cols.value(),self.rows.value(),self.cellw.value(),self.cellh.value())
        pairs=list(self.pairs);size=self.raw_size;session=self.session
        def work():
            p,report=solve_calibration(pairs,size,obj);write_json(session/'report.json',report);np.savez(session/'candidate.npz',**p);return p,report
        def done(value):
            p,report=value
            summary=[txt('检查结果：','Check result: ')+(txt('通过','Passed') if report['passed'] else txt('未通过','Failed')),
                     txt('拟合图组 / 留出检查图组：','Fit pairs / held-out pairs: ')+f"{report['train_pairs']} / {report['heldout_pairs']}",
                     txt('左 / 右 / 双目重投影误差（像素，越小越好）：','Left / right / stereo reprojection error (pixels, lower is better): ')+f"{report['left_rms_px']:.3f} / {report['right_rms_px']:.3f} / {report['stereo_rms_px']:.3f}",
                     txt('留出图像左右垂直误差95分位（像素）：','Held-out vertical mismatch, 95th percentile (pixels): ')+f"{report['heldout_epipolar_p95_px']:.3f}",
                     txt('留出棋盘格长度相对误差95分位：','Held-out board length error, 95th percentile: ')+f"{report['heldout_length_error_p95_percent']:.2f}%",
                     txt('双目基线（毫米）：','Stereo baseline (mm): ')+f"{report['baseline_mm']:.2f}",
                     txt('以上是拟合和同一棋盘格的留出检查，不代表独立精度验证。原始图像、失败原因和详细报告已保存。','These are fit and held-out checks using the same board, not independent accuracy verification. Original images, failures and the detailed report are saved.'),str(self.session)]
            summary.append(txt('各留出图组（接受序号：垂直差px / 长度误差）：','Held-out pairs (accepted index: vertical mismatch px / length error):')+'\n'+'\n'.join(f"#{v['accepted_pair']}: {v['epipolar_p95_px']:.3f} px / {v['length_error_p95_percent']:.2f}%" for v in report.get('heldout_views',[])))
            self.report.setPlainText('\n\n'.join(summary));self.tabs.setCurrentIndex(2)
            self.proposal=(p,session/'candidate.npz') if report['passed'] else None
            self.message(txt('检查通过，可以启用；仍需独立长度验证。','Checks passed. You may activate; independent length verification is still required.') if report['passed'] else txt('检查未通过。请增加覆盖、倾角，检查纸板平整度和焦点；旧标定保留。','Checks failed. Improve coverage/tilts, board flatness and focus. Previous calibration is retained.'))
        self.message(txt('后台计算中，可能需要数分钟。请稍候；窗口仍可移动。','Computing in the background; this may take several minutes. The window remains responsive.'));self.run_job(work,done)
    def import_file(self):
        path,_=QFileDialog.getOpenFileName(self,txt('导入标定','Import calibration'),'', 'Calibration (*.npz *.yml *.yaml *.xml)')
        if not path:return
        units='m'
        if Path(path).suffix.lower()!='.npz':
            q=QMessageBox(self);q.setWindowTitle(txt('平移向量T的单位','Translation T units'));q.setText(txt('请按导出文件的实际单位选择，不要猜测。','Select the actual translation unit in the export file; do not guess.'))
            m=q.addButton('m',QMessageBox.AcceptRole);mm=q.addButton('mm',QMessageBox.AcceptRole);q.addButton(QMessageBox.Cancel);q.exec()
            if q.clickedButton() not in (m,mm):return
            units='mm' if q.clickedButton()==mm else 'm'
        self.proposal=None;self.imported=None;self.refresh()
        def done(p):
            self.proposal=(p,Path(path));self.report.setPlainText(txt('文件结构通过，尚未实测验证。\n','File structure passed; not yet verified with real measurements.\n')+f"Per eye: {p['image_size']}\nBaseline: {np.linalg.norm(p['T'])*1000:.2f} mm")
            self.message(txt('先确认预览是此文件对应的相机、分辨率、左右顺序，再点击启用。','Verify this is the matching camera, resolution and eye order in preview, then activate.'))
        self.run_job(lambda:load_calibration(path,units),done)
    def review_current(self):
        path=ROOT/self.config.get('calibration','')
        self.proposal=None;self.imported=None;self.refresh()
        def done(p):
            self.proposal=(p,path)
            self.report.setPlainText(txt('已读取当前文件，仅完成结构检查。请预览并确认它属于当前设备、对焦和左右顺序，然后启用并保存。其他相机必须重新标定或导入自己的文件。','Current file loaded; structural checks only. Preview and confirm the same device, focus and eye order, then activate and save. A different camera needs its own calibration.'))
        self.run_job(lambda:load_calibration(path),done)

    def use_result(self):
        if not self.proposal:return
        p,origin=self.proposal
        if not self.verified or self.verified['profile']!=self.values():self.message(txt('请先开启当前配置的预览以核对尺寸。','Preview this configuration first to verify dimensions.'));return
        a=self.verified['actual'];shape=(a['width']//2,a['height']) if self.values()['camera_layout']=='sbs' else (a['width'],a['height']//2)
        if tuple(p['image_size'])!=shape:self.message(txt('标定单眼尺寸与实际画面不一致，禁止启用。','Calibration per-eye size does not match the actual frame. Activation blocked.'));return
        if QMessageBox.question(self,txt('确认标定对应关系','Confirm calibration match'),txt('确认此标定属于当前相机、当前对焦和左右顺序？尺寸相同不代表标定可通用。','Does this calibration belong to this camera, focus and eye order? Matching resolution alone does not make calibration interchangeable.'))!=QMessageBox.Yes:return
        target=ROOT/'calibration'/('user_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.npz');target.parent.mkdir(exist_ok=True)
        try:
            np.savez(target,**p);self.imported=(str(target.relative_to(ROOT)),self.values());self.message(txt('新标定已另存，点击保存相机配置以应用。','New calibration saved separately. Save camera setup to apply it.'))
        except Exception as e:self.message(str(e))
    def save(self):
        if not self.can_access():return
        try:p=self.values()
        except Exception as e:self.message(str(e));return
        if not self.verified or self.verified['profile']!=p:self.message(txt('请先预览核对实际画面，再保存。','Preview and verify the actual views before saving.'));return
        a=self.verified['actual'];p.update(camera_width=a['width'],camera_height=a['height'])
        try:p=profile(p)
        except Exception as e:self.message(str(e));return
        updated=dict(self.config,**p)
        if self.imported and self.imported[1]==self.values():updated.update(calibration=self.imported[0],calibration_profile=p.copy(),calibration_sha256=calibration_digest(ROOT/self.imported[0]))
        elif p!=self.original:updated.update(calibration='',calibration_profile=None,calibration_sha256=None)
        updated['camera_actual']=a;self.result_config=updated
        if self.preview:self.preview.stop.set();self.preview_btn.setEnabled(False);QTimer.singleShot(150,self.finish_save)
        else:self.finish_save()
    def finish_save(self):
        if self.preview:QTimer.singleShot(150,self.finish_save);return
        self.applied.emit(self.result_config)

    def mark_saved(self,config):
        self.config=dict(config);self.original=profile(config)
        # Reflect the negotiated size, otherwise the next save can erase calibration.
        for key,value in self.original.items():
            widget=self.fields[key]
            if isinstance(widget,QCheckBox):widget.setChecked(value)
            elif isinstance(widget,QComboBox):widget.setCurrentIndex(widget.findData(value))
            else:widget.setValue(value)
        self.verified=None;self.imported=None;self.proposal=None
        self.message(txt('相机配置已保存。可返回监视开启相机；新标定请再做已知长度验证。','Camera setup saved. Return to Monitor to open the camera; verify new calibration with a separate known length.'))

    def closeEvent(self,event):
        if not self.prepare_shutdown():event.ignore();return
        self.timer.stop();super().closeEvent(event)
