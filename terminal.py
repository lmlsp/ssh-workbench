"""SSH terminals and ordered runs. The server owns the PTY even when the browser leaves."""

import base64
from collections import deque
import errno
import fcntl
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

from settings import DATA, ROOT

BOOT = base64.b64encode((ROOT / 'shell-integration.sh').read_bytes()).decode()
ESC = b'\x1b]777;OW:'
DISPATCH = b'\x18\x07'
ACTIVE = {'connecting', 'switching', 'dispatch', 'running', 'between', 'paused'}


def ssh_args(connection, interactive):
    # OpenSSH uses its own double-quoted configuration syntax for paths containing spaces.
    known_hosts = [DATA / 'known_hosts', Path.home() / '.ssh/known_hosts', Path.home() / '.ssh/known_hosts2']
    known_hosts_option = 'UserKnownHostsFile=' + ' '.join('"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"' for path in known_hosts)
    return ['ssh', '-tt' if interactive else '-T', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=25',
            '-o', 'ServerAliveCountMax=3', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
            '-o', known_hosts_option,
            '-o', 'StrictHostKeyChecking=' + ('ask' if interactive else 'yes'),
            '-p', str(connection['port']), '-l', connection['user'], '--', connection['host']]


def pack(fields):
    return base64.b64encode(('\0'.join(fields) + '\0').encode())


