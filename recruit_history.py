"""Persistent records for individual recruitment runs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import uuid


LOCAL_TIMEZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')


def now():
    return datetime.now(LOCAL_TIMEZONE).isoformat(timespec='seconds')


def display_time(value):
    if not value:
        return ''
    return datetime.fromisoformat(value).astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')


class RecruitHistory:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.runs = json.loads(self.path.read_text(encoding='utf-8')).get('runs', []) if self.path.exists() else []
        changed = False
        for run in self.runs:
            if run['status'] == 'running':
                # A crash/power loss does not provide a reliable ending time.
                run.update(status='interrupted', reason='上次运行未正常结束，结束时间未知')
                changed = True
        if changed:
            self._save()

    def _save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'runs': self.runs}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)

    def _run(self, run_id):
        return next(run for run in self.runs if run['id'] == run_id)

    def snapshot(self):
        with self.lock:
            return deepcopy(self.runs)

    def begin(self, targets):
        with self.lock:
            run_id = uuid.uuid4().hex
            self.runs.append({'id': run_id, 'started_at': now(), 'ended_at': None,
                              'status': 'running', 'reason': '', 'rounds': 0, 'coins': 0,
                              'targets': deepcopy(targets), 'recruited': {}})
            try:
                self._save()
            except OSError:
                self.runs.pop()
                raise
            return run_id

    def record_draw(self, run_id, rounds, coins):
        with self.lock:
            run = self._run(run_id)
            run.update(rounds=rounds, coins=coins)
            self._save()

    def record_recruited(self, run_id, hero_id, name):
        with self.lock:
            run = self._run(run_id)
            name = next((target['name'] for target in run['targets'] if target['id'] == hero_id), name)
            count = run['recruited'].setdefault(hero_id, {'name': name, 'count': 0})
            count['count'] += 1
            self._save()

    def finish(self, run_id, status, reason, rounds, coins):
        with self.lock:
            run = self._run(run_id)
            if run['ended_at'] is not None:
                return
            run.update(ended_at=now(), status=status, reason=reason, rounds=rounds, coins=coins)
            self._save()
