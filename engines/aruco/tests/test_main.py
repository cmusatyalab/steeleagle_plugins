import pytest

from aruco import main


def test_another_plugin_is_refused(monkeypatch):
    monkeypatch.setenv("STEELEAGLE_PLUGIN", "detection.plugin.toml")
    with pytest.raises(SystemExit, match="Unrecognized plugin"):
        main.main()


def test_an_engine_endpoint_is_required(monkeypatch):
    monkeypatch.setenv("STEELEAGLE_PLUGIN", main.PLUGIN_FILE)
    monkeypatch.delenv("STEELEAGLE_ENGINE_ENDPOINT", raising=False)
    with pytest.raises(SystemExit, match="ENDPOINT"):
        main.main()
