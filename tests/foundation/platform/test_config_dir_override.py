"""Opt-in config root: no changes to default paths or shared host functions."""

import pytest

from chrys.foundation.platform import conventional_config_dir


def test_explicit_absolute_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CHRYS_CONFIG_DIR", str(tmp_path))
    assert conventional_config_dir() == tmp_path
    monkeypatch.setenv("CHRYS_CONFIG_DIR", "relative/config")
    with pytest.raises(ValueError, match="absolute"):
        conventional_config_dir()
    monkeypatch.setenv("CHRYS_CONFIG_DIR", "")
    assert conventional_config_dir() != tmp_path
