"""Local-only settings shared by the server and the SSH client."""

import os
from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('WORKBENCH_DATA_DIR', ROOT / '.data')).expanduser().resolve()
PORT = int(os.environ.get('WORKBENCH_PORT', '14441'))


def setup_data():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = DATA.stat()
    if owner.st_uid != os.getuid() or stat.S_IMODE(owner.st_mode) & 0o077:
        raise PermissionError(f'Set {DATA} to mode 0700 and give it to the current user')
