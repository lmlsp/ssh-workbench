"""Saved SSH connections, shortcut commands, flows, and application log sources."""

import base64
from hashlib import sha256
import ipaddress
import json
import os
import re
import secrets
import shlex
import threading
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from settings import DATA

STATE = DATA / 'workbench.json'
KEY = DATA / 'workbench.key'
USER = re.compile(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?')
HOST = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,251}')
PARAM = re.compile(r'\{\{([a-zA-Z][a-zA-Z0-9_]{0,31})\}\}')
DELETABLE = ('servers', 'connections', 'commands', 'flows')


def server_id(connection):
    host = connection['host'].lower().rstrip('.')
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        pass
    endpoint = json.dumps([host, connection['port']])
    return sha256(endpoint.encode()).hexdigest()[:32]


def groups(connections):
    result = {}
    for connection in connections:
        identifier = server_id(connection)
        group = result.setdefault(identifier, {'id': identifier, 'name': connection['name'],
                                                'host': connection['host'], 'port': connection['port'], 'connections': []})
        group['connections'].append(connection['id'])
    return list(result.values())


def initial():
    return {'connections': [], 'apps': [], 'commands': [], 'flows': []}


def clean(data, key, limit, required=True):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('请检查输入字段的格式或长度')
    value = value.strip()
    if required and not value:
        raise ValueError('请填写名称及必填信息')
    return value


def command_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 16000 or '\0' in value:
        raise ValueError('请输入要执行的命令，最多 16 KB')
    return value


def switch_target(script):
    """Recognize a standalone, literal su login command."""
    script = script.strip()
    if '\n' in script or '\r' in script:
        return None
    try:
        words = shlex.split(script)
    except ValueError:
        return None
    if len(words) == 3 and words[0] == 'su' and words[1] in ('-', '-l', '--login') and USER.fullmatch(words[2]):
        return words[2]
    return None


def render(script, values):
    """Replace complete, unquoted shell words with a single quoted argument."""
    placeholders = {match.start(): match for match in PARAM.finditer(script)}
    if placeholders and ('<<' in script or '`' in script):
        raise ValueError('含 heredoc 或反引号的命令请先把参数赋值提到独立一行')
    output = []
    quote = None
    index = 0
    while index < len(script):
        current = script[index]
        if index in placeholders:
            match = placeholders[index]
            before = script[index - 1:index] if index else ''
            after = script[match.end():match.end() + 1]
            if quote or before and before not in ' \t\r\n=(|&;' or after and after not in ' \t\r\n)|&;':
                raise ValueError('参数需要替换一个完整的值，外层引号也要选中')
            output.append(shlex.quote(values[match[1]]))
            index = match.end()
            continue
        if current == '\\' and quote != "'" and index + 1 < len(script):
            if index + 1 in placeholders:
                raise ValueError('参数位置不能带反斜杠')
            output.append(script[index:index + 2])
            index += 2
            continue
        if current in "'\"":
            if quote == current:
                quote = None
            elif quote is None:
                quote = current
        output.append(current)
        index += 1
    return ''.join(output)


def associated(state, connection, app):
    match(state, 'connections', connection)
    if app and match(state, 'apps', app)['connection'] != connection:
        raise ValueError('请选择这个连接上的应用')


def match(state, collection, identifier):
    found = next((item for item in state[collection] if item['id'] == identifier), None)
    if not found:
        raise ValueError('这条记录已不存在')
    return found


