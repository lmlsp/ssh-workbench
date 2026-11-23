"""Persist execution results without storing commands, terminal output, or input values."""

from contextlib import closing
import json
import sqlite3
import time

SCHEMA = ('CREATE TABLE IF NOT EXISTS runs (seq INTEGER PRIMARY KEY, id TEXT UNIQUE, '
          'updated INTEGER NOT NULL, details TEXT NOT NULL)')


class RunLog:
    def __init__(self, path):
        self.path = path

    def save(self, entry):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(SCHEMA)
            db.execute('INSERT INTO runs (id, updated, details) VALUES (?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, details=excluded.details',
                       (entry['id'], int(time.time() * 1000), json.dumps(entry, ensure_ascii=False)))

    def page(self, before=None):
        if not self.path.exists():
            return {'runs': [], 'before': None}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(SCHEMA)
            rows = db.execute('SELECT seq, updated, details FROM runs '
                              'WHERE (? IS NULL OR seq < ?) ORDER BY seq DESC LIMIT 31', (before, before)).fetchall()
        runs = [dict(json.loads(details), updatedAt=updated) for _, updated, details in rows[:30]]
        return {'runs': runs, 'before': rows[29][0] if len(rows) == 31 else None}
