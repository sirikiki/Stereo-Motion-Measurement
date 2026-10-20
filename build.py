"""Build the portable release on Windows; do not package acquired data."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
root=Path(__file__).resolve().parent
name='Stereo Motion Measurement'
env=os.environ.copy()
windows=Path(os.environ.get('SystemRoot','C:/Windows'))
env['PATH']=os.pathsep.join([str(Path(sys.executable).parent),str(windows/'System32'),str(windows)])
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed','--onedir','--name',name,
 '--icon',str(root/'assets/app.ico'),'--distpath',str(root/'dist'),'--workpath',str(root/'build/work'),
 '--specpath',str(root/'build'),'--paths',str(root/'source'),'--add-data',str(root/'source/translations.json')+';.',
 '--collect-submodules','OpenGL','--collect-data','pyqtgraph','--exclude-module','matplotlib',
 '--exclude-module','PySide6.QtWebEngineCore','--exclude-module','PySide6.QtWebEngineWidgets',
 '--exclude-module','PySide6.QtQml','--exclude-module','PySide6.QtQuick',str(root/'source/app.py')],check=True,env=env)
dest=root/'dist'/name
for folder in ['source','assets','calibration']:
 shutil.copytree(root/folder,dest/folder,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
(dest/'config').mkdir(exist_ok=True)
shutil.copy2(root/'config/defaults.json',dest/'config/settings.json')
shutil.copy2(root/'config/defaults.json',dest/'config/defaults.json')
for folder in ['data/sessions','data/history','logs']:(dest/folder).mkdir(parents=True,exist_ok=True)
for f in ['README.md','使用说明.md','requirements.txt','build.py','.gitignore']:
 if (root/f).exists():shutil.copy2(root/f,dest/f)
if (dest/'_internal/icuuc.dll').exists():raise RuntimeError('Unexpected app-local ICU DLL')
print(dest)
