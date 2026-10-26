"""Keep bundled resources separate from writable portable-app data."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys


RESOURCE_BASE = Path(__file__).resolve().parent
BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else RESOURCE_BASE
BUNDLED_ADB = RESOURCE_BASE / 'adb' / 'adb.exe'


def resolve_adb(value):
    value = str(value).strip().strip('"')
    if not value:
        return None
    # Keep the saved value "adb" portable when the whole folder is moved.
    if value.lower() in ('adb', 'adb.exe') and BUNDLED_ADB.is_file():
        return str(BUNDLED_ADB)
    path = Path(value).expanduser()
    if not path.is_absolute() and (BASE / path).is_file():
        return str((BASE / path).resolve())
    if path.is_file():
        return str(path.resolve())
    return shutil.which(value)
