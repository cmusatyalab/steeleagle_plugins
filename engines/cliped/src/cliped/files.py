"""The daemon's files directory: where an image prompt is read from, and where
log_tiles writes the images an engine takes in and sends out.

eagled sets STEELEAGLE_FILES to the host path of the files directory, and
mounts it into a sandboxed plugin at /steeleagle/files, where the host path
may not exist; so the mount is the fallback.
"""
import logging
import os
from pathlib import Path

import cv2

logger = logging.getLogger("cliped.files")

FILES_ENV = "STEELEAGLE_FILES"
SANDBOX_FILES_DIR = Path("/steeleagle/files")
JPEG_QUALITY = 75


class FilesError(Exception):
    pass


def files_dir(environ=os.environ):
    """The daemon's files directory."""
    configured = environ.get(FILES_ENV)
    if configured and Path(configured).is_dir():
        return Path(configured)
    if SANDBOX_FILES_DIR.is_dir():
        return SANDBOX_FILES_DIR
    raise FilesError(f"{FILES_ENV} is unset or missing, and {SANDBOX_FILES_DIR} does not exist")


def resolve(directory, name):
    """The existing file `name`, relative to `directory` and inside it."""
    if not name or Path(name).is_absolute():
        raise FilesError(f"{name!r} must be a file name relative to the files directory")
    root = Path(directory).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise FilesError(f"{name!r} is outside the files directory")
    if not path.is_file():
        raise FilesError(f"{name!r} is not a file in {root}")
    return path


def read_rgb(path):
    """The image file at `path` as an (h, w, 3) uint8 RGB array."""
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise FilesError(f"{path} is not an image OpenCV can read")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


class TileLog:
    """Writes each input image to <root>/in/<frame id>.jpeg and each output
    tile to <root>/out/<frame id>_<tile number>.jpeg, as JPEG at quality 75.
    A write that fails is logged, never raised: logging must not cost a
    frame."""

    def __init__(self, root):
        self.in_dir = Path(root) / "in"
        self.out_dir = Path(root) / "out"
        self.in_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("writing input images to %s and output tiles to %s", self.in_dir, self.out_dir)

    @classmethod
    def for_engine(cls, endpoint, environ=os.environ):
        """The TileLog of the engine served as `endpoint`, in the files
        directory."""
        return cls(files_dir(environ) / endpoint.replace(os.sep, "_"))

    def input(self, frame_id, rgb):
        self._write(self.in_dir / f"{frame_id}.jpeg", rgb)

    def output(self, frame_id, tile_number, rgb):
        self._write(self.out_dir / f"{frame_id}_{tile_number}.jpeg", rgb)

    @staticmethod
    def _write(path, rgb):
        try:
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
                logger.warning("failed to write %s", path)
        except Exception:
            logger.exception("failed to write %s", path)
