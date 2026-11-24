"""Saved SSH connections, shortcut commands, flows, and application log sources."""

import base64
from hashlib import sha256
import ipaddress
import json
import os
import pwd
import re
import secrets
import shlex
import socket
import threading
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from settings import DATA, LOCAL_TERMINAL_ENABLED

STATE = DATA / 'workbench.json'
KEY = DATA / 'workbench.key'
USER = re.compile(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?')
HOST = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,251}')
PARAM = re.compile(r'\{\{([a-zA-Z][a-zA-Z0-9_]{0,31})\}\}')
DELETABLE = ('servers', 'connections', 'commands', 'flows')
LOCAL_CONNECTION_ID = 'builtin-local'


def local_connection():
    return {'id':LOCAL_CONNECTION_ID, 'kind':'local', 'name':'本机终端', 'host':socket.gethostname(),
            'port':None, 'user':pwd.getpwuid(os.geteuid()).pw_name}


def normalize_host(host):
    host = host.lower().rstrip('.')
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def valid_host(host):
    if ':' in host:
        try:
            return '%' not in host and isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
        except ValueError:
            return False
    return bool(HOST.fullmatch(host))


def server_id(connection):
    if connection.get('kind') == 'local':
        return LOCAL_CONNECTION_ID
    location = [normalize_host(connection['host']), connection['port']]
    if connection.get('kind') == 'nested':
        location.append(connection['route'])
    endpoint = json.dumps(location)
    return sha256(endpoint.encode()).hexdigest()[:32]


def groups(connections):
    result = {}
    for connection in connections:
        identifier = server_id(connection)
        group = result.setdefault(identifier, {'id': identifier, 'name': connection['name'],
                                                'host': connection['host'], 'port': connection['port'], 'connections': []})
        if connection.get('kind') == 'local':
            group.update(kind='local', user=connection['user'])
        if connection.get('kind') == 'nested':
            group.update(kind='nested', via=connection['via'])
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


def ssh_target(script):
    """Recognize the interactive SSH forms supported by the terminal hook."""
    if '\n' in script or '\r' in script:
        return None
    try:
        words = shlex.split(script)
    except ValueError:
        return None
    if not words or words[0] != 'ssh':
        return None
    destination = ''
    port = None
    options = True
    args = iter(words[1:])
    for word in args:
        if word == '--' and options and not destination:
            options = False
        elif word.startswith('-p') and options and port is None:
            value = word[2:] or next(args, '')
            if not re.fullmatch(r'[0-9]{1,5}', value) or not 1 <= int(value) <= 65535:
                return None
            port = int(value)
        elif not destination and not word.startswith('-'):
            destination = word
        else:
            return None
    account, delimiter, host = destination.rpartition('@')
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if not valid_host(host) or delimiter and not USER.fullmatch(account):
        return None
    return (normalize_host(host), port or 22, account)


def flow_groups(data, steps):
    groups = data.get('stepGroups')
    if groups is None:
        return [''] * len(steps)
    if not isinstance(groups, list) or len(groups) != len(steps):
        raise ValueError('每个步骤须对应一个分组')
    return [clean({'name': group}, 'name', 32, False) for group in groups]


def step_waits(values, steps):
    if values is None:
        return [0] * len(steps)
    if not isinstance(values, list) or len(values) != len(steps):
        raise ValueError('每个步骤须对应一个等待时间')
    if any(type(wait) is not int or not 0 <= wait <= threading.TIMEOUT_MAX for wait in values):
        raise ValueError('等待时间须填写非负整数秒')
    return values


def flow_revision(flow, steps):
    return sha256(json.dumps([flow, steps], sort_keys=True).encode()).hexdigest()


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


def public_connection(item):
    return {**{key:value for key,value in item.items() if key != 'secret'},
            'server':server_id(item), 'passwordSet':bool(item.get('secret'))}


