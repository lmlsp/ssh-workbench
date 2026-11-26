"""Local SSH workbench HTTP service."""

import argparse
from getpass import getpass
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from catalog import Store, match, server_id
from file_transfer import FileConflict, TRANSFER_MODES, checked_request
from auth import AUTH, set_password, verify
from multi_logs import follow
from settings import DATA, PORT, ROOT, setup_data
from run_log import RunLog
from terminal import ACTIVE, Terminal


BIND_ADDRESS = os.environ.get('WORKBENCH_BIND_ADDRESS', '127.0.0.1')
TLS_CERT = os.environ.get('WORKBENCH_TLS_CERT', '')
TLS_KEY = os.environ.get('WORKBENCH_TLS_KEY', '')
ALLOWED_NETWORKS = os.environ.get('WORKBENCH_ALLOWED_NETWORKS', '')
ALLOW_HTTP = os.environ.get('WORKBENCH_ALLOW_HTTP') == '1'
# A previous HTTPS Secure cookie must not shadow an HTTP session after a deployment switches transport.
COOKIE_NAME = 'ssh_workbench_lan_http' if ALLOW_HTTP and not TLS_CERT else 'ssh_workbench'
HOST = f'{BIND_ADDRESS}:{PORT}'
ORIGIN = ('https://' if TLS_CERT else 'http://') + HOST
STORE = Store()
RUN_LOG = RunLog(DATA / 'run-log.sqlite3')
SESSIONS = {}
TERM_LOCK = threading.RLock()
TERMS = {}
FILE_JOBS = {}
FILE_LOCK = threading.Lock()
FILE_ROUTE = re.compile(r'/api/files/([A-Za-z0-9_-]{22})/(upload|download|status|cancel)')
MAX_TERMINALS = 12
TERMINAL_EVENTS = threading.Condition()
TERMINAL_REVISION = 0
ASSETS = {
    '/': ('ssh.html', 'text/html; charset=utf-8'),
    '/ssh.js': ('ssh.js', 'text/javascript; charset=utf-8'),
    '/terminal-ui.js': ('terminal-ui.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/ssh.css': ('ssh.css', 'text/css; charset=utf-8'),
    '/vendor/xterm.js': ('node_modules/@xterm/xterm/lib/xterm.js', 'text/javascript; charset=utf-8'),
    '/vendor/xterm.css': ('node_modules/@xterm/xterm/css/xterm.css', 'text/css; charset=utf-8'),
    '/vendor/fit.js': ('node_modules/@xterm/addon-fit/lib/addon-fit.js', 'text/javascript; charset=utf-8'),
    '/vendor/workspace.js': ('dist/workspace.js', 'text/javascript; charset=utf-8'),
    '/vendor/workspace.css': ('dist/workspace.css', 'text/css; charset=utf-8'),
}


def file_request(data):
    request = checked_request(data)
    with TERM_LOCK:
        term = TERMS.get(data.get('terminal'))
    if not term:
        raise ValueError('终端已不存在')
    with term.condition:
        job = term.start_files(data, request)
        if job.mode in TRANSFER_MODES:
            with FILE_LOCK:
                completed = [key for key, item in FILE_JOBS.items() if item.released.is_set()]
                for key in completed[:-32]:
                    del FILE_JOBS[key]
                FILE_JOBS[job.id] = job
    return job.prepare()


def terminal_changed():
    global TERMINAL_REVISION
    with TERMINAL_EVENTS:
        TERMINAL_REVISION += 1
        TERMINAL_EVENTS.notify_all()


def record_run(entry):
    try:
        RUN_LOG.save(entry)
    except (OSError, sqlite3.Error):
        logging.exception('Could not save workbench run results')


def recent_runs(before):
    with TERM_LOCK:
        result = RUN_LOG.page(before)
        live_ids = {term.run['id'] for term in TERMS.values() if term.run}
    for entry in result['runs']:
        if entry['phase'] in ACTIVE and entry['id'] not in live_ids:
            entry.update(phase='disconnected', message='工作台会话已结束，请在服务器核对步骤结果。')
            for step in entry['steps']:
                if step['status'] == 'running':
                    step['status'] = 'needs-review'
    return result


def open_terminal(data):
    with TERM_LOCK:
        connection, password = STORE.login_connection(data.get('connection'))
        if connection.get('kind') == 'nested':
            raise ValueError('请从原终端内执行 SSH 进入这台服务器')
        if sum(not t.closed for t in TERMS.values()) >= MAX_TERMINALS:
            raise ValueError(f'终端已达到 {MAX_TERMINALS} 个，请关掉不用的连接')
        remaining = [t for t in TERMS.values() if t.closed]
        for old in remaining[:-12]:
            TERMS.pop(old.id, None)
        term = Terminal(connection, on_event=terminal_changed, login_password=password, ssh_password=STORE.ssh_password,
                        on_run=record_run)
        TERMS[term.id] = term
        return {'id': term.id}


def execute_on_terminal(data):
    with TERM_LOCK:
        term = TERMS.get(data.get('terminal'))
        if not term or term.closed:
            raise ValueError('请先选中一个已连接的 SSH 终端')
        kind = data.get('kind')
        if kind not in ('command', 'flow'):
            raise ValueError('请选择快捷指令或流程')
        name, connection, steps, password = STORE.plan(kind, data.get('id'), data.get('params') or {},
                                                       data.get('groups'), data.get('groupParams'), data.get('revision'),
                                                       data.get('stepWaits'))
        with term.condition:
            if term.closed:
                raise ValueError('选中的 SSH 终端已断开')
            if any(term.endpoint[key] != connection[key] for key in ('id', 'host', 'port', 'user')):
                raise ValueError(f"请选择「{connection['name']}」的终端执行。连接配置已改动时，请重新连接。")
            replacement = (term.ready and term.run and term.run['phase'] == 'paused'
                           and term.run['connection'] == connection['id'] and term.run['id'] == data.get('replaceRun'))
            busy = next((t.run for t in TERMS.values() if not t.closed and t.run and t.run['phase'] in ACTIVE
                         and (t is term or t.run['connection'] == connection['id'])
                         and not (t is term and replacement)), None)
            if busy:
                step = busy['steps'][min(busy['index'], len(busy['steps']) - 1)]['name']
                raise ValueError(f"「{busy['name']}」正在处理「{step}」。请等步骤返回或先退出自动化。")
            if term.context is None:
                raise ValueError('请先完成选中终端的 SSH 登录')
            if not term.ready:
                raise ValueError('选中的终端正在执行命令或等待输入，请等命令提示符出现后再执行')
            if replacement:
                term.control('leave')
            term.begin(name, steps, password, kind)
            return {'id': term.id}


def save_operation(collection, data):
    if collection != 'commands' or 'terminal' not in data:
        return STORE.save(collection, data)
    with TERM_LOCK:
        term = TERMS.get(data['terminal'])
        if not term:
            raise ValueError('终端已不存在')
        with term.condition:
            endpoint = term.endpoint
            if term.closed or endpoint.get('kind') != 'nested' or endpoint['id'] != data.get('connection'):
                raise ValueError('请回到保存此指令的 SSH 连接')
            return STORE.save(collection, data, nested=endpoint)


def terminal_events(data):
    requested = data.get('terminals')
    if not isinstance(requested, list) or not 1 <= len(requested) <= 2 * MAX_TERMINALS:
        raise ValueError('请选择要显示的终端')
    with TERM_LOCK:
        watched, missing = [], []
        for item in requested:
            identifier, after = item.get('id'), item.get('after')
            if not isinstance(identifier, str) or type(after) is not int or after < -1:
                raise ValueError('无效的终端位置')
            if identifier in TERMS:
                watched.append((TERMS[identifier], after))
            else:
                missing.append(identifier)

    def available():
        packets = []
        for term, after in watched:
            with term.condition:
                if term.revision > after:
                    packets.append(term.events(after, False))
        return packets

    # Snapshot the global revision first, then check the individual sessions without that lock.
    with TERMINAL_EVENTS:
        observed = TERMINAL_REVISION
    packets = available()
    if not packets and not missing:
        with TERMINAL_EVENTS:
            TERMINAL_EVENTS.wait_for(lambda: TERMINAL_REVISION != observed, timeout=18)
        packets = available()
    return {'packets': packets, 'missing': missing}


def delete_configuration(data, preview):
    with TERM_LOCK:
        active = set()
        for term in TERMS.values():
            with term.condition:
                managed = term.run and term.run['phase'] in ACTIVE
                manual = term.context is not None and not term.ready
                if not term.closed:
                    if managed:
                        active.add(term.run['connection'])
                    if manual:
                        active.add(term.endpoint['id'])
        fingerprint = None if preview else data.get('fingerprint')
        if not preview and (not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint)):
            raise ValueError('请先查看需要删除的内容')
        return STORE.deletion(data.get('collection'), data.get('id'), fingerprint, active)


