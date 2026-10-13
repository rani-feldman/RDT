from pathlib import Path

from sim.__main__ import EXAMPLE_CONFIG, default_config


def test_example_config_without_sim_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_config() == EXAMPLE_CONFIG


def test_sim_toml_in_the_current_folder_is_the_default(tmp_path, monkeypatch):
    (tmp_path / "sim.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    assert default_config().resolve() == (tmp_path / "sim.toml").resolve()
    assert EXAMPLE_CONFIG.is_file() and isinstance(default_config(), Path)
