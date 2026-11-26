"""Relay bounded file frames between HTTP and an authenticated terminal."""

import base64
import hashlib
import json
import queue
import re
import secrets
import threading

CHUNK = 48 * 1024
WINDOW = CHUNK * 16
FRAME_TIMEOUT = 50
TRANSFER_MODES = {'upload', 'download'}
METADATA_MODES = {'list', 'mkdir', 'inspect'}


class FileConflict(ValueError):
    pass


def checked_request(data):
    mode = data.get('mode')
    directory = data.get('directory')
    if mode not in METADATA_MODES | TRANSFER_MODES:
        raise ValueError('未知文件操作')
    if not isinstance(directory, str) or not directory.startswith('/') or '\0' in directory or len(directory.encode()) > 8192:
        raise ValueError('请选择服务器上的绝对路径')
    request = {'mode': mode, 'directory': directory}
    if mode != 'list':
        name = data.get('name')
        if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\0' in name or len(name.encode()) > 255:
            raise ValueError('请输入单个文件或文件夹名称')
        request['name'] = name
    if mode == 'upload':
        size = data.get('size')
        if type(size) is not int or not 0 <= size <= 2**53 - 1:
            raise ValueError('文件长度不正确')
        overwrite = data.get('overwrite', False)
        version = data.get('version')
        if type(overwrite) is not bool or overwrite and (not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{64}', version)):
            raise ValueError('请重新确认是否覆盖同名文件')
        request.update(size=size, overwrite=overwrite, version=version)
    return request


class FileTransfer:
    def __init__(self, terminal, request):
        self.terminal = terminal
        self.request = request
        self.id = secrets.token_urlsafe(16)
        self.mode = request['mode']
        self.name = request.get('name', '')
        self.directory = request['directory']
        self.server = terminal.endpoint['name']
        self.user = terminal.context['user']
        self.size = request.get('size')
        self.transferred = 0
        self.started = False
        self.cancel_requested = False
        self.claimed = False
        self.result = None
        self.delivery_error = None
        self.released = threading.Event()
        self.lock = threading.RLock()
        self.inbox = queue.Queue()

    def public(self):
        with self.lock:
            result = self.result or {}
            status = result.get('status', 'running')
            if self.delivery_error and status != 'cancelled':
                status = 'failed'
            elif status == 'succeeded' and self.mode == 'download' and self.transferred < (self.size or 0):
                status = 'running'
            return {
                'id':self.id, 'terminal':self.terminal.id, 'direction':self.mode, 'server':self.server,
                'user':self.user, 'directory':self.directory, 'name':self.name, 'size':self.size,
                'transferred':self.transferred, 'status':status, 'error':self.delivery_error or result.get('error', ''),
                'released':self.released.is_set(),
            }

    def send(self, action, data=b''):
        # The comment prefix is harmless even if a connection drops mid-frame.
        frame = b'#' + action + base64.b64encode(data) + b'\n'
        with self.lock:
            if self.result or self.released.is_set():
                raise ValueError((self.result or {}).get('error', '文件操作已结束'))
            self.terminal.write(frame)

    def frame(self, kind, payload):
        if kind == 'ready':
            self.inbox.put((kind, None))
        elif kind == 'chunk':
            self.inbox.put((kind, payload))
        elif kind == 'progress':
            self.transferred = int(payload)
        elif kind in ('entry', 'meta'):
            self.inbox.put((kind, json.loads(payload)))
        elif kind == 'result':
            self.finish(json.loads(payload), release_worker=True)

    def finish(self, result, release_worker=False):
        with self.lock:
            if self.result is None:
                self.result = result
                self.inbox.put(('result', result))
            if release_worker and not self.released.is_set():
                try:
                    self.terminal.write(b'#E\n')
                except (OSError, ValueError):
                    pass

    def terminal_returned(self, error='文件助手已退出，未完成的文件操作已停止'):
        self.finish({'status':'failed', 'error':error})
        self.released.set()

    def receive(self):
        try:
            kind, data = self.inbox.get(timeout=FRAME_TIMEOUT)
        except queue.Empty as exc:
            raise ValueError('等待文件操作响应超时') from exc
        if kind == 'result' and data['status'] != 'succeeded':
            if data['status'] == 'conflict':
                raise FileConflict(data['error'])
            raise ValueError(data['error'])
        return kind, data

    def prepare(self):
        try:
            self.require('ready')
            with self.lock:
                self.started = True
                cancelled = self.cancel_requested
            if cancelled:
                self.cancel()
                self.require('result')
            self.send(b'Q', json.dumps(self.request, ensure_ascii=True).encode('ascii'))
            if self.mode in TRANSFER_MODES:
                meta = self.require('meta')
                with self.lock:
                    self.directory = meta['directory']
                    self.size = meta['size']
                return self.public()
            listing = {}
            entries = []
            while True:
                kind, data = self.receive()
                if kind == 'result':
                    if self.mode == 'list':
                        entries.sort(key=lambda entry:(entry['kind']!='directory', entry['name'].casefold()))
                        data = {**listing, 'entries':entries}
                    self.released.wait(3)
                    return data
                if kind == 'meta':
                    listing = data
                elif kind == 'entry':
                    entries.append(data)
        except (ValueError, OSError):
            self.cancel()
            self.released.wait(3)
            raise

    def require(self, expected):
        kind, data = self.receive()
        if kind != expected:
            raise ValueError('文件助手返回了意外响应')
        return data

    def claim(self, mode):
        with self.lock:
            if self.mode != mode or self.claimed or self.result or self.released.is_set():
                raise ValueError('这项传输已结束或已在进行中')
            self.claimed = True

    def cancel(self):
        with self.lock:
            if self.result is None and not self.released.is_set():
                self.cancel_requested = True
                if self.started:
                    try:
                        self.send(b'C')
                    except (OSError, ValueError):
                        pass

    def upload(self, source, size):
        if size != self.size:
            raise ValueError('上传文件的长度已变化')
        self.claim('upload')
        digest = hashlib.sha256()
        remaining = size
        try:
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError('上传提前结束，原文件已保留')
                remaining -= len(chunk)
                digest.update(chunk)
                self.send(b'D', chunk)
            self.send(b'H', digest.hexdigest().encode('ascii'))
            self.require('result')
        except (ValueError, OSError):
            self.cancel()
            self.released.wait(3)
            raise
        self.released.wait(3)

    def download(self, destination):
        try:
            received = 0
            while True:
                self.send(b'R')
                end = min(self.size, received + WINDOW)
                while received < end:
                    chunk = self.require('chunk')
                    if not 0 < len(chunk) <= min(CHUNK,end-received):
                        raise ValueError('文件助手返回的数据长度不正确')
                    received += len(chunk)
                    if received == self.size:
                        # Hold the last chunk until the source has been validated.
                        self.require('result')
                    destination.write(chunk)
                    self.transferred = received
                if received == self.size:
                    if not received:
                        self.require('result')
                    break
        except (ValueError, OSError) as exc:
            with self.lock:
                if not self.result or self.result['status'] != 'cancelled':
                    self.delivery_error = str(exc) if isinstance(exc, ValueError) else '下载连接中断'
            self.cancel()
            raise