def terminal_list():
    entries = []
    with TERM_LOCK:
        for term in TERMS.values():
            with term.condition:
                entries.append({'id': term.id, 'connection': term.connection['id'], 'server': server_id(term.connection),
                                'closed': term.closed, 'active': not term.closed and bool(term.run and term.run['phase'] in ACTIVE),
                                'name': term.run['name'] if term.run else term.connection['name'],
                                'connectionName': term.connection['name']})
    return entries


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, data, content_type='application/json', cookie=None, head=False):
        body = json.dumps(data, ensure_ascii=False).encode() if content_type == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def authorized(self):
        try:
            jar = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return SESSIONS.get(jar[COOKIE_NAME].value, 0) > time.monotonic()
        except (KeyError, cookies.CookieError):
            return False

    def transfer_stream(self, job, operation):
        self.connection.settimeout(55)
        if operation == 'upload':
            length = int(self.headers.get('Content-Length', '-1'))
            if self.headers.get('Transfer-Encoding') or length != job.size:
                raise ValueError('上传文件的长度不正确')
            job.upload(self.rfile, length)
            self.respond(200, job.public())
            return
        job.claim('download')
        if job.size == 0:
            job.download(self.wfile)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(job.size))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(job.name, safe=''))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if job.size:
                job.download(self.wfile)
        except (OSError, ValueError):
            job.cancel()
            self.close_connection = True

    def route(self, post=False, head=False):
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.headers.get('Host') != HOST or post and self.headers.get('Origin') != ORIGIN:
            self.respond(403, {'error': '请使用工作台配置的访问地址'}, head=head)
            return
        try:
            if not post and path in ASSETS:
                file, mime = ASSETS[path]
                self.respond(200, (ROOT / 'ui' / file).read_bytes(), mime, head=head)
                return
            file_route = FILE_ROUTE.fullmatch(path)
            if file_route:
                if not self.authorized():
                    self.respond(401, {'error': '请先登录'}, head=head)
                    return
                identifier, operation = file_route.groups()
                with FILE_LOCK:
                    job = FILE_JOBS.get(identifier)
                if not job:
                    self.respond(404, {'error': '找不到这项文件传输'}, head=head)
                elif operation == 'status' and not post:
                    self.respond(200, job.public(), head=head)
                elif operation == 'cancel' and post:
                    job.cancel()
                    self.respond(200, job.public())
                elif not head and (post and operation == 'upload' or not post and operation == 'download'):
                    self.transfer_stream(job, operation)
                else:
                    self.respond(405, {'error': '无效的文件传输请求'}, head=head)
                return
            data = {}
            if post:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 65536:
                    raise ValueError('请求长度不正确')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('无效请求')
            if post and path == '/api/login':
                if not verify(data.get('username'), data.get('password')):
                    self.respond(401, {'error': '账号或密码不正确'})
                    return
                token = secrets.token_urlsafe(32)
                now = time.monotonic()
                with TERM_LOCK:
                    for old in [key for key, expiry in SESSIONS.items() if expiry <= now]:
                        del SESSIONS[old]
                    SESSIONS[token] = now + 8 * 60 * 60
                cookie = f'{COOKIE_NAME}={token}; Path=/; SameSite=Strict; HttpOnly'
                self.respond(200, {'ok': True}, cookie=cookie + ('; Secure' if TLS_CERT else ''))
                return
            if not self.authorized():
                self.respond(401, {'error': '请先登录'}, head=head)
                return
            if not post and path == '/api/state':
                self.respond(200, STORE.current())
            elif not post and path == '/api/run-log':
                query = parse_qs(parsed.query)
                before = int(query['before'][0]) if 'before' in query else None
                if before is not None and not 0 < before < 2**63:
                    raise ValueError('无效的执行日志位置')
                self.respond(200, recent_runs(before))
            elif not post and path == '/api/terminals':
                self.respond(200, terminal_list())
            elif post and path == '/api/terminal':
                self.respond(201, open_terminal(data))
            elif post and path == '/api/files':
                self.respond(200, file_request(data))
            elif post and path == '/api/run':
                self.respond(200, execute_on_terminal(data))
            elif post and path == '/api/terminal-events':
                self.respond(200, terminal_events(data))
            elif post and path == '/api/logs':
                self.respond(200, follow(STORE.current(), data.get('sources')))
            elif post and path in ('/api/connections', '/api/apps', '/api/commands', '/api/flows'):
                self.respond(200, save_operation(path.rsplit('/', 1)[1], data))
            elif post and path in ('/api/delete-preview', '/api/delete'):
                self.respond(200, delete_configuration(data, preview=path.endswith('preview')))
            elif post and path == '/api/password':
                self.respond(200, {'password': STORE.password(data.get('id'))})
            elif post and path == '/api/connection-password':
                _, password = STORE.login_connection(data.get('id'))
                self.respond(200, {'password':password})
            elif re.fullmatch(r'/api/terminal/[A-Za-z0-9_-]{20,28}/(events|input|size|control|close)', path):
                _, _, _, identifier, operation = path.split('/')
                with TERM_LOCK:
                    term = TERMS.get(identifier)
                if not term:
                    raise ValueError('终端已不存在')
                if not post and operation == 'events':
                    query = parse_qs(parsed.query)
                    after = max(0, int(query.get('after', ['0'])[0]))
                    self.respond(200, term.events(after, query.get('wait') == ['1']))
                    return
                if not post or operation == 'events':
                    raise ValueError('无效的终端操作')
                if operation == 'input':
                    text = data.get('text')
                    if not isinstance(text, str) or len(text.encode()) > 24000 or '\0' in text:
                        raise ValueError('终端输入过长')
                    term.input(text)
                elif operation == 'size':
                    cols, rows = data.get('cols'), data.get('rows')
                    if type(cols) is int and type(rows) is int and 15 <= cols <= 520 and 4 <= rows <= 250 and not term.closed:
                        term.resize(cols, rows)
                elif operation == 'close':
                    with term.condition:
                        if not term.closed and term.file_transfer:
                            raise ValueError('当前终端仍有文件操作，请先取消或等待结束')
                        if not term.closed and term.run and term.run['phase'] in ACTIVE:
                            raise ValueError('请先退出自动化，再断开 SSH 终端')
                        term.close()
                else:
                    action = data.get('action')
                    if action not in ('takeover', 'pause', 'resume', 'retry', 'next', 'end', 'leave'):
                        raise ValueError('无效的执行操作')
                    term.control(action)
                self.respond(200, {'ok': True})
            else:
                self.respond(404, {'error': '找不到页面'}, head=head)
        except FileConflict as exc:
            self.respond(409, {'error': str(exc)}, head=head)
        except (ValueError, AttributeError, TypeError) as exc:
            self.respond(400, {'error': str(exc)}, head=head)
        except (OSError, sqlite3.Error):
            self.respond(503, {'error': '连接断开或本机服务暂不可用'}, head=head)

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route(head=True)

    def do_POST(self):
        self.route(post=True)


