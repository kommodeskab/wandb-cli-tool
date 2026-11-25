import stat

from wandb_cli_tool import config
from wandb_cli_tool.config import Credentials


def test_load_missing_returns_none():
    assert config.load() is None


def test_save_load_roundtrip_and_permissions():
    config.save(Credentials("k", "me"))
    assert config.load() == Credentials("k", "me")
    assert stat.S_IMODE(config.config_path().stat().st_mode) == 0o600


def test_clear_removes_file_and_is_idempotent():
    config.save(Credentials("k", "me"))
    config.clear()
    config.clear()
    assert config.load() is None


def test_corrupt_file_is_treated_as_logged_out():
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text("not json")
    assert config.load() is None
