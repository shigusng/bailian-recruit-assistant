"""Environment diagnostics invoked by the panel's environment-check button."""
from __future__ import annotations

import importlib
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import tempfile

from runtime_paths import resolve_adb
from adb_connection import ConnectionCancelled, connect_emulator


class CheckCancelled(Exception):
    pass


def check_environment(base, settings_directory, adb, serial, roles, report, stop):
    base = Path(base)
    rows = {}
    reader = None

    def cancelled():
        if stop.is_set():
            raise CheckCancelled()

    def emit(name, status, detail):
        row = {'name': name, 'status': status, 'detail': str(detail)}
        rows[name] = row
        report(row)

    def run_adb(*arguments):
        cancelled()
        result = subprocess.run([adb_path, *arguments], capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cancelled()
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or 'ADB 命令执行失败')
        return result.stdout.decode('utf-8', 'replace').strip()

    try:
        cancelled()
        bits = struct.calcsize('P') * 8
        compatible = (3, 10) <= sys.version_info[:2] < (3, 13) and bits == 64
        emit('Python 运行环境', 'ok' if compatible else 'error',
             f'{platform.python_version()} / {bits} 位 / {platform.system()}；程序路径：{sys.executable}' +
             ('' if compatible else '。请使用 64 位 Python 3.10～3.12。'))

        dependencies = {}
        for title, module_name in (('NumPy', 'numpy'), ('OpenCV', 'cv2'), ('Pillow', 'PIL'),
                                   ('RapidOCR', 'rapidocr_onnxruntime'), ('ONNX Runtime', 'onnxruntime')):
            cancelled()
            try:
                module = importlib.import_module(module_name)
                dependencies[module_name] = module
                if module_name == 'cv2' and not hasattr(module, 'SIFT_create'):
                    raise RuntimeError('OpenCV 缺少 SIFT，需重新安装 requirements-gui.txt 中的依赖')
                emit(title, 'ok', f"已加载 {getattr(module, '__version__', '')}；{getattr(module, '__file__', '')}")
            except Exception as error:
                emit(title, 'error', f'{error}。请安装 requirements-gui.txt 中的依赖。')

        cancelled()
        try:
            from gpu_ocr import find_discrete_adapter, list_adapters
            adapters = list_adapters()
            adapter = find_discrete_adapter(adapters)
            if adapter is not None:
                emit('独立显卡', 'ok', f"{adapter['name']}；独立显存 {adapter['memory'] / 1024**3:.1f} GB")
            else:
                names = ' / '.join(item['name'] for item in adapters) or '未找到可用独显'
                emit('独立显卡', 'warn', f'{names}。将自动使用 CPU，仍可运行。')
            runtime = dependencies.get('onnxruntime')
            if runtime is not None:
                providers = runtime.get_available_providers()
                emit('GPU 运行库', 'ok' if 'DmlExecutionProvider' in providers else 'warn',
                     ' / '.join(providers) + ('' if 'DmlExecutionProvider' in providers else
                     '。GPU 后端不可用时自动使用 CPU；需要 GPU 时安装 requirements-gpu.txt。'))
        except Exception as error:
            emit('独立显卡', 'warn', f'{error}。显卡检测失败时自动使用 CPU。')

        cancelled()
        missing = [str(path) for path in
                   [base / 'assets' / filename for filename in ('hero_names.json', 'recruited_tick.png', 'panel_background.jpg')]
                   + [base / 'assets' / 'characters' / (role['id'] + '.png') for role in roles]
                   if not path.is_file()]
        emit('角色与界面资源', 'error' if missing else 'ok',
             '缺少文件：' + '、'.join(missing) if missing else f'名单中的 {len(roles)} 个角色图片和界面资源齐全。')
        try:
            with tempfile.TemporaryFile(dir=settings_directory) as temporary:
                temporary.write(b'check')
                temporary.flush()
            emit('统计保存目录', 'ok', f'{settings_directory} 可写，支持保存设置和抽取记录。')
        except OSError as error:
            emit('统计保存目录', 'error', f'{error}。请将脚本放在有写入权限的目录。')

        adb_path = resolve_adb(adb)
        connected = False
        if adb_path is None:
            emit('ADB 路径', 'error', '未找到 adb.exe，请在左上角「设置」中选择正确的 ADB 路径。')
        else:
            emit('ADB 路径', 'ok', adb_path)
            try:
                emit('ADB 运行', 'ok', run_adb('version'))
                _, serial, _ = connect_emulator(adb_path, serial, stop,
                    report=lambda message: emit('设备连接', 'running', message))
                connected = True
                emit('设备连接', 'ok', f'已连接 {serial}；连接参数会自动保存。')
            except (CheckCancelled, ConnectionCancelled):
                raise CheckCancelled()
            except Exception as error:
                emit('设备连接', 'error', str(error))

        cancelled()
        emit('OCR 模型与后端', 'running', '正在加载模型并选择独显或 CPU……')
        try:
            from gacha_core import SceneReader
            reader = SceneReader()
            emit('OCR 模型与后端', 'ok' if not reader.backend_note else 'warn',
                 reader.backend + (f'\n{reader.backend_note}' if reader.backend_note else '') +
                 '\n文字检测和文字识别模型已加载。')
        except Exception as error:
            emit('OCR 模型与后端', 'error', str(error))

        cancelled()
        if connected:
            try:
                try:
                    size = run_adb('-s', serial, 'shell', 'wm', 'size')
                    values = re.findall(r'(\d+)x(\d+)', size)
                    if not values or min(map(int, values[-1])) <= 0:
                        raise RuntimeError('未能读取有效设备尺寸：' + size)
                    width, height = map(int, values[-1])
                    emit('设备分辨率', 'ok', f'{width}×{height}；不限分辨率模式，以实际截图自动适配。')
                except CheckCancelled:
                    raise
                except Exception as error:
                    emit('设备分辨率', 'warn', f'{error}。将以实际截图尺寸继续检测。')
                from gacha_core import AdbDevice
                device = AdbDevice(adb=adb_path, serial=serial, stop=stop)
                image = device.screenshot()
                emit('画面自动适配', 'ok', device.geometry.describe() + '；识别位置与点击坐标自动换算。')
                emit('设备截图', 'ok', '截图读取成功；未点击游戏。')
                cancelled()
                if reader is not None:
                    emit('截图 OCR 识别', 'running', '正在验证当前截图的 OCR 识别……')
                    scene = reader.read(image)
                    emit('截图 OCR 识别', 'ok', '识别流程正常；' +
                         ('当前为金币招募界面。' if scene.kind == 'select' else '请在抽取前手动打开常驻招募界面，并保持画面完整可见。'))
                    emit('OCR 模型与后端', 'warn' if reader.backend_note else 'ok',
                         reader.backend + (f'\n{reader.backend_note}' if reader.backend_note else ''))
            except CheckCancelled:
                raise
            except Exception as error:
                cancelled()
                emit('设备截图与识别', 'error', str(error))
        else:
            emit('设备截图与识别', 'warn', '设备连接正常后，重新检测截图和识别流程。')
        cancelled()
    except CheckCancelled:
        return {'cancelled': True, 'ready': False, 'reader': None, 'rows': list(rows.values())}

    return {'cancelled': False, 'ready': not any(row['status'] == 'error' for row in rows.values()),
            'reader': reader, 'rows': list(rows.values()),
            'connection': {'adb': adb, 'serial': serial} if connected else None}
