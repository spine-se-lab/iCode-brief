"""Explicit one-time migration of an old Compass profile; preview unless --apply is given."""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

import yaml

from chrys.foundation.platform.files import atomic_write_owner_only_text, secure_open_owner_verified_binary


def migrated_profile(data: dict) -> dict:
    result = deepcopy(data)
    metadata = result.get("metadata", {})
    if "compass_enabled" not in metadata:
        return result
    enabled = metadata.pop("compass_enabled") is True
    extensions = result.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(value, str) for value in extensions):
        raise ValueError("extensions must be a list of IDs")
    if enabled and result.get("acp"):
        raise ValueError("Compass does not support ACP profiles")
    if enabled and "compass" not in extensions:
        result["extensions"] = [*extensions, "compass"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with secure_open_owner_verified_binary(args.profile) as stream:
        original = yaml.safe_load(stream)
    if not isinstance(original, dict) or not isinstance(original.get("metadata", {}), dict):
        parser.error("Expected an agent profile mapping with mapping metadata")
    updated = migrated_profile(original)
    if updated == original:
        sys.stdout.write("No migration needed.\n")
    elif args.apply:
        atomic_write_owner_only_text(args.profile, yaml.safe_dump(updated, allow_unicode=True, sort_keys=False))
        sys.stdout.write("Migrated. Use Tools > Extensions to enable or disable Compass.\n")
    else:
        sys.stdout.write(
            "Migration available: replace metadata.compass_enabled with extensions. Run again with --apply.\n"
        )


if __name__ == "__main__":
    main()
