"""iCode's owner-only artifact persistence and append-only audit adapter."""

import os
from pathlib import Path

from chrys.foundation.platform.files import (
    atomic_write_owner_only_text,
    secure_open_owner_only_append,
    secure_open_owner_verified_binary,
)


class Artifacts:
    write_text = staticmethod(atomic_write_owner_only_text)
    open_verified_binary = staticmethod(secure_open_owner_verified_binary)

    @staticmethod
    def append_text(path: Path, payload: str) -> None:
        handle = secure_open_owner_only_append(path)
        with os.fdopen(handle.fd, "ab") as stream:
            stream.write(payload.encode("utf-8"))
