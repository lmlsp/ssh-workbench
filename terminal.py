"""SSH terminals and ordered runs. The server owns the PTY even when the browser leaves."""

import base64
from collections import deque
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
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

from settings import DATA, ROOT, LOCAL_TERMINAL_ENABLED
from catalog import USER, normalize_host, valid_host
from ssh_login import PasswordTicket

BOOT = base64.b64encode((ROOT / 'shell-integration.sh').read_bytes()).decode()
ESC = b'\x1b]777;OW:'
DISPATCH = b'\x18\x07'
CANCEL_DISPATCH = b'\x18\x0b'
ACTIVE = {'connecting', 'switching', 'dispatch', 'running', 'between', 'waiting', 'paused', 'stopping'}


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


def routed_endpoint(root, route):
    if not route:
        return root
    parent = sha256(json.dumps([root['id'], normalize_host(root['host']), root['port'], root['user']]).encode()).hexdigest()
    via = [root['name']]
    for hop in route.split('.'):
        host, port, user, end = base64.b64decode(hop, validate=True).decode().split('\0')
        port = int(port)
        if end or not valid_host(host) or not 1 <= port <= 65535 or not USER.fullmatch(user):
            raise ValueError('Invalid SSH endpoint')
        host = normalize_host(host)
        identifier = sha256(json.dumps([parent, host, port, user]).encode()).hexdigest()
        endpoint = {'id': identifier, 'kind': 'nested', 'name': host[:60], 'host': host, 'port': port,
                    'user': user, 'route': parent, 'via': ' → '.join(via)}
        parent = identifier
        via.append(host)
    return endpoint


