"""Keep every test away from the real ~/.cptr/config.toml.

Config.upsert mirrors settings into config.toml and create_token reads (or generates) the JWT secret there; tests
that set a temporary DB still wrote the live file, replacing its [app_config] and, once the file was unreadable,
the server's JWT secret.
"""

import pytest

from cptr.utils import config as cfg


@pytest.fixture(autouse=True)
def _isolated_config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    cfg.invalidate_config_cache()
    yield
    cfg.invalidate_config_cache()
