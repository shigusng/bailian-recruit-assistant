"""Windowed release entry point, including import-time error reporting."""
from __future__ import annotations

from error_journal import ErrorJournal
from runtime_paths import BASE


def main():
    try:
        from gui import run_gui
        run_gui()
    except Exception as error:
        journal = ErrorJournal(BASE / 'error_logs')
        journal.record('程序启动', str(error), error=error)
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('启动失败', f'{error}\n\n详细错误日志：{journal.path}', parent=root)
        root.destroy()


if __name__ == '__main__':
    main()
