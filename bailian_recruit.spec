# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata


project = Path(SPECPATH)
dependency_paths = [project / '.gpu-deps', project / '.deps']
sys.path[:0] = [str(path) for path in dependency_paths]
adb_directory = Path(os.environ['BAILIAN_BUILD_ADB_DIR'])

datas = [(str(project / 'assets'), 'assets')]
datas += collect_data_files('rapidocr_onnxruntime')
for distribution in ('rapidocr-onnxruntime', 'onnxruntime-directml', 'numpy', 'opencv-python',
                     'Pillow', 'pyclipper', 'shapely', 'PyYAML', 'coloredlogs', 'humanfriendly',
                     'flatbuffers', 'packaging', 'six'):
    datas += copy_metadata(distribution)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    datas.append((str(python_license), 'licenses'))
datas += [(str(adb_directory / filename), 'adb') for filename in
          ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt')]

a = Analysis(
    [str(project / 'launcher.py')],
    pathex=[str(project), *map(str, dependency_paths)],
    binaries=collect_dynamic_libs('onnxruntime'),
    datas=datas,
    hiddenimports=collect_submodules('rapidocr_onnxruntime') + ['PIL._tkinter_finder'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'tensorflow', 'matplotlib', 'pandas', 'scipy',
              'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'pytest', 'IPython', 'onnx'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='百炼英雄抽取助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory='_internal',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='百炼英雄抽取助手')
