"""Resolve Compass data from the shared deployment config on every host startup.

Only the config reference is an installation asset. Business directories are
never owned by the installer. Legacy sessions retain their host directory.
"""

from __future__ import annotations

import json
import sysconfig
import tempfile
from pathlib import Path

from compass_brief.persistence.locations import configured_root, outside_store, session_path


def deployment_binding():
    locator = Path(sysconfig.get_path("purelib")) / "codehelix_compass_brief.json"
    if not locator.exists():
        return None  # Pre-config installations and direct development use.
    reference = json.loads(locator.read_text())
    binding = json.loads(Path(reference["config_ref"]).read_text())
    return reference, binding["configuration"]


def session_directory(host_directory, session_id, *, binding=None):
    binding = deployment_binding() if binding is None else binding
    configuration = binding[1] if binding else {}
    reference = binding[0] if binding else {}
    store = reference.get("package_store")
    if store is None and reference.get("config_ref"):
        # Pre-boundary locators used the shared Home/config/plugins/<plugin>/<id>.json layout.
        store = Path(reference["config_ref"]).expanduser().absolute().parents[3] / "package-store"
    # Managed locators name Home/package-store explicitly. Only legacy locators
    # above depend on the old config path layout; config_ref may live elsewhere.
    home = Path(store).expanduser().absolute().parent if store is not None else None
    protected = () if store is None else (store,)
    # Missing mode is an existing/development binding, never a new-install
    # default. New managed installs persist the explicit codehelix selection.
    mode, root = configured_root(configuration, home, default_mode="host_session")
    if mode == "host_session":
        if host_directory is None:
            return None
        return outside_store(Path(host_directory) / "compass_brief", protected)
    # Host startup can precede session creation. Never invent a session ID or
    # fall back to a host directory for independently configured storage.
    directory = session_path(root, reference["deployment_id"], session_id, protected=protected)
    if session_id is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    # Fail explicitly; never switch back to the unselected default directory.
    with tempfile.TemporaryFile(dir=directory):
        pass
    return directory
