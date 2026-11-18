"""Application log reads use separate SSH connections from interactive terminals."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex
import subprocess

from catalog import match
from terminal import ssh_args


READER = Path(__file__).with_name('remote-logs.py').read_bytes()
POOL = ThreadPoolExecutor(max_workers=6)


def ssh_read(connection, items):
    paths = {row['slot']: row['path'] for row in items}
    requested = [{'source': row['slot'], 'cursor': row['cursor']} for row in items]
    code = READER + f'\nimport json\nprint(json.dumps(read_many({paths!r}, {requested!r}), ensure_ascii=True))\n'.encode()
    wrapper = 'import base64;exec(base64.b64decode(' + repr(base64.b64encode(code).decode()) + '))'
    args = ssh_args(connection, False)
    args[2:2] = ['-o', 'BatchMode=yes']
    task = subprocess.run([*args, '/usr/bin/python3 -c ' + shlex.quote(wrapper)], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=9, check=True)
    return json.loads(task.stdout)


def read_group(connection, items):
    try:
        results = ssh_read(connection, items)
    except (OSError, ValueError, subprocess.SubprocessError):
        results = [{'source': row['slot'], 'error': '这个 SSH 用户暂时无法读取日志'} for row in items]
    return {row['source']: row for row in results}


def follow(state, requested):
    if not isinstance(requested, list) or not 1 <= len(requested) <= 4:
        raise ValueError('每次可以查看 1 至 4 个日志来源')
    by_id = {source['id']: (app['connection'], source) for app in state['apps'] for source in app['sources']}
    groups, order = {}, []
    for position, item in enumerate(requested):
        if not isinstance(item, dict) or not isinstance(item.get('source'), str) or item['source'] not in by_id:
            raise ValueError('所选日志来源已不存在')
        key = item['source']
        cursor = item.get('cursor')
        if cursor is not None and (not isinstance(cursor, dict) or type(cursor.get('offset')) is not int
                                   or not isinstance(cursor.get('file'), str) or len(cursor['file']) > 90):
            raise ValueError('日志位置不正确')
        connection, config = by_id[key]
        groups.setdefault(connection, []).append({**config, 'slot': str(position), 'cursor': cursor})
        order.append(key)
    tasks = [POOL.submit(read_group, match(state, 'connections', connection), entries) for connection, entries in groups.items()]
    results = {key: row for task in tasks for key, row in task.result().items()}
    return [{**results.get(str(position), {'error': '日志暂不可读'}), 'source': key} for position, key in enumerate(order)]
