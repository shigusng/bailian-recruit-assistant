"""Launch the recruitment panel; keep legacy calibration commands available."""
from __future__ import annotations

import sys


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else 'gui'
    if mode in ('gui', 'run'):
        from launcher import main as launch_panel
        launch_panel()
    elif mode in ('cal', 'snap', 'test'):
        from tools.legacy_desktop import main as calibrate
        calibrate(mode)
    else:
        raise SystemExit('用法：python auto_gacha.py [gui|run|cal|snap|test]')


if __name__ == '__main__':
    main()