def key():
    if not KEY.is_file():
        try:
            with os.fdopen(os.open(KEY, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
                stream.write(AESGCM.generate_key(bit_length=256))
        except FileExistsError:
            pass
    return KEY.read_bytes()


def seal(identifier, password):
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(key()).encrypt(nonce, password.encode(), identifier.encode())).decode()


def unseal(command):
    if not command.get('secret'):
        return None
    encrypted = base64.b64decode(command['secret'])
    return AESGCM(key()).decrypt(encrypted[:12], encrypted[12:], command['id'].encode()).decode()


def public(item):
    row = {**{k: v for k, v in item.items() if k != 'secret'}, 'passwordSet': bool(item.get('secret'))}
    target = switch_target(item['script'])
    if target:
        row['switchTo'] = target
    return row


class Store:
    def __init__(self):
        self.lock = threading.RLock()

    def read(self):
        return json.loads(STATE.read_text()) if STATE.exists() else initial()

    def current(self):
        with self.lock:
            state = self.read()
            state['servers'] = groups(state['connections'])
            state['connections'] = [{**item, 'server': server_id(item)} for item in state['connections']]
            state['commands'] = [public(item) for item in state['commands']]
            return state

    def deletion(self, collection, identifier, fingerprint=None, active_connections=()):
        """Preview or apply an atomic deletion, including only the dependencies shown."""
        if collection not in DELETABLE:
            raise ValueError('请选择要删除的服务器、连接、快捷指令或流程')
        with self.lock:
            state = self.read()
            target = match({**state, 'servers': groups(state['connections'])}, collection, identifier)
            if collection == 'connections':
                connections = {identifier}
            elif collection == 'servers':
                connections = set(target['connections'])
            else:
                connections = set()
            impacted = {kind: [] for kind in ('connections', 'commands', 'flows', 'apps')}
            for kind in impacted:
                impacted[kind] = [item for item in state[kind] if (
                    kind == collection and item['id'] == identifier or
                    (item['id'] if kind == 'connections' else item['connection']) in connections)]
            command_ids = {item['id'] for item in impacted['commands']}
            removed_flows = {item['id'] for item in impacted['flows']}
            references = [item['name'] for item in state['flows']
                          if item['id'] not in removed_flows and command_ids.intersection(item['steps'])]
            busy = bool(connections.intersection(active_connections))
            token = sha256(json.dumps([target, impacted], sort_keys=True).encode()).hexdigest()
            summary = {'name': target['name'], 'references': references, 'busy': busy, 'fingerprint': token,
                       'items': {kind: [{'id': item['id'], 'name': item['name']} for item in rows]
                                 for kind, rows in impacted.items()}}
            if fingerprint is None:
                return summary
            if token != fingerprint:
                raise ValueError('关联配置已变化，请重新查看需要删除的内容')
            if references:
                raise ValueError('请先修改引用该指令的流程：' + '、'.join(references))
            if busy:
                raise ValueError('还有终端任务在运行或等待输入，请先处理完再删除')
            for kind, rows in impacted.items():
                removed = {item['id'] for item in rows}
                state[kind] = [item for item in state[kind] if item['id'] not in removed]
            self.write(state)
            return summary

    def save(self, collection, data):
        with self.lock:
            state = self.read()
            identifier = data.get('id') or uuid4().hex
            previous = match(state, collection, identifier) if data.get('id') else {}
            name = clean(data, 'name', 60)
            item = {'id': identifier, 'name': name}
            if collection == 'connections':
                host = clean(data, 'host', 253)
                user = clean(data, 'user', 65)
                port = data.get('port')
                if not HOST.fullmatch(host) or not USER.fullmatch(user) or type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError('SSH 主机、端口或用户格式不正确')
                item.update(host=host, user=user, port=port)
                if previous and any(previous[key] != item[key] for key in ('host', 'port', 'user')):
                    for command in state['commands']:
                        if command['connection'] == identifier:
                            command.pop('secret', None)
            elif collection == 'apps':
                connection = clean(data, 'connection', 64)
                match(state, 'connections', connection)
                raw = data.get('sources')
                if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
                    raise ValueError('请填写至少一个日志文件')
                old = {source['id']: source for source in previous.get('sources', [])}
                sources = []
                for source in raw:
                    path = clean(source, 'path', 1024)
                    if not path.startswith('/'):
                        raise ValueError('日志路径需要是服务器上的绝对文件路径')
                    entry = {'id': source.get('id') if source.get('id') in old else uuid4().hex,
                             'name': clean(source, 'name', 48), 'path': path}
                    sources.append(entry)
                item.update(connection=connection, sources=sources)
            else:
                connection = clean(data, 'connection', 64)
                app = clean(data, 'app', 64, False)
                associated(state, connection, app)
                item.update(connection=connection, app=app, type=clean(data, 'type', 24, False))
                if collection == 'commands':
                    user = clean(data, 'user', 65)
                    directory = clean(data, 'directory', 1024)
                    if not USER.fullmatch(user) or not directory.startswith('/'):
                        raise ValueError('请填写运行用户和绝对工作目录')
                    script = command_text(data.get('script'))
                    labels = data.get('params') or {}
                    if not isinstance(labels, dict):
                        raise ValueError('参数名称格式不正确')
                    params = {value: clean(labels, value, 40, False) or value for value in dict.fromkeys(PARAM.findall(script))}
                    render(script, dict.fromkeys(params, 'test parameter'))
                    item.update(user=user, directory=directory, script=script, params=params)
                    target = switch_target(script)
                    if target and any(identifier in flow['steps'] for flow in state['flows']):
                        raise ValueError('请先从流程移除这条指令，再改成切换用户指令')
                    if previous and (previous['connection'], previous['user']) != (connection, user):
                        for flow in state['flows']:
                            if identifier not in flow['steps']:
                                continue
                            peers = [match(state, 'commands', step) for step in flow['steps'] if step != identifier]
                            if flow['connection'] != connection or any(step['user'] != user for step in peers):
                                raise ValueError(f'请先从「{flow["name"]}」移除这条指令，再修改连接或执行用户')
                    password = data.get('password')
                    if password is not None:
                        if not isinstance(password, str) or len(password) > 500 or any(ord(x) < 32 for x in password):
                            raise ValueError('切换密码的格式不正确')
                        if password:
                            item['secret'] = seal(identifier, password)
                    elif (previous.get('secret') and previous['connection'] == connection and previous['user'] == user
                          and switch_target(previous['script']) == target):
                        item['secret'] = previous['secret']
                else:
                    steps = data.get('steps')
                    self.flow_steps(state, steps, connection)
                    item['steps'] = steps
            state[collection] = [item if row['id'] == identifier else row for row in state[collection]]
            if not previous:
                state[collection].append(item)
            self.write(state)
            return public(item) if collection == 'commands' else item

    def write(self, state):
        draft = STATE.with_suffix('.tmp')
        draft.write_text(json.dumps(state, ensure_ascii=False))
        draft.chmod(0o600)
        draft.replace(STATE)

    @staticmethod
    def flow_steps(state, steps, connection):
        if not isinstance(steps, list) or not 2 <= len(steps) <= 24:
            raise ValueError('请按顺序加入 2 至 24 条快捷指令')
        commands = [match(state, 'commands', identifier) for identifier in steps]
        if any(switch_target(step['script']) for step in commands):
            raise ValueError('切换用户指令请单独执行。流程中的步骤使用同一个执行用户。')
        if any(step['connection'] != connection or step['user'] != commands[0]['user'] for step in commands):
            raise ValueError('流程中的快捷指令须使用同一服务器连接和执行用户')
        return commands

    def plan(self, kind, identifier, values):
        with self.lock:
            state = self.read()
            chosen = match(state, 'flows' if kind == 'flow' else 'commands', identifier)
            steps = self.flow_steps(state, chosen['steps'], chosen['connection']) if kind == 'flow' else [chosen]
            connection = match(state, 'connections', chosen['connection'])
            if not isinstance(values, dict):
                raise ValueError('请输入执行参数')
            result = []
            for step in steps:
                bound = {name: clean(values, name, 4000) for name in step['params']}
                script = command_text(render(step['script'], bound))
                planned = {'name': step['name'], 'user': step['user'], 'directory': step['directory'],
                           'script': script, 'status': 'pending'}
                target = switch_target(step['script'])
                if target:
                    planned['switchTo'] = target
                result.append(planned)
            password = next((unseal(step) for step in steps if step.get('secret')), None)
            return chosen['name'], connection, result, password

    def password(self, identifier):
        with self.lock:
            return unseal(match(self.read(), 'commands', identifier))
