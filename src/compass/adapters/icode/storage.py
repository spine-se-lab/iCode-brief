"""iCode's owner-only artifact persistence port."""

from chrys.foundation.platform.files import atomic_write_owner_only_text, secure_open_owner_verified_binary


class Artifacts:
    write_text = staticmethod(atomic_write_owner_only_text)
    open_verified_binary = staticmethod(secure_open_owner_verified_binary)
