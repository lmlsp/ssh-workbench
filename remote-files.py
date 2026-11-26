"""Run file operations through the already authenticated terminal's effective user."""

import base64
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import tempfile
import termios
import time
import tty

CHUNK = 48 * 1024
WINDOW = CHUNK * 16
MAX_LINE = 2 + CHUNK * 4 // 3
TIMEOUT = 45


class Cancelled(Exception):
    pass


def json_bytes(data):
    return json.dumps(data, ensure_ascii=True, separators=(',', ':')).encode('ascii')


class Wire:
    def __init__(self, nonce):
        self.signature = b'\x1b]777;OW:' + nonce.encode('ascii') + b':'
        self.pending = b''

    def send(self, kind, payload=b''):
        encoded = base64.b64encode(b'F\0' + kind.encode('ascii') + b'\0' + payload)
        sys.stdout.buffer.write(self.signature + encoded + b'\x07')
        sys.stdout.buffer.flush()

    def read(self):
        deadline = time.monotonic() + TIMEOUT
        while b'\n' not in self.pending:
            if len(self.pending) > MAX_LINE:
                raise ValueError('文件传输帧过长')
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise TimeoutError('等待工作台发送文件数据超时')
            chunk = os.read(0, 65536)
            if not chunk:
                raise EOFError('终端已断开')
            self.pending += chunk
        line, _, self.pending = self.pending.partition(b'\n')
        if not 2 <= len(line) <= MAX_LINE or not line.startswith(b'#'):
            raise ValueError('文件传输帧格式不正确')
        if line[1:2] == b'C':
            raise Cancelled()
        return line[1:2], base64.b64decode(line[2:], validate=True)

    def check_cancel(self):
        if self.pending or select.select([0], [], [], 0)[0]:
            self.read()
            raise ValueError('收到意外的上传数据')

    def wait_for_release(self):
        # Both normal and canceled operations use an explicit release, so pending
        # messages stay inside the helper until it has restored the terminal.
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                action, _ = self.read()
                if action == b'E':
                    return
            except Cancelled:
                continue


def filename(name):
    if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\0' in name:
        raise ValueError('请输入单个文件或文件夹名称')
    return name


def existing(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def file_version(info):
    attributes = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    return hashlib.sha256(repr(attributes).encode('ascii')).hexdigest()


def listing(wire, directory):
    wire.send('meta', json_bytes({'directory': str(directory), 'home': str(Path.home())}))
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                entry.name.encode('utf-8')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) and entry.is_dir():
                    kind = 'directory'
                elif stat.S_ISREG(info.st_mode):
                    kind = 'file'
                else:
                    kind = 'other'
                wire.send('entry', json_bytes({'name': entry.name, 'kind': kind, 'size': info.st_size}))
            except (FileNotFoundError, PermissionError, UnicodeError):
                continue


def upload(wire, directory, path, request):
    size = request['size']
    original = existing(path)
    if original and not stat.S_ISREG(original.st_mode):
        raise ValueError('同名目标不是普通文件，请换一个文件名')
    if original and request['overwrite'] is not True:
        raise FileExistsError('目标目录已有同名文件')
    if request['overwrite'] and (not original or file_version(original) != request.get('version')):
        raise FileExistsError('原文件已变化，请重新确认覆盖')
    mode = stat.S_IMODE(original.st_mode) & 0o777 if original else None
    fd, temporary = tempfile.mkstemp(prefix='.workbench-upload-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as sink:
            wire.send('meta', json_bytes({'directory': str(directory), 'size': size}))
            received = 0
            digest = hashlib.sha256()
            while received < size:
                action, data = wire.read()
                if action != b'D' or not 0 < len(data) <= min(CHUNK, size - received):
                    raise ValueError('收到的文件长度不正确')
                sink.write(data)
                digest.update(data)
                received += len(data)
                wire.send('progress', str(received).encode('ascii'))
            action, expected = wire.read()
            if action != b'H' or expected != digest.hexdigest().encode('ascii'):
                raise ValueError('文件完整性校验失败')
            if mode is None:
                mask = os.umask(0)
                os.umask(mask)
                mode = 0o666 & ~mask
            os.fchmod(sink.fileno(), mode)
            sink.flush()
            os.fsync(sink.fileno())
        wire.check_cancel()
        if original:
            current = existing(path)
            if not current or file_version(current) != file_version(original):
                raise FileExistsError('原文件已变化，请重新确认覆盖')
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def download(wire, directory, path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        initial = os.fstat(source.fileno())
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError('请选择普通文件下载')
        size = initial.st_size
        remaining = size
        wire.send('meta', json_bytes({'directory': str(directory), 'size': size}))
        while True:
            action, _ = wire.read()
            if action != b'R':
                raise ValueError('下载请求格式不正确')
            length = min(remaining, WINDOW)
            while length:
                data = source.read(min(CHUNK, length))
                if not data:
                    raise ValueError('文件在下载过程中发生了变化')
                wire.send('chunk', data)
                length -= len(data)
                remaining -= len(data)
            if remaining == 0:
                break
        if file_version(os.fstat(source.fileno())) != file_version(initial):
            raise ValueError('文件在下载过程中发生了变化，请重试')


def inspect_target(directory, path):
    info = existing(path)
    if info and not stat.S_ISREG(info.st_mode):
        raise ValueError('同名目标不是普通文件，请换一个文件名')
    return {'directory':str(directory), 'version':file_version(info) if info else None}


def operate(wire):
    action, payload = wire.read()
    if action != b'Q':
        raise ValueError('文件操作请求格式不正确')
    request = json.loads(payload)
    directory = Path(request['directory']).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('该路径不是目录')
    mode = request['mode']
    if mode == 'list':
        listing(wire, directory)
        return {}
    path = directory / filename(request['name'])
    if mode == 'mkdir':
        path.mkdir()
    elif mode == 'inspect':
        return inspect_target(directory, path)
    elif mode == 'upload':
        upload(wire, directory, path, request)
    elif mode == 'download':
        download(wire, directory, path)
    else:
        raise ValueError('未知文件操作')
    return {}


def main():
    def terminate(*_):
        raise SystemExit(1)

    for kind in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(kind, terminate)
    wire = Wire(os.environ['OW_NONCE'])
    original = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        try:
            wire.send('ready')
            result = {'status':'succeeded', **operate(wire)}
        except Cancelled:
            result = {'status':'cancelled', 'error':'已取消文件操作'}
        except FileExistsError as exc:
            result = {'status':'conflict', 'error':exc.strerror or str(exc)}
        except PermissionError:
            result = {'status':'failed', 'error':'当前用户没有这个路径的访问权限'}
        except FileNotFoundError:
            result = {'status':'failed', 'error':'文件或目录已不存在'}
        except (ValueError, TimeoutError, OSError, EOFError) as exc:
            result = {'status':'failed', 'error':getattr(exc,'strerror',None) or str(exc)}
        wire.send('result', json_bytes(result))
        wire.wait_for_release()
    except (EOFError, OSError, ValueError):
        pass
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, original)


if __name__ == '__main__':
    main()