class Terminal:
    def __init__(self, connection, on_close=None, on_event=None, login_password=None, ssh_password=None, on_run=None):
        local = connection.get('kind') == 'local'
        if local and not LOCAL_TERMINAL_ENABLED:
            raise ValueError('本机终端尚未启用')
        self.id = secrets.token_urlsafe(16)
        self.nonce = secrets.token_hex(18)
        self.connection = dict(connection)
        self.endpoint = self.connection
        self.ssh_password = ssh_password
        self.on_close = on_close
        self.on_event = on_event
        self.on_run = on_run
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
        self.wait_timer = None
        self.pending = b''
        self.prompt = b''
        self.signature = ESC + self.nonce.encode() + b':'
        self.interaction = None
        self.waiting_for_input = ''
        self.login_ticket = None
        self.fd, slave = os.openpty()
        shell = 'exec /bin/bash --rcfile <(printf %s "$OW_BOOT" | base64 -d) -i'
        launcher = f'OW_ROUTE= OW_HOP_HOST= OW_BOOT={BOOT} OW_NONCE={self.nonce} exec /bin/bash -c {shlex.quote(shell)}'
        try:
            self.resize(120, 32)
            cwd = None
            env = {**os.environ, 'TERM':'xterm-256color'}
            if local:
                account = pwd.getpwuid(os.geteuid())
                cwd = account.pw_dir
                env = {name:os.environ[name] for name in ('PATH','LANG','LC_ALL','LC_CTYPE','TZ') if name in os.environ}
                env.update(HOME=cwd, USER=account.pw_name, LOGNAME=account.pw_name, SHELL='/bin/bash', TERM='xterm-256color')
                args = [sys.executable, str(ROOT/'pty-child.py'), '/bin/bash', '-c']
            else:
                args = ssh_args(connection, True)
                if login_password:
                    self.login_ticket = PasswordTicket(connection['user'], login_password)
                    # Without a controlling TTY, even older OpenSSH invokes SSH_ASKPASS for authentication.
                    env.update(DISPLAY=':0', SSH_ASKPASS=str(ROOT/'ssh-askpass.py'), SSH_ASKPASS_REQUIRE='force',
                               WORKBENCH_SSH_PTY=os.ttyname(slave), WORKBENCH_SSH_USER=connection['user'],
                               WORKBENCH_SSH_TICKET=self.login_ticket.path)
                    args[2:2] = ['-o','ForwardX11=no','-o','NumberOfPasswordPrompts=1']
                else:
                    args = [sys.executable, str(ROOT/'pty-child.py'), *args]
            self.process = subprocess.Popen([*args, launcher],
                                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                            env=env, cwd=cwd)
        except BaseException:
            os.close(self.fd)
            if self.login_ticket:
                self.login_ticket.close()
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self.read, daemon=True, name=f'terminal-{self.id[:8]}').start()

    def resize(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        if hasattr(self, 'process'):
            self.process.send_signal(signal.SIGWINCH)

    def write(self, data):
        if self.closed:
            raise ValueError('终端连接已关闭')
        total = 0
        while total < len(data):
            total += os.write(self.fd, data[total:])

    def changed(self):
        self.revision += 1
        self.condition.notify_all()
        if self.on_event:
            self.on_event()

    def run_changed(self):
        if self.on_run:
            run = self.run
            self.on_run({
                **{key: run[key] for key in ('id', 'kind', 'name', 'phase', 'message', 'user', 'startedAt', 'target')},
                'steps': [{key: step[key] for key in ('name', 'group', 'directory', 'status', 'exitCode', 'attempts', 'waitSeconds')
                           if key in step} for step in run['steps']],
            })
        self.changed()

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
        if self.interaction:
            self.respond_to_prompt()

    def prepare_input(self, kind, password=None, **details):
        self.interaction = {'kind': kind, 'response': password, **details}
        self.waiting_for_input = ''
        self.prompt = b''
        self.changed()

    def respond_to_prompt(self):
        interaction = self.interaction
        plain = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', self.prompt)
        line = re.split(rb'[\r\n]', plain)[-1].decode(errors='replace').strip()
        kind = interaction['kind']
        label = '等待输入密码'
        if kind == 'ssh':
            match = re.fullmatch(re.escape(interaction['login']) + r"@([^\s]+)'s password:", line)
            if not match or normalize_host(match[1].strip('[]')) != interaction['host']:
                if line.endswith('Are you sure you want to continue connecting (yes/no/[fingerprint])?'):
                    self.waiting_for_input = '等待确认 SSH 主机指纹'
                    self.changed()
                return
        elif kind == 'su':
            if line.lower() not in ('password:', '密码:', '密码：'):
                return
        else:
            if line != interaction['prompt']:
                return
            label = '等待手动输入'
        self.prompt = b''
        password = interaction.pop('response', None)
        if password is not None:
            self.waiting_for_input = ''
            self.write(password.encode() + b'\r')
        else:
            self.waiting_for_input = label
        self.changed()

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
                self.cancel_wait()
                if self.run and self.run['phase'] in ACTIVE:
                    if self.run['index'] < len(self.run['steps']):
                        step = self.run['steps'][self.run['index']]
                        if step['status'] == 'running':
                            step['status'] = 'needs-review'
                    self.run['phase'] = 'disconnected'
                    self.run['password'] = None
                    self.run['message'] = 'SSH 已断开，当前步骤结果需在服务器核对。'
                    self.run_changed()
                else:
                    self.changed()
            if self.login_ticket:
                self.login_ticket.close()
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

    def remember(self, endpoint, user, directory, status, script, switch_to=None):
        if script.strip() and len(script) <= 32000:
            entry = {'id': secrets.token_urlsafe(9), 'user': user, 'directory': directory,
                     'exitCode': status, 'script': script, 'connection': endpoint['id']}
            if switch_to:
                entry['switchTo'] = switch_to
            self.history.appendleft(entry)
            self.changed()

    def marker(self, fields):
        if len(fields) < 2:
            return
        route, event, *values = fields
        endpoint = routed_endpoint(self.connection, route)
        current_run = self.run if self.run and self.run['connection'] == endpoint['id'] else None
        if event == 'N' and len(values) == 5 and endpoint['id'] == self.endpoint['id']:
            self.prepare_ssh(route, current_run, *values)
        elif event == 'U' and len(values) == 1 and endpoint['id'] == self.endpoint['id']:
            password = None
            if (current_run and current_run['phase'] == 'switching' and not current_run['takeover']
                    and values[0] == current_run['switchingTo']):
                password = current_run.get('password')
            self.prepare_input('su', password)
        elif event == 'B' and current_run and current_run['phase'] == 'running' and values == [current_run['ticket']]:
            step = current_run['steps'][current_run['index']]
            if step.get('followup'):
                response = None if current_run['takeover'] else step.get('response')
                self.prepare_input('custom', response, prompt=step['followup']['prompt'])
        elif event == 'H' and len(values) == 4:
            user, directory, status, script = values
            self.remember(endpoint, user, directory, int(status), script)
        elif event == 'S' and len(values) == 4:
            user, directory, script, target = values
            self.remember(endpoint, user, directory, 0, script, target)
            if current_run and current_run['phase'] == 'switching' and current_run['switchingTo'] == target:
                current_run['switchAcknowledged'] = True
        elif event == 'P' and len(values) == 2:
            if self.login_ticket:
                self.login_ticket.close()
                self.login_ticket = None
            connected = self.completed_ssh_login(route, endpoint, values[0])
            if self.run and self.run['phase'] == 'exited':
                self.run.pop('manualUntilPrompt', None)
            self.interaction = None
            self.waiting_for_input = ''
            self.endpoint = endpoint
            self.context = {'user': values[0], 'directory': values[1]}
            self.ready = True
            self.changed()
            if current_run and current_run['phase'] == 'stopping':
                self.finish_dispatch_cancel()
            elif connected and self.run and self.run['phase'] == 'running' and self.run['index'] == len(self.run['steps']) - 1:
                self.run['steps'][self.run['index']].update(status='succeeded', exitCode=0)
                self.run['index'] += 1
                self.run['ticket'] = None
                self.succeed(f"已连接到 {endpoint['host']}")
            elif not current_run and self.run and self.run['phase'] in ACTIVE:
                self.pause('已进入另一台服务器。回到原连接后可重试或继续。')
            elif current_run and current_run['phase'] == 'running':
                current_run['steps'][current_run['index']]['status'] = 'needs-review'
                self.pause('当前命令已中断，处理完后可重试或继续。')
            elif current_run and current_run['phase'] == 'switching':
                self.finish_switch(values[0])
            else:
                self.advance()
        elif event == 'D':
            if current_run and current_run['phase'] == 'stopping' and not current_run['payloadSent']:
                self.write(b'0\n')
            elif current_run and current_run['phase'] == 'dispatch' and not current_run['payloadSent']:
                current = self.run['steps'][self.run['index']]
                payload = pack([self.run['ticket'], current['directory'], current['script']])
                chunks = [payload[i:i + 1400] for i in range(0, len(payload), 1400)]
                self.run['payloadSent'] = True
                self.write(str(len(chunks)).encode() + b'\n' + b'\n'.join(chunks) + b'\n')
        elif event == 'A' and current_run and values == [current_run['ticket']] and current_run['payloadSent']:
            if current_run['phase'] == 'stopping':
                self.write(CANCEL_DISPATCH)
                return
            if current_run['phase'] != 'dispatch':
                return
            current = self.run['steps'][self.run['index']]
            self.run['phase'] = 'running'
            self.run['message'] = f"正在执行 {current['name']}"
            current['status'] = 'running'
            current['attempts'] = current.get('attempts', 0) + 1
            self.write(b'\r')
            self.run_changed()
        elif event == 'I' and current_run and current_run['phase'] == 'running' and values == [current_run['ticket']]:
            current_run['steps'][current_run['index']].update(status='failed', exitCode=2)
            current_run['ticket'] = None
            self.write(b'\x03')
            self.pause('命令不完整，请检查引号、括号或续行符。')
        elif event == 'X' and len(values) == 1 and current_run and current_run['phase'] == 'stopping':
            self.finish_dispatch_cancel()
        elif event == 'X' and len(values) == 1 and current_run and current_run['phase'] == 'dispatch':
            self.run['steps'][self.run['index']]['status'] = 'failed'
            self.run['ticket'] = None
            self.pause(values[0])
        elif event == 'R' and len(values) == 2 and current_run and current_run['ticket'] == values[0]:
            if current_run['phase'] == 'stopping':
                return
            current = self.run['steps'][self.run['index']]
            status = int(values[1])
            interrupted = self.run.pop('interruptRequested', False)
            self.run['ticket'] = None
            current['exitCode'] = status
            current['status'] = 'interrupted' if interrupted else 'succeeded' if status == 0 else 'failed'
            if interrupted:
                self.pause('当前步骤已中断，可以重试或确认本步已手动完成。')
            elif status:
                self.pause(f"{current['name']} 返回 {status}，可以在终端处理。")
            else:
                self.finish_step()

    def prepare_ssh(self, route, run, destination, port, account, login, host):
        port = int(port)
        if not valid_host(destination) or not valid_host(host.strip('[]')) or not USER.fullmatch(login) or not 1 <= port <= 65535:
            return
        destination = normalize_host(destination)
        host = normalize_host(host.strip('[]'))
        step = run['steps'][run['index']] if run and run['phase'] == 'running' else {}
        planned = step.get('sshLogin')
        managed = bool(planned and planned[0] == destination and planned[2] == account)
        response = None
        if destination == host and not (run and (run['takeover'] or run.get('manualUntilPrompt'))):
            if managed and planned[1] == port:
                response = step.get('response')
            if response is None and self.ssh_password and account == login and self.context and (managed or not run or run['phase'] not in ACTIVE):
                response = self.ssh_password(self.endpoint['id'], self.context['user'], destination, port, login)
        self.prepare_input('ssh', response, host=host, port=port, login=login, destination=destination, route=route, managed=managed)

    def completed_ssh_login(self, route, endpoint, user):
        interaction = self.interaction
        return bool(interaction and interaction['kind'] == 'ssh' and interaction['managed']
                    and route and route.rpartition('.')[0] == interaction['route']
                    and (endpoint['host'], endpoint['port'], user) ==
                        (interaction['destination'], interaction['port'], interaction['login']))

    def begin(self, name, steps, password=None, kind='command'):
        with self.condition:
            self.run = {'name': name, 'user': steps[0]['user'], 'steps': steps, 'index': 0,
                        'phase': 'connecting', 'message': '正在连接服务器', 'password': password,
                        'id': secrets.token_urlsafe(12), 'finishedAt': None,
                        'kind': kind, 'startedAt': int(time.time() * 1000),
                        'target': {key: self.endpoint[key] for key in ('name', 'host', 'port', 'via') if key in self.endpoint},
                        'connection': self.endpoint['id'],
                        'takeover': False, 'ticket': None, 'waitRemaining': 0, 'waitDeadline': None,
                        'switchTo': steps[0].get('switchTo') if len(steps) == 1 else None}
            self.run_changed()
            self.advance()

    def request_switch(self, user, script):
        run = self.run
        self.ready = False
        self.prompt = b''
        run['phase'] = 'switching'
        run['switchingTo'] = user
        run['switchAcknowledged'] = False
        run['message'] = f'正在切换到 {user}'
        self.run_changed()
        self.write((script + '\r').encode())

    def finish_switch(self, user):
        run = self.run
        if user != run['switchingTo'] or (run['switchTo'] == run['user'] and not run['switchAcknowledged']):
            if run['switchTo']:
                run['steps'][run['index']]['status'] = 'failed'
                self.pause('未能切换到所需用户。请处理终端中的提示，或重试。')
            else:
                run['password'] = None
                self.pause(f"请在终端切换到 {run['user']}，再继续。", before_step=True)
            return
        run['password'] = None
        if run['switchTo']:
            run['steps'][run['index']].update(status='succeeded', exitCode=0)
            self.finish_step()
        else:
            run['phase'] = 'between'
        self.advance()

    def finish_step(self):
        run = self.run
        step = run['steps'][run['index']]
        run['index'] += 1
        run['waitRemaining'] = step.get('waitSeconds', 0) if run['index'] < len(run['steps']) else 0
        run['phase'] = 'between'
        self.run_changed()

    def advance(self):
        run = self.run
        if not run or not self.ready or run['connection'] != self.endpoint['id'] or run['phase'] not in ('connecting', 'between'):
            return
        if run['index'] >= len(run['steps']):
            self.succeed(f"已切换到 {run['switchTo']}" if run['switchTo'] else '流程完成')
            return
        if run['takeover']:
            self.pause('已暂停后续步骤，可以在终端输入。', before_step=True)
            return
        if self.context['user'] != run['user']:
            if run['switchTo'] or not run.get('password'):
                self.pause(f"请在终端切换到 {run['user']}，再继续。", before_step=True)
                return
            self.request_switch(run['user'], f"su - {shlex.quote(run['user'])}")
            return
        if run['waitRemaining']:
            self.wait_before_next_step()
            return
        if run['switchTo']:
            step = run['steps'][run['index']]
            step['status'] = 'running'
            step['attempts'] = step.get('attempts', 0) + 1
            self.request_switch(run['switchTo'], step['script'].strip())
            return
        run['password'] = None
        self.ready = False
        run['phase'] = 'dispatch'
        run['ticket'] = secrets.token_hex(12)
        run['payloadSent'] = False
        run.pop('interruptRequested', None)
        run['message'] = f"准备执行 {run['steps'][run['index']]['name']}"
        self.run_changed()
        self.write(DISPATCH)

    def wait_before_next_step(self):
        run = self.run
        delay = run['waitRemaining']
        deadline = time.monotonic() + delay
        run.update(phase='waiting', waitDeadline=deadline, message='等待执行下一步')
        timer = threading.Timer(delay, self.wait_finished, (run, deadline))
        self.wait_timer = timer
        timer.daemon = True
        timer.start()
        self.run_changed()

    def cancel_wait(self):
        if self.wait_timer:
            self.wait_timer.cancel()
            self.wait_timer = None

    def wait_finished(self, run, deadline):
        with self.condition:
            if self.closed or self.run is not run or run['phase'] != 'waiting' or run['waitDeadline'] != deadline:
                return
            self.wait_timer = None
            run.update(phase='between', waitRemaining=0, waitDeadline=None)
            self.advance()

    def succeed(self, message):
        self.run.update(phase='succeeded', password=None, finishedAt=time.monotonic(), message=message)
        self.run_changed()

    def pause(self, message, before_step=False):
        run = self.run
        if run['phase'] == 'waiting':
            run['waitRemaining'] = max(0, run['waitDeadline'] - time.monotonic())
            run['waitDeadline'] = None
            self.cancel_wait()
        run.update(phase='paused', message=message, beforeStep=before_step)
        self.run_changed()

    def cancel_dispatch(self, action, message):
        # Keep the Readline handler alive long enough to cancel any unsubmitted command.
        self.run.update(phase='stopping', cancelAction=action, message=message)
        self.run_changed()

    def finish_dispatch_cancel(self):
        action = self.run['cancelAction']
        if action == 'pause':
            self.run['ticket'] = None
            self.pause('已暂停后续步骤，可以在终端输入。', before_step=True)
        elif action == 'leave':
            self.leave_run()
        else:
            self.end_run()

    def leave_run(self):
        self.cancel_wait()
        run = self.run
        if run['index'] < len(run['steps']):
            step = run['steps'][run['index']]
            if step['status'] == 'running':
                step['status'] = 'detached'
        if self.interaction:
            self.interaction.pop('response', None)
        run.update(phase='exited', ticket=None, password=None, message='已退出自动化',
                   manualUntilPrompt=run['phase'] in ('running', 'switching') and not self.ready)
        self.run_changed()

    def end_run(self):
        self.cancel_wait()
        run = self.run
        if run['index'] < len(run['steps']):
            step = run['steps'][run['index']]
            if step['status'] == 'running':
                step['status'] = 'interrupted'
        run.update(phase='ended', ticket=None, password=None, message='已结束流程')
        self.interaction = None
        self.waiting_for_input = ''
        self.run_changed()

    def input(self, text):
        with self.condition:
            phase = self.run['phase'] if self.run else None
            if phase == 'stopping':
                raise ValueError('流程正在切换状态，请稍等片刻')
            if phase == 'dispatch':
                raise ValueError('正在发送命令，请稍等片刻')
            if phase == 'running' and text == '\x03':
                self.run['interruptRequested'] = True
                if not self.run['takeover']:
                    self.pause_automation()
            if (phase in ('running', 'between', 'waiting') and not self.run['takeover'] and not self.waiting_for_input
                    and not (self.interaction and self.interaction['kind'] == 'ssh')):
                raise ValueError('请先暂停后续步骤再输入')
            if text and self.ready:
                self.ready = False
                self.changed()
            self.write(text.encode())
            if self.waiting_for_input and ('\r' in text or '\n' in text):
                self.waiting_for_input = ''
                self.changed()

    def pause_automation(self):
        run = self.run
        if run['phase'] == 'paused':
            return
        run['takeover'] = True
        if self.interaction:
            self.interaction.pop('response', None)
        if run['phase'] == 'dispatch':
            self.cancel_dispatch('pause', '正在暂停后续步骤')
        elif run['phase'] == 'waiting':
            self.pause('已暂停倒计时，可以在终端输入。', before_step=True)
        else:
            run['message'] = '已暂停后续。当前命令可以继续，可以在终端输入。'
            self.run_changed()

    def require_prompt_for(self, user):
        if not self.ready or not self.context:
            raise ValueError('请等当前命令返回到终端提示符')
        if self.run['connection'] != self.endpoint['id']:
            raise ValueError('请先回到开始流程时的 SSH 连接')
        if self.context['user'] != user:
            raise ValueError(f'请先在终端切换到 {user}')

    def resume_automation(self):
        run = self.run
        if run['phase'] == 'paused':
            if not run.get('beforeStep'):
                raise ValueError('请重试本步或确认本步已手动完成')
            self.require_prompt_for(run['user'])
            run.update(phase='between', takeover=False)
            self.advance()
        elif run['takeover']:
            run.update(takeover=False, message='已恢复自动化，等待当前步骤结束。')
            self.run_changed()
            self.advance()
        else:
            raise ValueError('当前流程未暂停')

    def control(self, action):
        with self.condition:
            if not self.run or self.run['phase'] not in ACTIVE:
                raise ValueError('没有可操作的运行任务')
            run = self.run
            if run['phase'] == 'stopping':
                raise ValueError('流程正在切换状态，请稍等片刻')
            if action in ('takeover', 'pause'):
                self.pause_automation()
            elif action == 'resume':
                self.resume_automation()
            elif action == 'leave':
                if run['phase'] == 'dispatch':
                    self.cancel_dispatch(action, '正在退出自动化')
                else:
                    self.leave_run()
            elif action == 'end':
                if run['phase'] == 'dispatch':
                    self.cancel_dispatch(action, '正在结束流程')
                    return
                if run['connection'] == self.endpoint['id'] and (run['phase'] == 'running' or not self.ready):
                    self.write(b'\x03')
                self.end_run()
            elif action in ('retry', 'next'):
                if run['phase'] != 'paused' or run.get('beforeStep'):
                    raise ValueError('当前没有需要处理的步骤')
                expected = run['switchTo'] if action == 'next' and run['switchTo'] else run['user']
                self.require_prompt_for(expected)
                step = run['steps'][run['index']]
                run['takeover'] = False
                if action == 'next':
                    if step['status'] != 'succeeded':
                        step['status'] = 'manual'
                    self.finish_step()
                else:
                    step['status'] = 'pending'
                    step.pop('exitCode', None)
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
                run = {key: source[key] for key in ('id', 'phase', 'name', 'index', 'message', 'takeover', 'user', 'switchTo', 'connection')}
                if source['phase'] == 'paused':
                    run['beforeStep'] = source.get('beforeStep', False)
                if source['phase'] == 'waiting':
                    run['waitMs'] = max(0, round((source['waitDeadline']-time.monotonic()) * 1000))
                elif source['phase'] == 'paused' and source['waitRemaining']:
                    run['waitMs'] = round(source['waitRemaining'] * 1000)
                if source['finishedAt'] is not None:
                    run['completionAgeMs'] = int((time.monotonic() - source['finishedAt']) * 1000)
                run['steps'] = [{key: step[key] for key in ('name', 'status')} for step in source['steps']]
            history = [entry for entry in self.history if entry['connection'] == self.endpoint['id']]
            return {'id': self.id, 'connection': self.connection['id'], 'endpoint': self.endpoint, 'revision': self.revision,
                    'reset': after < self.floor, 'text': base64.b64encode(content).decode(), 'ready': self.ready,
                    'closed': self.closed, 'context': self.context, 'history': history, 'run': run,
                    'inputNotice': '' if self.closed else self.waiting_for_input}

    def close(self):
        with self.condition:
            if not self.closed:
                try:
                    os.killpg(self.process.pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
