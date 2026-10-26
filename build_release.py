"""Build a self-contained Windows release without launching the application."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile


PROJECT = Path(__file__).resolve().parent
APP_NAME = '百炼英雄抽取助手'


def find_adb_directory():
    candidates = []
    if os.environ.get('BAILIAN_BUILD_ADB_DIR'):
        candidates.append(Path(os.environ['BAILIAN_BUILD_ADB_DIR']))
    settings_path = PROJECT / 'settings.json'
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
        if settings.get('adb'):
            candidates.append(Path(settings['adb']).parent)
    executable = shutil.which('adb')
    if executable:
        candidates.append(Path(executable).parent)
    candidates.append(PROJECT / 'adb')
    required = ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt')
    for directory in candidates:
        if all((directory / name).is_file() for name in required):
            return directory.resolve()
    raise RuntimeError('找不到完整的 ADB 文件；请设置 BAILIAN_BUILD_ADB_DIR 为 platform-tools 目录。')


def build():
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        raise RuntimeError('请使用 Windows 64 位 Python 打包。')
    if not (PROJECT / '.deps' / 'rapidocr_onnxruntime' / '__init__.py').is_file():
        raise RuntimeError('缺少 .deps 运行依赖，请先安装 requirements-gui.txt。')
    if not (PROJECT / '.gpu-deps' / 'onnxruntime' / 'capi' / 'DirectML.dll').is_file():
        raise RuntimeError('缺少 .gpu-deps GPU 运行库，请先安装 requirements-gpu.txt。')

    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
    release_directory = PROJECT / 'dist' / timestamp
    # A new directory for every build preserves previous releases and their user data.
    release_directory.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment['BAILIAN_BUILD_ADB_DIR'] = str(find_adb_directory())
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(release_directory),
                    '--workpath', str(PROJECT / '.build-artifacts' / 'pyinstaller'),
                    str(PROJECT / 'bailian_recruit.spec')],
                   cwd=PROJECT, env=environment, check=True)

    application = release_directory / APP_NAME
    executable = application / (APP_NAME + '.exe')
    required = [executable, application / '_internal' / 'assets' / 'panel_background.jpg',
                application / '_internal' / 'adb' / 'adb.exe',
                application / '_internal' / 'onnxruntime' / 'capi' / 'DirectML.dll']
    required += [application / '_internal' / 'rapidocr_onnxruntime' / 'models' / name for name in
                 ('ch_PP-OCRv4_det_infer.onnx', 'ch_PP-OCRv4_rec_infer.onnx',
                  'ch_ppocr_mobile_v2.0_cls_infer.onnx')]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError('打包缺少文件：\n' + '\n'.join(missing))

    shutil.copy2(PROJECT / 'docs' / 'release_readme.txt', application / '使用说明.txt')
    shutil.copy2(PROJECT / 'assets' / 'triple_roles.csv', application / '抽取角色名单.csv')
    archive = release_directory / 'BailianHero-AutoGacha-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(application.rglob('*')):
            if path.is_file():
                package.write(path, path.relative_to(release_directory))
    print(f'EXE: {executable}', flush=True)
    print(f'ZIP: {archive}', flush=True)
    print('Build complete. The application was not launched.', flush=True)


if __name__ == '__main__':
    build()
