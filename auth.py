"""Local administrator password, initialized explicitly from the console."""

import hashlib
import hmac
import json
import secrets

from settings import DATA


AUTH = DATA / 'admin.json'
USERNAME = 'admin'
ROUNDS = 600000


def derive(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, ROUNDS)


def set_password(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('密码需要至少 12 个字符，最多 256 个字符')
    salt = secrets.token_bytes(24)
    item = {'salt': salt.hex(), 'digest': derive(password, salt).hex()}
    draft = AUTH.with_suffix('.tmp')
    draft.write_text(json.dumps(item), encoding='utf-8')
    draft.chmod(0o600)
    draft.replace(AUTH)


def verify(username, password):
    if username != USERNAME or not isinstance(password, str) or not 1 <= len(password) <= 256:
        return False
    item = json.loads(AUTH.read_text(encoding='utf-8'))
    return hmac.compare_digest(derive(password, bytes.fromhex(item['salt'])), bytes.fromhex(item['digest']))
