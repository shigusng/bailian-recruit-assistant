"""Thread-safe, local error records with bounded log-file rotation."""
from __future__ import annotations

from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import traceback
import uuid

from recruit_history import display_time, now


class JournalHandler(RotatingFileHandler):
    def __init__(self, journal):
        self.journal = journal
        super().__init__(journal.path, maxBytes=2 * 1024**2, backupCount=3, encoding='utf-8', delay=True)
        self.setFormatter(logging.Formatter('%(message)s'))

    def handleError(self, record):
        # A full/read-only disk must not cause a second exception loop in the UI.
        self.journal.write_error = str(sys.exc_info()[1])


class ErrorJournal:
    def __init__(self, directory):
        self.path = Path(directory) / 'errors.log'
        self.lock = threading.RLock()
        self.entries = deque(maxlen=500)
        self.handler = None
        self.write_error = ''
        for path in [Path(str(self.path) + f'.{index}') for index in (3, 2, 1)] + [self.path]:
            try:
                with path.open(encoding='utf-8') as source:
                    for line in source:
                        try:
                            entry = json.loads(line)
                            if isinstance(entry, dict) and all(isinstance(entry.get(key), str)
                                                              for key in ('id', 'time', 'level', 'source', 'message', 'details')):
                                self.entries.append(entry)
                        except (ValueError, TypeError):
                            continue
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError) as error:
                self.write_error = str(error)

    def record(self, source, message, error=None, details='', level='ERROR'):
        if error is not None:
            details = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        entry = {'id': uuid.uuid4().hex, 'time': now(), 'level': level,
                 'source': str(source), 'message': str(message), 'details': str(details)}
        with self.lock:
            self.entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.handler is None:
                    self.handler = JournalHandler(self)
                self.write_error = ''
                record = logging.LogRecord('bailian.errors', logging.ERROR, '', 0,
                                           json.dumps(entry, ensure_ascii=False), (), None)
                self.handler.handle(record)
            except OSError as error:
                self.write_error = str(error)
        return entry

    def snapshot(self):
        with self.lock:
            return [dict(entry) for entry in reversed(self.entries)]

    @staticmethod
    def format_entry(entry):
        level = '错误' if entry['level'] == 'ERROR' else '提示'
        return (f"时间：{display_time(entry['time'])}（北京时间）\n"
                f"来源：{entry['source']}\n级别：{level}\n"
                f"信息：{entry['message']}\n\n{entry['details']}").rstrip()

    def export_text(self):
        return ('\n\n' + '=' * 72 + '\n\n').join(self.format_entry(entry) for entry in self.snapshot())
