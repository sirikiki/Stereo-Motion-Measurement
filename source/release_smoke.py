"""Offline release checks. Synthetic data only; never opens a camera."""
import csv,json,os,re,tempfile,traceback
from pathlib import Path
import numpy as np
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel,QPushButton,QComboBox
from i18n import tr

def run_smoke(w,app):
    checks=[]
    out=Path(os.environ.get('SMM_QA_DIR',tempfile.gettempdir()))
    out=out/w.config.get('language','en') if os.environ.get('SMM_LANGUAGE_CYCLE') else out
    out.mkdir(parents=True,exist_ok=True)
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    try:
        check('no camera opened',w.worker is None)
        check('release title',w.windowTitle()==tr('双目运动测量'))
        check('window icon has image',not w.windowIcon().isNull() and not w.windowIcon().pixmap(32,32).isNull())
        check('application icon has image',not app.windowIcon().isNull())
        import sys
        if sys.platform=='win32':
            guard=w.taskbar_guard;guard.refresh();native=guard.snapshot()
            check('shell window identity set',native['error'] is None and native['properties']['ID']=='Siriy.StereoMotionMeasurement')
            if getattr(sys,'frozen',False):
                check('shell icon resource points to executable',native['properties']['RelaunchIconResource'].endswith('.exe,0'))
            hwnd=int(w.winId())
            for size in (0,1):guard.user.SendMessageW(hwnd,0x80,size,0)
            guard.refresh();native=guard.snapshot()
            check('native icon loss repaired',native['big_icon_matches'] and native['small_icon_matches'])
            for action in (w.showMinimized,w.showNormal,w.showMaximized,w.showNormal):
                action();app.processEvents();guard.refresh()
            native=guard.snapshot();check('icons survive window state changes',native['big_icon_matches'] and native['small_icon_matches'])
            (out/'native_taskbar.json').write_text(json.dumps(native,indent=2),encoding='utf-8')

        check('no bundled sessions',w.records_table.rowCount()==0)
        check('eight pages',w.pages.count()==8)
        check('brand follows language',w.brand_title.text()==tr('双目运动测量'))
        check('camera setup between playback and settings',w.page_ids[-3:]==['replay','camera_setup','settings'])
        check('language first setting',w.language.parentWidget().layout().itemAt(0).layout().labelForField(w.language).text() in ('Language / 语言','语言 / Language'))
        check('author credit',any(x.text()=='A software made by Siri.y' for x in w.findChildren(QLabel)))
        for page in w.page_ids:
            w.navigate(page);app.processEvents()
            check('page '+page,w.page_is(page))
            w.grab().save(str(out/(page+'.png')))
        visible_text=[x.text() for x in w.findChildren(QLabel) if x.isVisible()]
        for retired in ('范围说明','轨迹显示长度','目标类型','标定参数','Range information','Trajectory length','Target type','Calibration'):
            check('retired setting '+retired,retired not in visible_text)
        check('settings notes hidden',w.frame_tip.isHidden())
        if w.config.get('language')=='en':
            chinese=[]
            for x in w.findChildren(QLabel)+w.findChildren(QPushButton):
                if re.search('[\u4e00-\u9fff]',x.text()) and x.text()!='Language / 语言':chinese.append(x.text())
            check('English UI has no untranslated labels',not chinese)
        w.navigate('environment');w.env_board.setChecked(True);w.environment_board_changed(True);app.processEvents()
        check('board fields visible only when enabled',w.board_fields.isVisible())
        check('cell size placeholders empty',w.board_square.placeholderText()=='' and w.board_square_height.placeholderText()=='')
        w.resize(1100,760);app.processEvents()
        check('board input fields fit window',w.board_square_height.mapTo(w,w.board_square_height.rect().topRight()).x()<w.width())
        check('board input shares assistance button row',abs(w.board_fields.mapTo(w,w.board_fields.rect().center()).y()-w.env_board.mapTo(w,w.env_board.rect().center()).y())<=4)
        w.grab().save(str(out/'board_fields_small.png'))
        w.env_board.setChecked(False);w.environment_board_changed(False)
        check('board fields hide when disabled',w.board_fields.isHidden())
        def board_layout_bounds():
            return [(v.mapTo(w,v.rect().topLeft()).x(),v.mapTo(w,v.rect().topLeft()).y(),v.width(),v.height()) for v in (w.env_board,w.env_mode,*w.env_cameras)]
        app.processEvents();before_board_bounds=board_layout_bounds()
        w.env_board.setChecked(True);w.environment_board_changed(True);app.processEvents()
        check('board assistance toggle preserves geometry',board_layout_bounds()==before_board_bounds)
        w.env_board.setChecked(False);w.environment_board_changed(False);app.processEvents()
        check('board assistance hide preserves geometry',board_layout_bounds()==before_board_bounds)
        w.navigate('curves')
        for plot,line in zip(w.curve_plots,w.curve_lines):
            line.setData([0,1,2],[0,.0001,.0005]);plot.enableAutoRange();app.processEvents()
            check('curve uses fixed mm '+str(len(checks)),plot.getAxis('left').autoSIPrefixScale==1.0 and plot.getAxis('left').labelUnits=='mm')
        w.navigate('monitor');w.toggle_sidebar();check('sidebar collapsed',w.sidebar.isHidden());w.toggle_sidebar()
        w.toggle_expand();check('expanded trajectory',w.expanded);w.leave_expand();check('restored trajectory',not w.expanded)
        for key in ('images','trajectory','xyz'):
            w.toggles[key].setChecked(False);w.toggles[key].setChecked(True)
        with tempfile.TemporaryDirectory(prefix='stereo_synthetic_') as temp:
            folder=Path(temp);path=folder/'measurements.csv'
            fields=['host_read_complete_ns','valid','x_m','y_m','z_m','phase','attempt']
            with path.open('w',newline='',encoding='utf-8') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
                for i in range(180):
                    phase=('A1','B','A2')[i//60]
                    writer.writerow(dict(host_read_complete_ns=10**9+i*100000000,valid=int(i!=65),x_m=.1 if phase=='B' else 0,y_m=.01,z_m=.7,phase=phase,attempt=1))
            w.load_replay(path);w.navigate('replay');app.processEvents()
            check('synthetic CSV read',len(w.replay_samples)==180)
            check('missing frame retained',not w.replay_samples[65]['valid'] and np.isnan(w.replay_samples[65]['xyz']).all())
            w.slider.setValue(179);w.replay_view.position_switch.set_relative(False);w.replay_view.denoise_switch.set_relative(True)
            check('replay relative/absolute switch',not w.replay_view.relative)
            check('replay filtering switch',w.replay_view.denoise)
            w.open_replay_curves();check('replay to curves',w.page_is('curves') and not w.curve_back.isHidden())
            w.return_to_replay();check('return to playback',w.page_is('replay'))
            w.update_curves();data_id=id(w.curve_lines[0].xData)
            check('unchanged curve data skipped',w.update_curves() is False and id(w.curve_lines[0].xData)==data_id)
            check('curve display clips to view',all(line.opts['clipToView'] for line in w.curve_lines))
            check('curve display reduction uses extrema',all(line.opts['autoDownsample'] and line.opts['downsampleMethod']=='peak' for line in w.curve_lines))

            w.navigate('precision');check('known synthetic displacement','100.000' in w.precision_result.text())
            w.navigate('replay');w.replay_view.fit_trajectory();QTest.qWait(250);app.processEvents();w.grab().save(str(out/'synthetic_playback.png'))
        for size in ((1100,760),(1440,900)):
            w.resize(*size);w.navigate('curves');app.processEvents()
            for plot in w.curve_plots:
                axis=plot.getAxis('left')
                center=axis.label.mapRectToParent(axis.label.boundingRect()).center().y()
                check('axis label centered '+str(size)+str(len(checks)),abs(center-axis.size().height()/2)<=2)
        check('no history written to release',w.records_table.rowCount()==0)
        check('settings save',w.save_settings())
        w.resize(1100,760);w.navigate('settings');app.processEvents();w.grab().save(str(out/'settings_small.png'))
        from camera_dialog import CameraSetupPage
        from camera_setup import split_frame, profile, load_calibration
        w.navigate('camera_setup');dialog=w.camera_setup;app.processEvents()
        check('camera setup embedded',not dialog.isWindow())
        check('two bottom actions',dialog.save_btn.text() in ('Save camera setup','保存相机配置'))
        check('camera setup starts without camera access',dialog.preview is None)
        check('calibration requires captured pairs',not dialog.solve_btn.isEnabled())
        check('calibration activation requires checked result',not dialog.use_btn.isEnabled())
        check('new calibration requires measured cell dimensions',dialog.cellw.value()==0 and dialog.cellh.value()==0)
        dialog.grab().save(str(out/'camera_setup.png'))
        dialog.tabs.setCurrentIndex(1);app.processEvents();dialog.grab().save(str(out/'camera_calibration.png'))
        w.navigate('monitor')
        raw=np.zeros((240,640,3),np.uint8);raw[:,320:]=255
        check('packed image split',split_frame(raw,{})[1].mean()==255)
        check('eye order swap',split_frame(raw,dict(camera_swap=True))[0].mean()==255)
        from storage import DEFAULTS
        check('fresh users do not inherit original calibration',DEFAULTS['calibration']=='')
        check('no private calibration bundled',not any((__import__('storage').ROOT/'calibration').glob('*.npz')))
        code=0
    except Exception:
        code=1
        (out/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
    (out/'result.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    if code==0 and os.environ.get('SMM_LANGUAGE_CYCLE') and not getattr(app,'cycle_done',False):
        app.cycle_done=True
        w.language.setCurrentIndex(0 if w.config.get('language')=='en' else 1)
    else:w.close();app.exit(code)
