#!/usr/bin/env python3
"""OpenSSH askpass helper, deliberately separate from terminal output parsing."""

import os
import re
import socket
import sys
import termios

from ssh_login import password_prompt


def remembered_password(prompt):
    user = os.environ.get('WORKBENCH_SSH_USER', '')
    if not password_prompt(prompt, user):
        return b''
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(3)
            channel.connect(os.environ['WORKBENCH_SSH_TICKET'])
            channel.sendall(prompt.strip().encode() + b'\n')
            with channel.makefile('rb') as stream:
                return stream.readline(1024).rstrip(b'\r\n')
    except (KeyError, OSError):
        return b''


def manual_prompt(prompt):
    # The SSH process has no controlling TTY in remembered-password mode.
    # Use the existing terminal's slave for host confirmation and any other prompts.
    fd = os.open(os.environ['WORKBENCH_SSH_PTY'], os.O_RDWR | os.O_NOCTTY)
    original = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
    reveal = 'continue connecting' in prompt.lower() and 'fingerprint' in prompt.lower()
    value = bytearray()
    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, mode)
        safe = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', prompt)
        os.write(fd, ('\r\n' + safe).encode())
        while True:
            char = os.read(fd, 1)
            if char in (b'\r', b'\n'):
                return bytes(value)
            if char in (b'', b'\x03', b'\x04'):
                return None
            if char in (b'\x7f', b'\x08') and value:
                while value and value[-1] & 0xc0 == 0x80:
                    value.pop()
                if value:
                    value.pop()
                if reveal:
                    os.write(fd, b'\b \b')
            elif char >= b' ' and char != b'\x7f' and len(value) < 1000:
                value.extend(char)
                if reveal:
                    os.write(fd, char)
    finally:
        os.write(fd, b'\r\n')
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)
        os.close(fd)


if __name__ == '__main__':
    try:
        prompt = sys.argv[1]
        answer = remembered_password(prompt) or manual_prompt(prompt)
        if answer is None:
            raise SystemExit(1)
        sys.stdout.buffer.write(answer + b'\n')
    except (IndexError, KeyError, OSError):
        raise SystemExit(1)
