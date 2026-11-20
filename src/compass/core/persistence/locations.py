"""Shared preflight/runtime path rules, with no directory creation."""

import json
import sysconfig
from pathlib import Path


def configured_root(configuration, home, *, default_mode):
    mode = configuration.get("data_location", default_mode)
    value = configuration.get("data_root", "")
    if mode not in {"host_session", "codehelix", "custom"}:
        raise ValueError("data_location must be host_session, codehelix or custom")
    if mode != "custom":
        if value:
            raise ValueError("data_root requires data_location=custom")
        if mode == "host_session":
            return mode, None
        if home is None:
            raise ValueError("CodeHelix data location requires the deployment Home reference")
        return mode, Path(home) / "data/compass-brief"
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise ValueError("custom mode requires an absolute data_root")
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("custom mode requires an absolute data_root")
    return mode, root


def outside_store(path, protected):
    resolved = Path(path).expanduser().resolve()
    for area in protected:
        area = Path(area).resolve()
        if resolved.is_relative_to(area) or area.is_relative_to(resolved):
            raise ValueError("Compass business data must be separate from PackageStore")
    return resolved


def identity(value):
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\\0"):
        raise ValueError("Invalid Compass deployment/session identity")
    return value


def session_path(root, deployment, session=None, *, protected=()):
    # Resolve/check each existing component, not only data_root. Return the
    # canonical external path for host no-symlink artifact APIs as well.
    path = outside_store(root, protected)
    for component in ("sessions", identity(deployment), *(() if session is None else (identity(session),))):
        path = outside_store(path / component, protected)
    return path


def deployment_binding():
    """Read the managed deployment reference a CodeHelix install left beside the .pth.

    Host-neutral: the locator lives next to the activation in the target
    interpreter's own site-packages, so every host that is activated this way
    finds its deployment id and configuration the same way. Absent means a
    development checkout or a pre-configuration install, never a default.
    """
    locator = Path(sysconfig.get_path("purelib")) / "codehelix_compass_brief.json"
    if not locator.exists():
        return None
    reference = json.loads(locator.read_text())
    binding = json.loads(Path(reference["config_ref"]).read_text())
    return reference, binding["configuration"]
