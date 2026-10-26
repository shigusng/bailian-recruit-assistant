"""Discover local emulators and establish ADB transports before game actions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

from runtime_paths import resolve_adb


class ConnectionCancelled(Exception):
    pass


class DeviceChoiceRequired(RuntimeError):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__('发现多个可用设备，请在「设置 → 设备序列号」的下拉列表中选择，再点击「一键连接」。')


@dataclass(frozen=True)
class DeviceCandidate:
    serial: str
    name: str
    state: str

    @property
    def emulator(self):
        return self.serial.startswith('emulator-') or local_endpoint(self.serial) is not None

    @property
    def description(self):
        state = {'device': '已连接', 'available': '待连接', 'offline': '离线', 'unauthorized': '未授权'}[self.state]
        return f'{self.name}（{self.serial}，{state}）' if self.name != self.serial else f'{self.serial}（{state}）'


def cancelled(stop):
    if stop is not None and stop.is_set():
        raise ConnectionCancelled()


def local_endpoint(serial):
    match = re.fullmatch(r'(127\.0\.0\.1|localhost):(\d{1,5})', serial, re.I)
    if match and 0 < int(match[2]) <= 65535:
        return '127.0.0.1', int(match[2])
    return None


def adb_command(adb, *arguments, stop=None):
    cancelled(stop)
    try:
        result = subprocess.run([adb, *arguments], capture_output=True, timeout=8,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('ADB 连接超时，请确认模拟器已启动，并开启 ADB 调试（本机连接）。') from error
    cancelled(stop)
    output = result.stdout.decode('utf-8', 'replace').strip()
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or output or 'ADB 连接失败')
    return output


def list_devices(adb, stop=None):
    output = adb_command(adb, 'devices', '-l', stop=stop)
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in ('device', 'offline', 'unauthorized'):
            continue
        model = next((part[6:].replace('_', ' ') for part in parts[2:] if part.startswith('model:')), '')
        devices.append(DeviceCandidate(parts[0], model or parts[0], parts[1]))
    return devices


def emulator_processes(stop):
    if sys.platform != 'win32':
        return []
    cancelled(stop)
    script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
              "Get-CimInstance Win32_Process | Where-Object { $_.Name -match "
              "'(?i)mumu|nemu|ldplayer|dnplayer|ldvbox|ld9box|nox' } | "
              "Select-Object Name,ExecutablePath | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                                capture_output=True, timeout=6,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cancelled(stop)
        if result.returncode or not result.stdout.strip():
            return []
        rows = json.loads(result.stdout.decode('utf-8-sig', 'replace'))
        return rows if isinstance(rows, list) else [rows]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def discover_emulators(adb, stop=None, report=lambda message: None, devices=None):
    """Probe only loopback ports belonging to known emulator layouts."""
    devices = list_devices(adb, stop) if devices is None else devices
    report('正在扫描本机模拟器……')
    processes = emulator_processes(stop)
    roots = set()
    for process in processes:
        if process.get('ExecutablePath'):
            roots.update(list(Path(process['ExecutablePath']).parents)[:5])
    for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(variable):
            roots.add(Path(os.environ[variable]) / 'Netease' / 'MuMu')
            roots.add(Path(os.environ[variable]) / 'Netease' / 'MuMuPlayer-12.0')
    ports = {}
    for root in roots:
        cancelled(stop)
        for config in (root / 'vms').glob('*/configs/vm_config.json'):
            try:
                value = json.loads(config.read_text(encoding='utf-8-sig'))
                port = int(value['vm']['nat']['port_forward']['adb']['host_port'])
                if 0 < port <= 65535:
                    ports[port] = 'MuMu · ' + config.parent.parent.name
            except (OSError, ValueError, KeyError, TypeError):
                continue
    names = ' '.join(process.get('Name', '') for process in processes).lower()
    # Configured MuMu ports take priority over aliases such as 5555/7555.
    # This avoids presenting the same MuMu instance as several devices.
    mumu_fallback = not ports and ('mumu' in names or 'nemu' in names)
    if mumu_fallback:
        ports.update({port: 'MuMu' for port in (16384, 7555, 5555)})
    if any(process.get('Name', '').lower() in ('ldplayer.exe', 'dnplayer.exe', 'ldvboxheadless.exe', 'ld9boxheadless.exe')
           for process in processes):
        ports.update({5555 + index * 2: '雷电模拟器' for index in range(16) if 5555 + index * 2 not in ports})
    if 'nox' in names:
        ports.update({62001 + index * 25: '夜神模拟器' for index in range(8)})

    def listening(port):
        cancelled(stop)
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.15):
                return port
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        opened = {port for port in pool.map(listening, ports) if port is not None}
    if mumu_fallback:
        aliases = [port for port in (16384, 7555, 5555) if port in opened and ports[port] == 'MuMu']
        opened.difference_update(aliases[1:])
    candidates = {device.serial: device for device in devices}
    for port in sorted(opened):
        serial = f'127.0.0.1:{port}'
        state = candidates[serial].state if serial in candidates else 'available'
        candidates[serial] = DeviceCandidate(serial, ports[port], state)
    return list(candidates.values())


def connect_emulator(adb, serial, stop=None, report=lambda message: None):
    adb = resolve_adb(adb)
    if adb is None:
        raise RuntimeError('未找到 ADB，请在「设置」中选择 adb.exe；便携版可填 adb。')
    devices = list_devices(adb, stop)
    selected = next((device for device in devices if device.serial == serial and device.state == 'device'), None)
    if selected is not None:
        return adb, selected.serial, devices
    candidates = discover_emulators(adb, stop, report, devices)
    selected = next((device for device in candidates if device.serial == serial), None)
    if selected is None:
        emulators = [device for device in candidates if device.emulator]
        if len(emulators) == 1:
            selected = emulators[0]
        elif emulators or candidates:
            raise DeviceChoiceRequired(candidates)
        elif local_endpoint(serial):
            selected = DeviceCandidate(serial, '已保存的模拟器', 'available')
        else:
            raise RuntimeError('未找到可用模拟器。请先启动模拟器，并在模拟器设置中开启 ADB 调试（本机连接），再点击「扫描模拟器」或「一键连接」。')
    if selected.state == 'unauthorized':
        raise RuntimeError(f'{selected.serial} 尚未授权，请在模拟器中允许 ADB 调试后重新连接。')
    if local_endpoint(selected.serial):
        report('正在连接 ' + selected.serial + '……')
        message = adb_command(adb, 'connect', selected.serial, stop=stop)
    else:
        message = ''
    current = list_devices(adb, stop)
    state = next((device.state for device in current if device.serial == selected.serial), None)
    if state != 'device':
        hint = '请在模拟器中允许 ADB 调试。' if state == 'unauthorized' else '请确认模拟器已启动，并开启 ADB 调试（本机连接）。'
        raise RuntimeError(f'未能连接 {selected.serial}（{state or "未发现设备"}）。{hint}' + ('\n' + message if message else ''))
    report('已连接：' + selected.serial)
    return adb, selected.serial, candidates
