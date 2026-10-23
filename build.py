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
version_args=[]
if (root/'version_info.txt').is_file():version_args=['--version-file',str(root/'version_info.txt')]
subprocess.run([sys.executable,'-m','PyInstaller',*version_args,'--noconfirm','--clean','--windowed','--onedir','--name',name,
 '--icon',str(root/'assets/app.ico'),'--distpath',str(root/'dist'),'--workpath',str(root/'build/work'),
 '--specpath',str(root/'build'),'--paths',str(root/'source'),'--add-data',str(root/'source/translations.json')+';.',
 '--add-data',str(root/'assets/app.ico')+';assets','--collect-submodules','OpenGL','--collect-data','pyqtgraph','--exclude-module','matplotlib',
 '--exclude-module','PySide6.QtWebEngineCore','--exclude-module','PySide6.QtWebEngineWidgets',
 '--exclude-module','PySide6.QtQml','--exclude-module','PySide6.QtQuick',str(root/'source/app.py')],check=True,env=env)
dest=root/'dist'/name
for folder in ['source','assets','calibration']:
 if (root/folder).is_dir():shutil.copytree(root/folder,dest/folder,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
(dest/'calibration').mkdir(exist_ok=True)
(dest/'config').mkdir(exist_ok=True)
shutil.copy2(root/'config/defaults.json',dest/'config/settings.json')
shutil.copy2(root/'config/defaults.json',dest/'config/defaults.json')
for folder in ['data/sessions','data/history','logs']:(dest/folder).mkdir(parents=True,exist_ok=True)
for f in ['requirements.txt','build.py','.gitignore','VERSION.txt']:
 if (root/f).exists():shutil.copy2(root/f,dest/f)
if (dest/'_internal/icuuc.dll').exists():raise RuntimeError('Unexpected app-local ICU DLL')
print(dest)