def listener_options(address, certificate, key, allowed, allow_http=False):
    bind = IPv4Address(address)
    if bind.is_unspecified or bind.is_multicast or int(bind) == 0xffffffff:
        raise ValueError('Set a specific IPv4 address for WORKBENCH_BIND_ADDRESS')
    if bool(certificate) != bool(key):
        raise ValueError('Set both WORKBENCH_TLS_CERT and WORKBENCH_TLS_KEY')
    networks = tuple(IPv4Network(item.strip()) for item in allowed.split(',') if item.strip())
    if not bind.is_loopback:
        if not networks:
            raise ValueError('LAN access requires WORKBENCH_ALLOWED_NETWORKS')
        if not certificate and not allow_http:
            raise ValueError('LAN access requires HTTPS or an explicit WORKBENCH_ALLOW_HTTP=1 setting')
    context = None
    if certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, key)
    return context, networks


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, address, handler, tls, networks):
        self.tls = tls
        self.networks = networks
        super().__init__(address, handler)

    def verify_request(self, request, client_address):
        return not self.networks or any(IPv4Address(client_address[0]) in network for network in self.networks)

    def process_request_thread(self, request, client_address):
        if self.tls:
            try:
                request.settimeout(10)
                request = self.tls.wrap_socket(request, server_side=True)
                request.settimeout(30)
            except OSError:
                self.shutdown_request(request)
                return
        super().process_request_thread(request, client_address)


