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
        check('release title',w.windowTitle()=='Stereo Motion Measurement')
        check('no bundled sessions',w.records_table.rowCount()==0)
        check('seven pages',w.pages.count()==7)
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
            w.navigate('precision');check('known synthetic displacement','100.000' in w.precision_result.text())
            w.navigate('replay');w.replay_view.fit_trajectory();QTest.qWait(250);app.processEvents();w.grab().save(str(out/'synthetic_playback.png'))
        check('no history written to release',w.records_table.rowCount()==0)
        check('settings save',w.save_settings())
        w.resize(1100,760);w.navigate('settings');app.processEvents();w.grab().save(str(out/'settings_small.png'))
        code=0
    except Exception:
        code=1
        (out/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
    (out/'result.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    if code==0 and os.environ.get('SMM_LANGUAGE_CYCLE') and not getattr(app,'cycle_done',False):
        app.cycle_done=True
        w.language.setCurrentIndex(0 if w.config.get('language')=='en' else 1)
    else:w.close();app.exit(code)
