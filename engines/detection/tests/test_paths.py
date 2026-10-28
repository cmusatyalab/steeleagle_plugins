import pytest

from detection import paths


def test_files_dir_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.FILES_ENV, str(tmp_path))
    assert paths.files_dir() == tmp_path


def test_files_dir_falls_back_to_the_sandbox_mount(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.FILES_ENV, raising=False)
    monkeypatch.setattr(paths, "SANDBOX_FILES_DIR", tmp_path)
    assert paths.files_dir() == tmp_path


def test_files_dir_uses_the_mount_when_the_environment_path_does_not_exist(
    monkeypatch, tmp_path
):
    # The daemon sets the host path, which does not exist inside a sandbox.
    monkeypatch.setenv(paths.FILES_ENV, str(tmp_path / "host-only"))
    monkeypatch.setattr(paths, "SANDBOX_FILES_DIR", tmp_path)
    assert paths.files_dir() == tmp_path


def test_files_dir_is_an_error_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.FILES_ENV, raising=False)
    monkeypatch.setattr(paths, "SANDBOX_FILES_DIR", tmp_path / "nope")
    with pytest.raises(paths.PathError, match=paths.FILES_ENV):
        paths.files_dir()


def test_models_dir_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.MODELS_ENV, str(tmp_path))
    assert paths.models_dir() == tmp_path


def test_resolve_returns_the_file_in_the_directory(tmp_path):
    (tmp_path / "site.kml").write_text("x")
    assert paths.resolve(tmp_path, "site.kml") == tmp_path / "site.kml"


@pytest.mark.parametrize("name", ["../site.kml", "sub/site.kml", "/etc/passwd", ""])
def test_resolve_rejects_names_with_directories(tmp_path, name):
    with pytest.raises(paths.PathError, match="without directories|empty"):
        paths.resolve(tmp_path, name)