def main():
    parser = argparse.ArgumentParser(description='SSH Workbench for a local desktop')
    parser.add_argument('command', choices=('serve', 'set-password'), nargs='?', default='serve')
    options = parser.parse_args()
    os.umask(0o077)
    setup_data()
    if options.command == 'set-password':
        password = getpass('New admin password: ')
        if password != getpass('Confirm admin password: '):
            parser.error('Passwords do not match')
        try:
            set_password(password)
        except ValueError as exc:
            parser.error(str(exc))
        print('Local administrator password configured.')
        return
    if not AUTH.exists():
        parser.error('Run python3 workbench.py set-password first')
    if not (ROOT / 'ui/node_modules/@xterm/xterm/lib/xterm.js').is_file():
        parser.error('Run npm ci --prefix ui first')
    if not all((ROOT / 'ui/dist' / file).is_file() for file in ('workspace.js', 'workspace.css')):
        parser.error('Run npm run build --prefix ui first')
    try:
        tls, networks = listener_options(BIND_ADDRESS, TLS_CERT, TLS_KEY, ALLOWED_NETWORKS, ALLOW_HTTP)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    server = WorkbenchServer((BIND_ADDRESS, PORT), Handler, tls, networks)
    print(f'SSH Workbench is listening at {ORIGIN}/', flush=True)
    with server:
        server.serve_forever()


if __name__ == '__main__':
    main()
