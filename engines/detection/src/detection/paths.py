"""Where eagled puts the files and models it transfers to an engine.

eagled sets STEELEAGLE_FILES and STEELEAGLE_MODELS to host paths, and also
mounts the directories into a sandboxed or containerized plugin at
/steeleagle/files and /steeleagle/models. The host path may not exist inside
the sandbox, so the mount is the fallback.
"""

from pathlib import Path

FILES_ENV = "STEELEAGLE_FILES"
MODELS_ENV = "STEELEAGLE_MODELS"
SANDBOX_FILES_DIR = Path("/steeleagle/files")
SANDBOX_MODELS_DIR = Path("/steeleagle/models")


class PathError(Exception):
    pass


def _directory(env, sandbox_dir):
    import os

    configured = os.environ.get(env)
    if configured and Path(configured).is_dir():
        return Path(configured)
    if sandbox_dir.is_dir():
        return sandbox_dir
    raise PathError(f"{env} is unset or missing, and {sandbox_dir} does not exist")


def files_dir():
    """The directory transferred files are saved in."""
    return _directory(FILES_ENV, SANDBOX_FILES_DIR)


def models_dir():
    """The directory transferred models are saved in."""
    return _directory(MODELS_ENV, SANDBOX_MODELS_DIR)


def resolve(directory, name):
    """The file `name` in `directory`. Like eagled's transfers, `name` is a bare
    file name, so it cannot point outside the directory."""
    if not name:
        raise PathError("file name is empty")
    if Path(name).name != name or name in (".", ".."):
        raise PathError(f"{name!r} must be a file name without directories")
    return directory / name