class Store:
    def __init__(self):
        self.lock = threading.RLock()

    def read(self):
        state = json.loads(STATE.read_text()) if STATE.exists() else initial()
        state['connections'] = [item for item in state['connections'] if item['id'] != LOCAL_CONNECTION_ID and item.get('kind') != 'local']
        if LOCAL_TERMINAL_ENABLED:
            state['connections'].append(local_connection())
        return state

    def current(self):
        with self.lock:
            state = self.read()
            state['servers'] = groups(state['connections'])
            state['connections'] = [public_connection(item) for item in state['connections']]
            commands = {command['id']: command for command in state['commands']}
            state['flows'] = [dict(flow, revision=flow_revision(flow, [commands.get(step) for step in flow['steps']]))
                              for flow in state['flows']]
            state['commands'] = [public(item) for item in state['commands']]
            return state

    def deletion(self, collection, identifier, fingerprint=None, active_connections=()):
        """Preview or apply an atomic deletion, including only the dependencies shown."""
        if collection not in DELETABLE:
            raise ValueError('请选择要删除的服务器、连接、快捷指令或流程')
        if collection in ('servers', 'connections') and identifier == LOCAL_CONNECTION_ID:
            raise ValueError('本机终端由部署设置提供')
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

    def save(self, collection, data, nested=None):
        with self.lock:
            state = self.read()
            if nested and not any(row['id'] == nested['id'] for row in state['connections']):
                state['connections'].append(nested)
            identifier = data.get('id') or uuid4().hex
            if collection == 'connections' and identifier == LOCAL_CONNECTION_ID:
                raise ValueError('本机终端使用工作台运行用户')
            previous = match(state, collection, identifier) if data.get('id') else {}
            name = clean(data, 'name', 60)
            item = {'id': identifier, 'name': name}
            if collection == 'connections':
                if previous.get('kind') == 'nested':
                    item = {**previous, 'name': name}
                else:
                    host = clean(data, 'host', 253)
                    user = clean(data, 'user', 65)
                    port = data.get('port')
                    if not valid_host(host) or not USER.fullmatch(user) or type(port) is not int or not 1 <= port <= 65535:
                        raise ValueError('SSH 主机、端口或用户格式不正确')
                    item.update(host=host, user=user, port=port)
                    changed_endpoint = previous and any(previous[key] != item[key] for key in ('host', 'port', 'user'))
                    password = data.get('password')
                    if password is not None:
                        if (not isinstance(password, str) or len(password.encode()) > 512
                                or any(ord(char) < 32 for char in password)):
                            raise ValueError('SSH 登录密码的格式不正确')
                        if password:
                            item['secret'] = seal(identifier, password)
                    elif previous.get('secret') and not changed_endpoint:
                        item['secret'] = previous['secret']
                    if changed_endpoint:
                        for command in state['commands']:
                            if command['connection'] == identifier:
                                command.pop('secret', None)
            elif collection == 'apps':
                connection = clean(data, 'connection', 64)
                if match(state, 'connections', connection).get('kind') == 'nested':
                    raise ValueError('请在该终端内执行日志命令')
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
                    ssh = ssh_target(script)
                    followup = data.get('followup', previous.get('followup')) or {}
                    if not isinstance(followup, dict):
                        raise ValueError('请检查下一步输入的配置')
                    prompt = clean(followup, 'prompt', 200, False)
                    if prompt:
                        mode = followup.get('mode')
                        if mode not in ('auto', 'manual'):
                            raise ValueError('请选择如何响应提示')
                        if target or ssh:
                            raise ValueError('SSH 和 su 会自动识别密码提示，请将自定义提示留空')
                        item['followup'] = {'prompt': prompt, 'mode': mode}
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
                          and switch_target(previous['script']) == target
                          and ssh_target(previous['script']) == ssh
                          and previous.get('followup', {}).get('prompt') == item.get('followup', {}).get('prompt')):
                        item['secret'] = previous['secret']
                else:
                    steps = data.get('steps')
                    self.flow_steps(state, steps, connection)
                    item['steps'] = steps
                    if 'stepGroups' not in data and previous.get('stepGroups') and steps != previous['steps']:
                        raise ValueError('请刷新页面后修改带分组的流程')
                    groups = flow_groups(data if 'stepGroups' in data else previous, steps)
                    if any(groups):
                        item['stepGroups'] = groups
                    if 'stepWaits' not in data and previous.get('stepWaits') and steps != previous['steps']:
                        raise ValueError('请刷新页面后修改带等待时间的流程')
                    waits = step_waits(data.get('stepWaits', previous.get('stepWaits')), steps)
                    if any(waits):
                        item['stepWaits'] = waits
            state[collection] = [item if row['id'] == identifier else row for row in state[collection]]
            if not previous:
                state[collection].append(item)
            self.write(state)
            if collection == 'connections':
                return public_connection(item)
            return public(item) if collection == 'commands' else item

    def write(self, state):
        draft = STATE.with_suffix('.tmp')
        saved = {**state, 'connections':[item for item in state['connections'] if item['id'] != LOCAL_CONNECTION_ID]}
        draft.write_text(json.dumps(saved, ensure_ascii=False))
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

    def plan(self, kind, identifier, values, selected=None, group_values=None, revision=None, waits=None):
        with self.lock:
            state = self.read()
            chosen = match(state, 'flows' if kind == 'flow' else 'commands', identifier)
            steps = self.flow_steps(state, chosen['steps'], chosen['connection']) if kind == 'flow' else [chosen]
            if kind == 'flow' and revision is not None and flow_revision(chosen, steps) != revision:
                raise ValueError('流程或快捷指令已有修改。请刷新页面后再运行。')
            connection = match(state, 'connections', chosen['connection'])
            if not isinstance(values, dict):
                raise ValueError('请输入执行参数')
            groups = flow_groups(chosen, steps) if kind == 'flow' else ['']
            waits = step_waits(chosen.get('stepWaits') if waits is None else waits, steps) if kind == 'flow' else [0]
            available = set(groups) - {''}
            if available:
                if not isinstance(selected, list) or not selected or any(not isinstance(group, str) or group not in available for group in selected):
                    raise ValueError('请至少选择一个要执行的分组')
                if group_values is None:
                    group_values = {}
                if not isinstance(group_values, dict):
                    raise ValueError('请检查分组参数')
            selected_steps = [(step, group, wait) for step, group, wait in zip(steps, groups, waits)
                              if not group or group in selected]
            result = []
            password = None
            for index, (step, group, wait) in enumerate(selected_steps):
                params = group_values.get(group, {}) if group else values
                if not isinstance(params, dict):
                    raise ValueError('请检查分组参数')
                bound = {name: clean(params, name, 4000) for name in step['params']}
                script = command_text(render(step['script'], bound))
                planned = {'name': step['name'], 'user': step['user'], 'directory': step['directory'],
                           'script': script, 'status': 'pending', 'group': group,
                           'waitSeconds': wait if index+1 < len(selected_steps) else 0}
                target = switch_target(step['script'])
                if target:
                    planned['switchTo'] = target
                ssh = ssh_target(step['script'])
                if ssh:
                    planned['sshLogin'] = ssh
                    planned['response'] = unseal(step)
                elif step.get('followup'):
                    planned['followup'] = step['followup']
                    planned['response'] = unseal(step) if step['followup']['mode'] == 'auto' else None
                elif password is None:
                    password = unseal(step)
                result.append(planned)
            return chosen['name'], connection, result, password

    def ssh_password(self, connection, source_user, host, port, user):
        with self.lock:
            passwords = {
                unseal(step) for step in self.read()['commands']
                if step['connection'] == connection and step['user'] == source_user and step.get('secret')
                and ssh_target(step['script']) == (host, port, user)
            }
            return next(iter(passwords)) if len(passwords) == 1 else None

    def password(self, identifier):
        with self.lock:
            return unseal(match(self.read(), 'commands', identifier))

    def login_connection(self, identifier):
        with self.lock:
            raw = match(self.read(), 'connections', identifier)
            return public_connection(raw), unseal(raw)
