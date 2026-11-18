#!/usr/bin/env python3
"""Read only the configured application logs over an authenticated terminal."""

import os
from pathlib import Path
import re
import stat


SECRET = re.compile(r'(?i)(\b(?:password|passwd|pwd|token|secret|client[_-]?secret|api[_-]?key|access[_-]?token|authorization)\b["\']?\s*[:=]\s*)(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;&}]+)')
BEARER = re.compile(r'(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]+')
MAX_READ = 48 * 1024


def safe(line):
    line = BEARER.sub('Bearer [REDACTED]', line)
    line = SECRET.sub(r'\1[REDACTED]', line)
    return re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', line)


def read_one(paths, item):
    code = item['source']
    path = Path(paths[code])
    if path.resolve(strict=True) != path:
        raise ValueError('日志路径已变化')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('日志不是普通文件')
        identity = f'{info.st_dev}:{info.st_ino}'
        cursor = item.get('cursor') or {}
        if not isinstance(cursor, dict):
            raise ValueError('无效的日志位置')
        offset = cursor.get('offset')
        incremental = cursor.get('file') == identity and type(offset) is int and 0 <= offset <= info.st_size
        start = offset if incremental else max(0, info.st_size - MAX_READ)
        stream.seek(start)
        data = stream.read(MAX_READ)
        if not data:
            return {'source': code, 'cursor': {'file': identity, 'offset': start}, 'lines': []}
        last_line = data.rfind(b'\n') + 1
        consumed = last_line or (len(data) if len(data) == MAX_READ else 0)
        text = data[:consumed].decode('utf-8', errors='replace')
        lines = (text[:-1] if text.endswith('\n') else text).split('\n') if text else []
        if start and not incremental:
            lines = lines[1:]
        return {'source': code, 'cursor': {'file': identity, 'offset': start + consumed},
                'lines': [safe(line[:-1] if line.endswith('\r') else line) for line in (lines if incremental else lines[-140:])]}


def read_many(paths, requests):
    results = []
    for item in requests:
        try:
            results.append(read_one(paths, item))
        except (OSError, KeyError, TypeError, ValueError):
            results.append({'source': item.get('source') if isinstance(item, dict) else None, 'error': '日志暂时无法读取'})
    return results
