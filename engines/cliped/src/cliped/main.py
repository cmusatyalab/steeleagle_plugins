"""Entrypoint of the cliped plugins, which eagled starts with `pixi run start`:
the edge filter (cliped.plugin.toml) and the verifier (verifier.plugin.toml).

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, the daemon's files directory, which holds image
prompts and the images log_tiles writes, in STEELEAGLE_FILES, and, for the
edge filter, the remote daemon's IP, which qualifies the verifier's name, in
STEELEAGLE_REMOTE_QUALIFIER.
"""
import json
import logging
import os
import signal
import sys
import threading

from cliped.clip import BACKGROUNDS, FP32_WEIGHTS, LIBS_DIR, QUANTIZED_MODEL
from cliped.files import FilesError, TileLog, files_dir, read_rgb, resolve

CLIPED_PLUGIN = "cliped.plugin.toml"
VERIFIER_PLUGIN = "verifier.plugin.toml"

# Defaults of the args of cliped.plugin.toml. The daemon passes only the args
# set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    "prompt": "",
    "image": "",
    "quantized": True,
    "tile_size": 200,
    "stride": 150,
    "margin": 0.25,
    "max_send": 1,
    "backgrounds": BACKGROUNDS,
    "verifier": "",
    "k": 2,
    "ema": 0.9,
    "max_queue": 64,
    "resend_after_ms": 5000,
    "npu": True,
    "threads": 4,
    "log_scores": False,
    "log_tiles": False,
}
# Defaults of the args of verifier.plugin.toml
VERIFIER_DEFAULTS = {
    "prompt": "",
    "image": "",
    "model": "qwen2.5vl:3b",
    "host": "",
    "timeout_ms": 30000,
    "log_tiles": False,
}
POSITIVE_INTS = ("tile_size", "stride", "max_send", "k", "max_queue", "threads")

LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
ADSP_LIBRARY_PATH = "ADSP_LIBRARY_PATH"
# Where the Tachyon keeps the DSP's own libraries; ADSP_LIBRARY_PATH replaces
# the default search path, so they must stay on it.
SYSTEM_ADSP_DIRS = ("/usr/lib/rfsa/adsp", "/usr/lib/dsp/cdsp", "/dsp")


class ConfigError(Exception):
    pass


def _load(content, defaults):
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in defaults.items():
        if args.get(name) is None:
            args[name] = default
    return args


def _strings(args, *names):
    for name in names:
        if not isinstance(args[name], str):
            raise ConfigError(f"{name} must be a string, not {args[name]!r}")
        args[name] = args[name].strip()


def _prompt_or_image(args):
    """Checks that the prompt or the image is set; the image wins over the
    prompt, which is then cleared."""
    _strings(args, "prompt", "image")
    if not args["prompt"] and not args["image"]:
        raise ConfigError("the 'prompt' or 'image' arg must be set")
    if args["image"]:
        args["prompt"] = ""


