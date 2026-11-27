"""Persist safe execution summaries without commands, terminal output, or input values."""

from contextlib import closing
import json
import sqlite3
import time

SCHEMA = ('CREATE TABLE IF NOT EXISTS runs (seq INTEGER PRIMARY KEY, id TEXT UNIQUE, '
          'updated INTEGER NOT NULL, details TEXT NOT NULL)')

ACTIVE = {'connecting', 'switching', 'dispatch', 'running', 'between', 'waiting', 'stopping'}


def outcome(entry):
    """Map a persisted run snapshot to the compact status used by filters and cards."""
    phase = entry.get('phase')
    statuses = {step.get('status') for step in entry.get('steps', [])}
    if phase == 'succeeded':
        return 'succeeded'
    if phase == 'disconnected' or 'needs-review' in statuses:
        return 'review'
    if 'failed' in statuses:
        return 'failed'
    if phase in ACTIVE:
        return 'running'
    if phase == 'paused':
        return 'paused'
    if phase in ('ended', 'exited'):
        return 'cancelled'
    if 'interrupted' in statuses or 'detached' in statuses:
        return 'review'
    return phase or 'unknown'


class RunLog:
    def __init__(self, path):
        self.path = path

    def save(self, entry):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(SCHEMA)
            db.execute('INSERT INTO runs (id, updated, details) VALUES (?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, details=excluded.details',
                       (entry['id'], int(time.time() * 1000), json.dumps(entry, ensure_ascii=False)))

    def page(self, before=None, limit=30, server=None, query=None, status=None, since=None):
        if not self.path.exists():
            return {'runs': [], 'before': None}
        limit = max(1, min(int(limit), 200))
        server = (server or '').strip().casefold()
        query = (query or '').strip().casefold()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(SCHEMA)
            cursor = db.execute('SELECT seq, updated, details FROM runs '
                                'WHERE (? IS NULL OR seq < ?) ORDER BY seq DESC', (before, before))
            matches = []
            for sequence, updated, details in cursor:
                entry = json.loads(details)
                entry['updatedAt'] = updated
                target = entry.get('target') or {}
                server_text = ' '.join(str(target.get(key) or '') for key in ('name', 'host', 'port')).casefold()
                if server and server not in server_text:
                    continue
                if query and query not in details.casefold():
                    continue
                if since is not None and int(entry.get('startedAt') or 0) < since:
                    continue
                if status and outcome(entry) != status:
                    continue
                matches.append((sequence, entry))
                if len(matches) > limit:
                    break
        runs = [entry for _, entry in matches[:limit]]
        return {'runs': runs, 'before': matches[limit - 1][0] if len(matches) > limit else None}