class Terminal:
    def __init__(self, connection, on_close=None):
        self.id = secrets.token_urlsafe(16)
        self.nonce = secrets.token_hex(18)
        self.connection = dict(connection)
        self.on_close = on_close
        self.condition = threading.Condition(threading.RLock())
        self.output = deque()
        self.size = 0
        self.revision = 0
        self.floor = 0
        self.history = deque(maxlen=80)
        self.context = None
        self.ready = False
        self.closed = False
        self.busy_since = time.monotonic()
        self.run = None
        self.pending = b''
        self.prompt = b''
        self.signature = ESC + self.nonce.encode() + b':'
        self.secret_consumed = False
        self.fd, slave = os.openpty()
        remote = 'exec /bin/bash --rcfile <(printf %s "$OW_BOOT" | base64 -d) -i'
        launcher = f'OW_BOOT={BOOT} OW_NONCE={self.nonce} exec /bin/bash -c {shlex.quote(remote)}'
        try:
            self.resize(120, 32)
            self.process = subprocess.Popen([sys.executable, str(ROOT / 'pty-child.py'), *ssh_args(connection, True), launcher],
                                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                            env={**os.environ, 'TERM': 'xterm-256color'})
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self.read, daemon=True, name=f'terminal-{self.id[:8]}').start()

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def write(self, data):
        if self.closed:
            raise ValueError('终端连接已关闭')
        total = 0
        while total < len(data):
            total += os.write(self.fd, data[total:])

    def changed(self):
        self.revision += 1
        self.condition.notify_all()

    def visible(self, chunk):
        if not chunk:
            return
        self.changed()
        self.output.append((self.revision, chunk))
        self.size += len(chunk)
        while self.size > 3 * 1024 * 1024 and self.output:
            self.floor, old = self.output.popleft()
            self.size -= len(old)
        self.prompt = (self.prompt + chunk)[-400:]
        if self.run and self.run['phase'] == 'switching' and not self.secret_consumed:
            if re.search(rb'(?:^|[\r\n])(?:Password:|password:|\xe5\xaf\x86\xe7\xa0\x81[:\xef\xbc\x9a]+)\s*$', self.prompt):
                secret = self.run.get('password')
                self.secret_consumed = True
                if secret:
                    self.write(secret.encode() + b'\r')

    def read(self):
        try:
            while True:
                chunk = os.read(self.fd, 16384)
                if not chunk:
                    break
                with self.condition:
                    self.feed(chunk)
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                with self.condition:
                    self.visible(b'\r\n[Connection error]\r\n')
        finally:
            with self.condition:
                self.closed = True
                self.ready = False
                if self.run and self.run['phase'] in ACTIVE:
                    self.run['phase'] = 'disconnected'
                    self.run['password'] = None
                    self.run['message'] = 'SSH 已断开，当前步骤结果需在服务器核对。'
                self.changed()
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.process.wait()
            if self.on_close:
                self.on_close(self)

    def feed(self, chunk):
        data = self.pending + chunk
        self.pending = b''
        while data:
            start = data.find(self.signature)
            if start == -1:
                tail = min(len(data), len(self.signature) - 1)
                while tail and not self.signature.startswith(data[-tail:]):
                    tail -= 1
                self.pending = data[-tail:] if tail else b''
                self.visible(data[:-tail] if tail else data)
                return
            self.visible(data[:start])
            end = data.find(b'\x07', start + len(self.signature))
            if end == -1 and len(data) - start < 82400:
                self.pending = data[start:]
                return
            if end == -1:
                return
            try:
                marker = base64.b64decode(data[start + len(self.signature):end], validate=True).decode().split('\0')[:-1]
                self.marker(marker)
            except (ValueError, IndexError):
                pass
            data = data[end + 1:]

    def marker(self, fields):
        if not fields:
            return
        event, *values = fields
        if event == 'H' and len(values) == 4:
            user, directory, status, script = values
            if script.strip() and len(script) <= 32000:
                self.history.appendleft({'id': secrets.token_urlsafe(9), 'user': user, 'directory': directory,
                                         'exitCode': int(status), 'script': script})
                self.changed()
        elif event == 'P' and len(values) == 2:
            self.context = {'user': values[0], 'directory': values[1]}
            self.ready = True
            self.changed()
            if self.run and self.run['phase'] == 'running':
                self.run['steps'][self.run['index']]['status'] = 'needs-review'
                self.pause('当前命令已中断，处理完后可重试或继续。')
            elif self.run and self.run['phase'] == 'switching' and values[0] != self.run['user']:
                self.pause('未能切换到所需用户。请在终端切换身份后继续。')
            else:
                self.advance()
        elif event == 'D':
            if self.run and self.run['phase'] == 'dispatch':
                current = self.run['steps'][self.run['index']]
                payload = pack([self.run['ticket'], current['directory'], current['script']])
                chunks = [payload[i:i + 1400] for i in range(0, len(payload), 1400)]
                self.run['phase'] = 'running'
                self.run['message'] = f"正在执行 {current['name']}"
                current['status'] = 'running'
                self.changed()
                self.write(str(len(chunks)).encode() + b'\n' + b'\n'.join(chunks) + b'\n')
        elif event == 'R' and len(values) == 2 and self.run and self.run['ticket'] == values[0]:
            current = self.run['steps'][self.run['index']]
            status = int(values[1])
            current['exitCode'] = status
            current['status'] = 'succeeded' if status == 0 else 'failed'
            if self.run['takeover'] or self.run['phase'] == 'paused':
                self.pause('当前步骤已返回，请选择重试或继续。')
            elif status:
                self.pause(f"{current['name']} 返回 {status}，可以在终端处理。")
            else:
                self.run['index'] += 1
                self.run['phase'] = 'between'
                self.changed()

    def begin(self, name, steps, password=None):
        with self.condition:
            self.run = {'name': name, 'user': steps[0]['user'], 'steps': steps, 'index': 0,
                        'phase': 'connecting', 'message': '正在连接服务器', 'password': password,
                        'takeover': False, 'ticket': None}
            self.changed()
            self.advance()

    def advance(self):
        run = self.run
        if not run or not self.ready or run['phase'] not in ('connecting', 'switching', 'between'):
            return
        if self.context['user'] != run['user']:
            if run['phase'] == 'switching':
                return
            if not run.get('password'):
                self.pause(f"请在终端切换到 {run['user']}，再继续。")
                return
            self.ready = False
            self.prompt = b''
            self.secret_consumed = False
            run['phase'] = 'switching'
            run['message'] = f"正在切换到 {run['user']}"
            self.changed()
            self.write(f"su - {shlex.quote(run['user'])}\r".encode())
            return
        run['password'] = None
        if run['index'] >= len(run['steps']):
            run['phase'] = 'succeeded'
            run['message'] = '流程完成'
            self.changed()
            return
        self.ready = False
        run['phase'] = 'dispatch'
        run['ticket'] = secrets.token_hex(12)
        run['message'] = f"准备执行 {run['steps'][run['index']]['name']}"
        self.changed()
        self.write(DISPATCH)

    def pause(self, message):
        self.run['phase'] = 'paused'
        self.run['message'] = message
        self.changed()

    def input(self, text):
        with self.condition:
            phase = self.run['phase'] if self.run else None
            if phase == 'dispatch':
                raise ValueError('正在发送命令，请稍等片刻')
            if phase in ('running', 'between') and not self.run['takeover']:
                raise ValueError('请先接管终端再输入')
            if text and self.ready:
                self.ready = False
                self.changed()
            self.write(text.encode())

    def control(self, action):
        with self.condition:
            if not self.run or self.run['phase'] not in ACTIVE:
                raise ValueError('没有可操作的运行任务')
            run = self.run
            if action == 'takeover':
                run['takeover'] = True
                if run['phase'] in ('between', 'connecting', 'switching'):
                    self.pause('已接管终端，处理完后可以继续。')
                else:
                    run['message'] = '已接管。当前命令仍在终端内。'
                    self.changed()
            elif action == 'end':
                if run['phase'] in ('running', 'dispatch') or not self.ready:
                    self.write(b'\x03')
                run['phase'] = 'ended'
                run['ticket'] = None
                run['password'] = None
                run['message'] = '已结束流程'
                self.changed()
            elif action in ('retry', 'next'):
                if run['phase'] != 'paused' or not self.ready or not self.context:
                    raise ValueError('请等当前命令返回到终端提示符')
                if self.context['user'] != run['user']:
                    raise ValueError(f"请先在终端切换到 {run['user']}")
                step = run['steps'][run['index']]
                if action == 'next':
                    if step['status'] != 'succeeded':
                        step['status'] = 'manual'
                    run['index'] += 1
                else:
                    step['status'] = 'pending'
                    step.pop('exitCode', None)
                run['takeover'] = False
                run['phase'] = 'between'
                self.advance()

    def events(self, after, wait):
        with self.condition:
            if wait and self.revision <= after and not self.closed:
                self.condition.wait_for(lambda: self.revision > after or self.closed, timeout=18)
            content = b''.join(chunk for seq, chunk in self.output if seq > after)
            run = None
            if self.run:
                source = self.run
                run = {key: source[key] for key in ('phase', 'name', 'index', 'message', 'takeover', 'user')}
                run['steps'] = [{key: step[key] for key in ('name', 'status')} for step in source['steps']]
            return {'id': self.id, 'connection': self.connection['id'], 'revision': self.revision,
                    'reset': after < self.floor, 'text': base64.b64encode(content).decode(), 'ready': self.ready,
                    'closed': self.closed, 'context': self.context, 'history': list(self.history), 'run': run}

    def close(self):
        with self.condition:
            if not self.closed:
                try:
                    os.killpg(self.process.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
