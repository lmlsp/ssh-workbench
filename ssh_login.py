"""Provide a remembered SSH password once, through a private local socket."""

import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time


def password_prompt(prompt, user):
    value = prompt.strip()
    return value.lower() == 'password:' or bool(re.fullmatch(re.escape(user) + r"@[^\r\n]+'s password:", value))


class PasswordTicket:
    def __init__(self, user, password):
        self.user = user
        self.secret = password.encode()
        self.directory = tempfile.TemporaryDirectory(prefix='ssh-workbench-auth-')
        self.path = str(Path(self.directory.name) / 'password.sock')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lock = threading.Lock()
        self.closed = False
        try:
            self.socket.bind(self.path)
            os.chmod(self.path, 0o600)
            self.socket.listen(2)
            self.socket.settimeout(1)
        except OSError:
            self.close()
            raise
        threading.Thread(target=self.serve, daemon=True, name='ssh-login-ticket').start()

    def serve(self):
        expires = time.monotonic() + 180
        try:
            while not self.closed and time.monotonic() < expires:
                try:
                    channel, _ = self.socket.accept()
                except socket.timeout:
                    continue
                with channel:
                    channel.settimeout(2)
                    with channel.makefile('rb') as stream:
                        prompt = stream.readline(2048).decode(errors='replace')
                    with self.lock:
                        if not self.closed and password_prompt(prompt, self.user):
                            channel.sendall(self.secret + b'\n')
                            break
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.secret = b''
            self.socket.close()
            self.directory.cleanup()
