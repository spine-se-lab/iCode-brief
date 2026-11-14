# Copyright (c) 2026 Chrys. All rights reserved.

"""Use Chrys owner verification and atomic writes for session artifacts."""

from chrys.foundation.platform.files import atomic_write_owner_only_text, secure_open_owner_verified_binary


class ChrysArtifacts:
    write_text = staticmethod(atomic_write_owner_only_text)
    open_verified_binary = staticmethod(secure_open_owner_verified_binary)
