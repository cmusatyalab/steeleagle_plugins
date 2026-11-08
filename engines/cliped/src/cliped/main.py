"""Entrypoint of the cliped engine, which eagled starts with `pixi run start`.

eagled passes the plugin's args in STEELEAGLE_PLUGIN_ARGS (a JSON object), the
name to serve the engine as in STEELEAGLE_ENGINE_ENDPOINT, and the remote
daemon's IP, which qualifies the verifier's name, in
STEELEAGLE_REMOTE_QUALIFIER.
"""
import json
import logging
import os
import signal
import sys
import threading

from cliped.clip import BACKGROUNDS, FP32_WEIGHTS, LIBS_DIR, QUANTIZED_MODEL

# Defaults of the args of cliped.plugin.toml. The daemon passes only the args
# set in its config, so any arg left unset is defaulted here.
DEFAULTS = {
    "prompt": "",
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
    "log_tiles": False,
}
REQUIRED = ("prompt", "verifier")
POSITIVE_INTS = ("tile_size", "stride", "max_send", "k", "max_queue", "threads")

LD_LIBRARY_PATH = "LD_LIBRARY_PATH"
ADSP_LIBRARY_PATH = "ADSP_LIBRARY_PATH"
# Where the Tachyon keeps the DSP's own libraries; ADSP_LIBRARY_PATH replaces
# the default search path, so they must stay on it.
SYSTEM_ADSP_DIRS = ("/usr/lib/rfsa/adsp", "/usr/lib/dsp/cdsp", "/dsp")


class ConfigError(Exception):
    pass


def parse_args(content):
    """The args in the JSON object `content`, with every unset arg defaulted.
    Raises ConfigError for args that are missing or out of range."""
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ConfigError("the args must be a JSON object")
    for name, default in DEFAULTS.items():
        if args.get(name) is None:
            args[name] = default
    for name in REQUIRED:
        if not isinstance(args[name], str) or not args[name].strip():
            raise ConfigError(f"the {name!r} arg must be set")
        args[name] = args[name].strip()
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
    for name in ("quantized", "npu", "log_tiles"):
        if not isinstance(args[name], bool):
            raise ConfigError(f"{name} must be true or false")
    return args


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


def build(args, verifier):
    """The Cliped of `args`, verified by the engine `verifier`."""
    import torch

    from cliped.clip import Encoder, NpuImageEncoder, Selector, text_vectors
    from cliped.engine import Cliped
    from cliped.send_queue import SendQueue

    log = logging.getLogger("cliped")
    torch.set_num_threads(args["threads"])
    # The text encoder always runs FP32 on the CPU, once, for the prompt
    text_encoder = Encoder(visual_weights=None if args["quantized"] else FP32_WEIGHTS)
    t, b = text_vectors(text_encoder, args["prompt"], args["backgrounds"])
    if args["quantized"]:
        qnn_dir = LIBS_DIR if args["npu"] else None
        image_encoder = NpuImageEncoder(QUANTIZED_MODEL, qnn_dir, num_threads=args["threads"])
        log.info("INT8 image encoder %s on the %s", QUANTIZED_MODEL.name, "HTP" if args["npu"] else "CPU")
        del text_encoder  # only its vectors are needed
    else:
        image_encoder = text_encoder
        log.info("FP32 image encoder %s on the CPU", FP32_WEIGHTS.name)

    selector = Selector(t, b, k=args["k"], ema=args["ema"], margin=args["margin"], max_send=args["max_send"])
    queue = SendQueue(max_size=args["max_queue"], resend_after_s=args["resend_after_ms"] / 1000)
    return Cliped(
        image_encoder,
        selector,
        queue,
        prompt=args["prompt"],
        verifier=verifier,
        tile_size=args["tile_size"],
        stride=args["stride"],
        log_tiles=args["log_tiles"],
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    try:
        args = parse_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for the cliped engine: {err}")
    if args["quantized"] and args["npu"]:
        ensure_qnn_on_library_path()

    from cliped.engine import max_output_size, qualify, run

    verifier = qualify(os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""), args["verifier"])
    cliped = build(args, verifier)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(name, cliped, max_output_size=max_output_size(args["tile_size"]), stop=stop)


if __name__ == "__main__":
    main()