def parse_args(content):
    """The args of cliped.plugin.toml in the JSON object `content`, with every
    unset arg defaulted. Raises ConfigError for args that are missing or out
    of range."""
    args = _load(content, DEFAULTS)
    _prompt_or_image(args)
    _strings(args, "verifier")
    if not args["verifier"]:
        raise ConfigError("the 'verifier' arg must be set")
    for name in POSITIVE_INTS:
        value = args[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
            raise ConfigError(f"{name} must be a positive integer, not {value!r}")
        args[name] = int(value)
    for name in ("margin", "ema", "resend_after_ms"):
        if isinstance(args[name], bool) or not isinstance(args[name], (int, float)):
            raise ConfigError(f"{name} must be a number, not {args[name]!r}")
    if not 0 <= args["ema"] < 1:
        raise ConfigError("ema must be in [0, 1)")
    if args["resend_after_ms"] <= 0:
        raise ConfigError("resend_after_ms must be positive")
    backgrounds = args["backgrounds"]
    if (not isinstance(backgrounds, list) or not backgrounds
            or not all(isinstance(bg, str) and bg.strip() for bg in backgrounds)):
        raise ConfigError("backgrounds must be a non-empty list of prompts")
    args["backgrounds"] = [bg.strip() for bg in backgrounds]
    for name in ("quantized", "npu", "log_scores", "log_tiles"):
        if not isinstance(args[name], bool):
            raise ConfigError(f"{name} must be true or false")
    return args


def parse_verifier_args(content):
    """The args of verifier.plugin.toml in the JSON object `content`, with
    every unset arg defaulted."""
    args = _load(content, VERIFIER_DEFAULTS)
    _prompt_or_image(args)
    _strings(args, "model", "host")
    if not args["model"]:
        raise ConfigError("the 'model' arg must be set")
    timeout = args["timeout_ms"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"timeout_ms must be a positive number, not {timeout!r}")
    if not isinstance(args["log_tiles"], bool):
        raise ConfigError("log_tiles must be true or false")
    return args


def image_path(args):
    """The image prompt's file, under the files directory, or None for a text
    prompt."""
    if not args["image"]:
        return None
    try:
        return resolve(files_dir(), args["image"])
    except FilesError as err:
        raise ConfigError(f"image: {err}") from None


def tile_log(args, name):
    """The TileLog of the engine `name` if log_tiles is set, or None."""
    if not args["log_tiles"]:
        return None
    try:
        return TileLog.for_engine(name)
    except (FilesError, OSError) as err:
        raise ConfigError(f"log_tiles: {err}") from None


def ensure_qnn_on_library_path():
    """Puts libs/ where QNN looks for its libraries: on LD_LIBRARY_PATH, which
    dlopen reads only when the process starts, so the process restarts with
    it if it is not there; and on ADSP_LIBRARY_PATH, where the DSP finds the
    HTP skel, ahead of the system's skel directories (';'-separated)."""
    libs = str(LIBS_DIR)
    adsp = os.environ.get(ADSP_LIBRARY_PATH, "")
    if libs not in adsp.split(";"):
        os.environ[ADSP_LIBRARY_PATH] = ";".join([libs, *filter(None, [adsp]), *SYSTEM_ADSP_DIRS])
    paths = os.environ.get(LD_LIBRARY_PATH, "")
    if libs in paths.split(":"):
        return
    os.environ[LD_LIBRARY_PATH] = f"{libs}:{paths}" if paths else libs
    os.execv(sys.executable, [sys.executable, "-m", "cliped.main", *sys.argv[1:]])


def build(args, verifier, *, image=None, tile_log=None):
    """The Cliped of `args`, verified by the engine `verifier`, searching for
    the image file `image` if given and for the text prompt if not."""
    import torch

    from cliped.clip import Encoder, NpuImageEncoder, Selector, image_vector, text_vectors
    from cliped.engine import Cliped
    from cliped.send_queue import SendQueue

    log = logging.getLogger("cliped")
    torch.set_num_threads(args["threads"])
    # The text encoder always runs FP32 on the CPU, once, for the prompt
    text_encoder = Encoder(visual_weights=None if args["quantized"] else FP32_WEIGHTS)
    t, b = text_vectors(text_encoder, None if image else args["prompt"], args["backgrounds"])
    if args["quantized"]:
        qnn_dir = LIBS_DIR if args["npu"] else None
        image_encoder = NpuImageEncoder(QUANTIZED_MODEL, qnn_dir, num_threads=args["threads"])
        log.info("INT8 image encoder %s on the %s", QUANTIZED_MODEL.name, "HTP" if args["npu"] else "CPU")
        del text_encoder  # only its vectors are needed
    else:
        image_encoder = text_encoder
        log.info("FP32 image encoder %s on the CPU", FP32_WEIGHTS.name)
    if image is not None:
        t = image_vector(image_encoder, read_rgb(image))
        log.info("searching for the image %s", image)

    selector = Selector(t, b, k=args["k"], ema=args["ema"], margin=args["margin"], max_send=args["max_send"])
    queue = SendQueue(max_size=args["max_queue"], resend_after_s=args["resend_after_ms"] / 1000)
    return Cliped(
        image_encoder,
        selector,
        queue,
        prompt=args["image"] or args["prompt"],
        verifier=verifier,
        tile_size=args["tile_size"],
        stride=args["stride"],
        log_scores=args["log_scores"],
        tile_log=tile_log,
    )


def stop_on_signals():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return stop


def main_cliped(name):
    try:
        args = parse_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
        image = image_path(args)
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for the cliped engine: {err}")
    if args["quantized"] and args["npu"]:
        ensure_qnn_on_library_path()
    try:
        log = tile_log(args, name)
    except ConfigError as err:
        sys.exit(f"Bad args for the cliped engine: {err}")

    from cliped.engine import max_output_size, qualify, run

    verifier = qualify(os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""), args["verifier"])
    cliped = build(args, verifier, image=image, tile_log=log)
    run(name, cliped, max_output_size=max_output_size(args["tile_size"]), stop=stop_on_signals())


def main_verifier(name):
    from cliped.verifier import DEFAULT_HOST, Ollama, OllamaError, Verifier, encode_png, run

    try:
        args = parse_verifier_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
        image = image_path(args)
        reference = encode_png(read_rgb(image)) if image is not None else None
        log = tile_log(args, name)
    except (ConfigError, FilesError, ValueError) as err:
        sys.exit(f"Bad args for the verifier engine: {err}")
    host = args["host"] or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
    ollama = Ollama(host, args["model"], timeout_s=args["timeout_ms"] / 1000)
    try:
        ollama.ensure_model()
    except OllamaError as err:
        sys.exit(f"Ollama at {ollama.host} can't serve {args['model']}: {err}")
    logging.getLogger("cliped.verifier").info("asking %s at %s", args["model"], ollama.host)
    verifier = Verifier(ollama.ask, label=args["image"] or args["prompt"], reference=reference, tile_log=log)
    run(name, verifier, stop=stop_on_signals())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", CLIPED_PLUGIN)
    plugins = {CLIPED_PLUGIN: main_cliped, VERIFIER_PLUGIN: main_verifier}
    if plugin not in plugins:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {sorted(plugins)}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    plugins[plugin](name)


if __name__ == "__main__":
    main()
