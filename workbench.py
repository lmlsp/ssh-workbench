"""Local SSH workbench HTTP service."""

import argparse
from getpass import getpass
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from catalog import Store, match
from auth import AUTH, set_password, verify
from multi_logs import follow
from settings import PORT, ROOT, setup_data
from terminal import ACTIVE, Terminal


HOST = f'127.0.0.1:{PORT}'
ORIGIN = 'http://' + HOST
STORE = Store()
SESSIONS = {}
TERM_LOCK = threading.RLock()
TERMS = {}
ASSETS = {
    '/': ('ssh.html', 'text/html; charset=utf-8'),
    '/ssh.js': ('ssh.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/ssh.css': ('ssh.css', 'text/css; charset=utf-8'),
    '/vendor/xterm.js': ('node_modules/@xterm/xterm/lib/xterm.js', 'text/javascript; charset=utf-8'),
    '/vendor/xterm.css': ('node_modules/@xterm/xterm/css/xterm.css', 'text/css; charset=utf-8'),
    '/vendor/fit.js': ('node_modules/@xterm/addon-fit/lib/addon-fit.js', 'text/javascript; charset=utf-8'),
}


def open_terminal(data, run=False):
    with TERM_LOCK:
        if run:
            kind = data.get('kind')
            if kind not in ('command', 'flow'):
                raise ValueError('请选择快捷指令或流程')
            name, connection, steps, password = STORE.plan(kind, data.get('id'), data.get('params') or {})
            if any(not t.closed and t.run and t.run['phase'] in ACTIVE and t.connection['id'] == connection['id'] for t in TERMS.values()):
                raise ValueError('这个连接已有自动任务，请先处理该任务')
            current = TERMS.get(data.get('terminal'))
            if current:
                with current.condition:
                    if (not current.closed and current.ready and current.connection == connection
                            and current.context.get('user') == steps[0]['user']):
                        current.begin(name, steps, password)
                        return {'id': current.id}
        else:
            connection = match(STORE.current(), 'connections', data.get('connection'))
        if sum(not t.closed for t in TERMS.values()) >= 12:
            raise ValueError('终端已达到 12 个，请关掉不用的连接')
        remaining = [t for t in TERMS.values() if t.closed]
        for old in remaining[:-12]:
            TERMS.pop(old.id, None)
        term = Terminal(connection)
        TERMS[term.id] = term
        if run:
            term.begin(name, steps, password)
        return {'id': term.id}


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
            return SESSIONS.get(jar['ssh_workbench'].value, 0) > time.monotonic()
        except (KeyError, cookies.CookieError):
            return False

    def route(self, post=False, head=False):
        parsed = urlsplit(self.path)
        path = parsed.path
        if self.headers.get('Host') != HOST or post and self.headers.get('Origin') != ORIGIN:
            self.respond(403, {'error': '请从本机工作台访问'}, head=head)
            return
        try:
            if not post and path in ASSETS:
                file, mime = ASSETS[path]
                self.respond(200, (ROOT / 'ui' / file).read_bytes(), mime, head=head)
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
                self.respond(200, {'ok': True}, cookie=f'ssh_workbench={token}; Path=/; SameSite=Strict; HttpOnly')
                return
            if not self.authorized():
                self.respond(401, {'error': '请先登录'}, head=head)
                return
            if not post and path == '/api/state':
                self.respond(200, STORE.current())
            elif not post and path == '/api/terminals':
                with TERM_LOCK:
                    self.respond(200, [{'id': t.id, 'connection': t.connection['id'], 'closed': t.closed,
                                        'name': t.run['name'] if t.run else t.connection['name']} for t in TERMS.values()])
            elif post and path in ('/api/terminal', '/api/run'):
                self.respond(201, open_terminal(data, run=(path == '/api/run')))
            elif post and path == '/api/logs':
                self.respond(200, follow(STORE.current(), data.get('sources')))
            elif post and path in ('/api/connections', '/api/apps', '/api/commands', '/api/flows'):
                self.respond(200, STORE.save(path.rsplit('/', 1)[1], data))
            elif post and path == '/api/password':
                self.respond(200, {'password': STORE.password(data.get('id'))})
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
                    term.close()
                else:
                    action = data.get('action')
                    if action not in ('takeover', 'retry', 'next', 'end'):
                        raise ValueError('无效的执行操作')
                    term.control(action)
                self.respond(200, {'ok': True})
            else:
                self.respond(404, {'error': '找不到页面'}, head=head)
        except (ValueError, AttributeError, TypeError) as exc:
            self.respond(400, {'error': str(exc)}, head=head)
        except OSError:
            self.respond(503, {'error': '连接断开或本机服务暂不可用'}, head=head)

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route(head=True)

    def do_POST(self):
        self.route(post=True)


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
    print(f'SSH Workbench is listening at {ORIGIN}/', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
