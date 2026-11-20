"""Native command credentials: never export the file's value to process environment."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

from chrys.foundation.platform.files import secure_open_owner_verified_binary


def read_key(path: Path) -> str:
    with secure_open_owner_verified_binary(path) as handle:
        mode = os.fstat(handle.fileno())
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) & 0o077:
            raise PermissionError("Key file must be an owner-only regular file (600).")
        key = handle.read(4097).decode().strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError("Put one API key on one line in the key file.")
    return key


def key_file_template(path: Path) -> str:
    # Isolated Python ignores repository/PYTHONPATH shadowing. The installed
    # package revalidates owner-only permissions on every credential refresh.
    command = shlex.join([sys.executable, "-I", "-m", "compass.adapters.icode.credentials", str(path.absolute())])
    command = command.replace("{{", "{{{{").replace("}}", "}}}}")
    return "{{!" + command + "}}"


if __name__ == "__main__":
    try:
        value = read_key(Path(sys.argv[1]))
    except Exception:
        # Credential failures must not print the path, contents or traceback.
        sys.stderr.write("Compass credential file could not be read securely.\n")
        raise SystemExit(1) from None
    sys.stdout.write(value)
