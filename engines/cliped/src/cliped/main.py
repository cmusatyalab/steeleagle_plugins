"""Entrypoint of the cliped plugins, which eagled starts with `pixi run start`:
the edge filter (cliped.plugin.toml), the verifier (verifier.plugin.toml),
and the detection reranker (box_rerank.plugin.toml).

eagled names the plugin to run in STEELEAGLE_PLUGIN, its args in
STEELEAGLE_PLUGIN_ARGS (a JSON object), the name to serve the engine as in
STEELEAGLE_ENGINE_ENDPOINT, the daemon's files directory, which holds image
prompts and the images log_tiles writes, in STEELEAGLE_FILES, and, for the
edge filter and the reranker, the remote daemon's IP, which qualifies the
verifier's name, in STEELEAGLE_REMOTE_QUALIFIER, and, for the reranker, the
local daemon's IP, which qualifies the detector's, in
STEELEAGLE_LOCAL_QUALIFIER.
"""
import json
import logging
import os
import signal
import sys
import threading

from cliped.clip import BACKGROUNDS, FP32_WEIGHTS, LIBS_DIR, MODELS_DIR, QUANTIZED_MODEL
from cliped.files import FilesError, TileLog, files_dir, read_rgb, resolve, resolve_dir

CLIPED_PLUGIN = "cliped.plugin.toml"
VERIFIER_PLUGIN = "verifier.plugin.toml"
BOX_RERANK_PLUGIN = "box_rerank.plugin.toml"

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
    "npu": True,
    "threads": 4,
    "log_scores": False,
    "log_tiles": False,
    "learn": True,
    "learn_similarity": 0.85,
    "confirmations": 2,
    "good_similarity": 0.9,
    "good_confirmations": 2,
    "max_learned": 32,
    "log_sent": "",
    "log_scored": "",
    "defer_similar": False,
    "defer_similar_matches": 40,
    "ignore_new": False,
    "visual_guard": True,
    "propagate_s": 20.0,
}
# Defaults of the args of verifier.plugin.toml
VERIFIER_DEFAULTS = {
    "prompt": "",
    "image": "",
    "model": "qwen3-vl:30b-a3b-instruct",
    "host": "",
    "timeout_ms": 30000,
    "log_tiles": False,
    "question": "",
    "min_side": 0,
    "live": "",
    "heartbeat": 1.0,
    "num_ctx": 4096,
    "image_mode": "class",
    "distractors": [],
    "yes_conf": 0.7,
    "no_conf": 0.9,
}
# Defaults of the args of box_rerank.plugin.toml
RERANK_DEFAULTS = {
    "prompt": "",
    "image": "",
    "detector": "",
    "verifier": "",
    "class": "",
    "classes": [],
    "confidence": 0.5,
    "require_verified": False,
    "max_candidates": 8,
    "context": 1.25,
    "verify_context": 2.0,
    "min_verify_side": 96,
    "max_verify_side": 256,
    "novelty": 0.92,
    "cooldown": 5.0,
    "prompt_weight": 0.0,
    "rank": "prompt",
    "gate": False,
    "class_prompt": "",
    "ignore_new": False,
    "timeout": 1000,
    "backgrounds": BACKGROUNDS,
    "quantized": True,
    "npu": True,
    "threads": 4,
    "max_queue": 64,
    "learn_similarity": 0.85,
    "confirmations": 2,
    "good_similarity": 0.9,
    "good_confirmations": 2,
    "max_learned": 32,
    "log_tiles": False,
    "log_candidates": "",
}
POSITIVE_INTS = ("tile_size", "stride", "max_send", "k", "max_queue", "threads", "confirmations",
                 "good_confirmations", "max_learned", "defer_similar_matches")

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
    # Neither is fine: every frame is dropped until the verifier's prompt
    # heartbeat arrives
    _strings(args, "prompt", "image")
    if args["image"]:
        args["prompt"] = ""
    _strings(args, "verifier")
    if not args["verifier"]:
        raise ConfigError("the 'verifier' arg must be set")
    for name in POSITIVE_INTS:
        value = args[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < 1:
            raise ConfigError(f"{name} must be a positive integer, not {value!r}")
        args[name] = int(value)
    for name in ("margin", "ema", "learn_similarity", "good_similarity"):
        if isinstance(args[name], bool) or not isinstance(args[name], (int, float)):
            raise ConfigError(f"{name} must be a number, not {args[name]!r}")
    if not 0 <= args["ema"] < 1:
        raise ConfigError("ema must be in [0, 1)")
    for name in ("learn_similarity", "good_similarity"):
        if not 0 < args[name] <= 1:
            raise ConfigError(f"{name} must be in (0, 1]")
    backgrounds = args["backgrounds"]
    if (not isinstance(backgrounds, list) or not backgrounds
            or not all(isinstance(bg, str) and bg.strip() for bg in backgrounds)):
        raise ConfigError("backgrounds must be a non-empty list of prompts")
    args["backgrounds"] = [bg.strip() for bg in backgrounds]
    for name in ("quantized", "npu", "log_scores", "log_tiles", "learn", "defer_similar", "ignore_new",
                 "visual_guard"):
        if not isinstance(args[name], bool):
            raise ConfigError(f"{name} must be true or false")
    propagate = args["propagate_s"]
    if isinstance(propagate, bool) or not isinstance(propagate, (int, float)) or propagate < 0:
        raise ConfigError(f"propagate_s must be a number of seconds of at least 0, not {propagate!r}")
    args["propagate_s"] = float(propagate)
    if args["ignore_new"] and not (args["prompt"] or args["image"]):
        raise ConfigError("ignore_new keeps this engine's own prompt: set 'prompt' or 'image'")
    for name in ("log_sent", "log_scored"):
        if not isinstance(args[name], str):
            raise ConfigError(f"{name} must be a file name")
        args[name] = args[name].strip()
    return args


def _positive_int(args, name, minimum=1):
    value = args[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or value < minimum:
        raise ConfigError(f"{name} must be an integer of at least {minimum}, not {value!r}")
    args[name] = int(value)


def _number(args, name, low, high, *, low_open=False):
    """Checks that the arg `name` is a number in [low, high] ((low, high] with
    `low_open`)."""
    value = args[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, not {value!r}")
    if value < low or (low_open and value == low) or value > high:
        raise ConfigError(f"{name} must be in {'(' if low_open else '['}{low}, {high}], not {value!r}")
    args[name] = float(value)


def _backgrounds(args):
    backgrounds = args["backgrounds"]
    if (not isinstance(backgrounds, list) or not backgrounds
            or not all(isinstance(bg, str) and bg.strip() for bg in backgrounds)):
        raise ConfigError("backgrounds must be a non-empty list of prompts")
    args["backgrounds"] = [bg.strip() for bg in backgrounds]


def _booleans(args, *names):
    for name in names:
        if not isinstance(args[name], bool):
            raise ConfigError(f"{name} must be true or false")


def parse_rerank_args(content):
    """The args of box_rerank.plugin.toml in the JSON object `content`, with
    every unset arg defaulted. Raises ConfigError for args that are missing or
    out of range."""
    args = _load(content, RERANK_DEFAULTS)
    # Neither is fine: the most confident box is reported until the
    # verifier's prompt heartbeat arrives
    _strings(args, "prompt", "image")
    if args["image"]:
        args["prompt"] = ""
    _strings(args, "detector", "verifier", "class", "log_candidates")
    for name in ("detector", "verifier", "class"):
        if not args[name]:
            raise ConfigError(f"the {name!r} arg must be set")
    classes = args["classes"]
    if not isinstance(classes, list) or not all(isinstance(c, str) and c.strip() for c in classes):
        raise ConfigError("classes must be a list of class names")
    args["classes"] = [c.strip() for c in classes] or [args["class"]]
    for name in ("max_candidates", "threads", "max_queue", "confirmations", "good_confirmations", "max_learned",
                 "max_verify_side", "timeout"):
        _positive_int(args, name)
    _positive_int(args, "min_verify_side", 0)
    if args["min_verify_side"] > args["max_verify_side"]:
        raise ConfigError("min_verify_side must be at most max_verify_side")
    _number(args, "confidence", 0, 1)
    _number(args, "context", 1, 10)
    _number(args, "verify_context", 1, 10)
    _number(args, "cooldown", 0, 3600)
    _number(args, "prompt_weight", -100, 100)
    from cliped.rerank import RANKS

    if args["rank"] not in RANKS:
        raise ConfigError(f"rank must be one of {', '.join(RANKS)}, not {args['rank']!r}")
    for name in ("novelty", "learn_similarity", "good_similarity"):
        _number(args, name, 0, 1, low_open=True)
    _backgrounds(args)
    _booleans(args, "require_verified", "quantized", "npu", "log_tiles", "gate", "ignore_new")
    _strings(args, "class_prompt")
    if args["gate"] and args["image"]:
        raise ConfigError("gate needs a text prompt: an image's likeness is on another scale than a text's")
    args["class_prompt"] = args["class_prompt"] or f"a {args['classes'][0]}"
    return args


def parse_verifier_args(content):
    """The args of verifier.plugin.toml in the JSON object `content`, with
    every unset arg defaulted."""
    args = _load(content, VERIFIER_DEFAULTS)
    _strings(args, "live")
    if args["live"]:
        # The live directory may hold the prompt instead: checked once it is read
        _strings(args, "prompt", "image")
        if args["image"]:
            args["prompt"] = ""
    else:
        _prompt_or_image(args)
    _strings(args, "model", "host", "question")
    if not args["model"]:
        raise ConfigError("the 'model' arg must be set")
    if args["question"]:
        try:
            args["question"].format(prompt="")
        except (KeyError, IndexError, ValueError) as err:
            raise ConfigError(f"question: only {{prompt}} may be in braces ({err!r})") from None
    timeout = args["timeout_ms"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"timeout_ms must be a positive number, not {timeout!r}")
    if not isinstance(args["log_tiles"], bool):
        raise ConfigError("log_tiles must be true or false")
    if args["image_mode"] not in ("class", "descriptive", "contrastive"):
        raise ConfigError(f"image_mode must be \"class\", \"descriptive\" or \"contrastive\", "
                          f"not {args['image_mode']!r}")
    distractors = args["distractors"]
    if not isinstance(distractors, list) or not all(isinstance(d, str) and d.strip() for d in distractors):
        raise ConfigError(f"distractors must be a list of image files, not {distractors!r}")
    args["distractors"] = [d.strip() for d in distractors]
    _positive_int(args, "min_side", 0)
    _positive_int(args, "num_ctx", 0)
    _number(args, "heartbeat", 0, 3600, low_open=True)
    for name in ("yes_conf", "no_conf"):
        value = args[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 < value <= 1:
            raise ConfigError(f"{name} must be over 0.5 and at most 1, not {value!r}")
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


def check_binaries(args):
    """Raises ConfigError unless the encoder `args` runs, and with npu the QNN
    libraries, are in cliped/ in the daemon's models directory: they are too
    large for the repository, so each host keeps its own copy there."""
    needed = [QUANTIZED_MODEL if args["quantized"] else FP32_WEIGHTS]
    if args["quantized"] and args["npu"]:
        needed.append(LIBS_DIR / "libQnnTFLiteDelegate.so")
    missing = [str(p) for p in needed if not p.is_file()]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)}: put image_encoder_int8.tflite, "
                          f"distill_visual.pt and libs/ (the QNN libraries) in {MODELS_DIR}")


def ensure_qnn_on_library_path():
    """Puts the QNN libraries (LIBS_DIR) where QNN looks for its libraries: on LD_LIBRARY_PATH, which
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


def encoders(args, image=None, texts=(), keep_text=False):
    """The image encoder of `args`, the prompt vector t (of the image file
    `image` if given, of the text prompt if not; None with neither), the
    background vectors b, a vector for each of the prompts `texts`, and, with
    `keep_text`, the text encoder, to embed more prompts later (None
    without)."""
    import torch

    from cliped.clip import Encoder, NpuImageEncoder, image_vector, text_vectors

    log = logging.getLogger("cliped")
    torch.set_num_threads(args["threads"])
    # The text encoder always runs FP32 on the CPU, once, for the prompt
    text_encoder = Encoder(visual_weights=None if args["quantized"] else FP32_WEIGHTS)
    t, b = text_vectors(text_encoder, None if image else args["prompt"], args["backgrounds"])
    extra = [text_vectors(text_encoder, text, args["backgrounds"])[0] for text in texts]
    if args["quantized"]:
        qnn_dir = LIBS_DIR if args["npu"] else None
        image_encoder = NpuImageEncoder(QUANTIZED_MODEL, qnn_dir, num_threads=args["threads"])
        log.info("INT8 image encoder %s on the %s", QUANTIZED_MODEL.name, "HTP" if args["npu"] else "CPU")
        if not keep_text:
            del text_encoder  # only its vectors are needed
            text_encoder = None
    else:
        image_encoder = text_encoder
        log.info("FP32 image encoder %s on the CPU", FP32_WEIGHTS.name)
    if image is not None:
        t = image_vector(image_encoder, read_rgb(image))
        log.info("searching for the image %s", image)
    return image_encoder, t, b, extra, text_encoder if keep_text else None


def build(args, verifier, *, image=None, tile_log=None):
    """The Cliped of `args`, verified by the engine `verifier`, searching for
    the image file `image` if given, for the text prompt if not, and for the
    verifier's prompt once its heartbeat arrives (unless ignore_new)."""
    import numpy as np

    from cliped.clip import Selector, text_vectors
    from cliped.engine import Cliped
    from cliped.send_queue import SendQueue

    log = logging.getLogger("cliped")
    # The text encoder is kept, to embed the verifier's prompt when it changes
    follow = not args["ignore_new"]
    image_encoder, t, b, _, text_encoder = encoders(args, image, keep_text=follow)
    has_prompt = t is not None
    if t is None:
        t = np.zeros(b.shape[1])  # no prompt yet: no frame is scored
    if args["learn"]:
        log.info("learning from the verifier's answers (similarity %.2f, %d no's to gate; good similarity %.2f, "
                 "%d yes's to force; at most %d of each)", args["learn_similarity"], args["confirmations"],
                 args["good_similarity"], args["good_confirmations"], args["max_learned"])

    def selector_for(t):
        return Selector(t, b, k=args["k"], ema=args["ema"], margin=args["margin"], max_send=args["max_send"],
                        learn=args["learn"], similarity=args["learn_similarity"],
                        confirmations=args["confirmations"], max_learned=args["max_learned"],
                        good_similarity=args["good_similarity"], good_confirmations=args["good_confirmations"])

    def retarget(text):
        return selector_for(text_vectors(text_encoder, text, args["backgrounds"])[0])

    selector = selector_for(t)
    from cliped.sent_log import ScoredLog, SentLog

    queue = SendQueue(max_size=args["max_queue"])
    similar = None
    if args["defer_similar"]:
        from cliped.similar import SimilarFilter

        similar = SimilarFilter(args["defer_similar_matches"])
        log.info("tiles like one sent before (%d keypoints matching) go behind the new ones",
                 args["defer_similar_matches"])
    visual = None
    if args["learn"] and (args["visual_guard"] or args["propagate_s"] > 0):
        from cliped.similar import SimilarFilter

        visual = similar or SimilarFilter(args["defer_similar_matches"])
        log.info("learning: %s; %s", "a no is guarded only if its tile is visually a tile said yes to"
                 if args["visual_guard"] else "the cosine guard", f"each answer is also learned for the tiles "
                 f"picked in the last {args['propagate_s']:g}s that are visually its tile"
                 if args["propagate_s"] > 0 else "no propagation")
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
        sent_log=SentLog(args["log_sent"]) if args.get("log_sent") else None,
        scored_log=ScoredLog(args["log_scored"]) if args.get("log_scored") else None,
        similar=similar,
        has_prompt=has_prompt,
        retarget=retarget if follow else None,
        ignore_new=args["ignore_new"],
        visual=visual,
        visual_guard=args["visual_guard"],
        propagate_s=args["propagate_s"],
    )


def build_rerank(args, detector, verifier, *, image=None, tile_log=None):
    """The Reranker of `args`, reranking the detections of the engine
    `detector`, verified by the engine `verifier`."""
    import numpy as np

    from cliped.clip import Selector, text_vectors
    from cliped.instance_gate import InstanceGate
    from cliped.rerank import CandidateLog, Reranker
    from cliped.send_queue import SendQueue

    # The text encoder is kept, to embed the verifier's prompt when it changes
    image_encoder, t, b, extra, text_encoder = encoders(
        args, image, [args["class_prompt"]] if args["gate"] else [], keep_text=True)
    has_prompt = t is not None
    if t is None:
        t = np.zeros(b.shape[1])  # no prompt yet: s is unused

    def selector_for(t):
        return Selector(t, b, learn=True, similarity=args["learn_similarity"],
                        confirmations=args["confirmations"], max_learned=args["max_learned"],
                        good_similarity=args["good_similarity"], good_confirmations=args["good_confirmations"])

    def gate_for(t, c=None):
        return InstanceGate(t, extra[0] if c is None else c) if args["gate"] else None

    def retarget(text, class_prompt=None):
        # The verifier's class prompt for the gate if it sent one, else class_prompt
        t, _ = text_vectors(text_encoder, text, args["backgrounds"])
        c = None
        if args["gate"] and class_prompt:
            c, _ = text_vectors(text_encoder, class_prompt, args["backgrounds"])
        return selector_for(t), gate_for(t, c)

    gate = gate_for(t) if has_prompt else None
    if args["gate"]:
        logging.getLogger("cliped").info("gate: learning a cutoff on likeness to %r over %r",
                                         args["prompt"] or "the verifier's prompt", args["class_prompt"])
    selector = selector_for(t)
    return Reranker(
        image_encoder,
        selector,
        SendQueue(max_size=args["max_queue"]),
        detector=detector,
        verifier=verifier,
        class_name=args["class"],
        classes=args["classes"],
        confidence=args["confidence"],
        max_candidates=args["max_candidates"],
        context=args["context"],
        verify_context=args["verify_context"],
        min_verify_side=args["min_verify_side"],
        max_verify_side=args["max_verify_side"],
        novelty=args["novelty"],
        cooldown_s=args["cooldown"],
        require_verified=args["require_verified"],
        prompt_weight=args["prompt_weight"],
        rank=args["rank"],
        gate=gate,
        prompt=args["prompt"] or None,
        has_prompt=has_prompt,
        retarget=retarget,
        ignore_new=args["ignore_new"],
        timeout_ns=args["timeout"] * 1_000_000,
        tile_log=tile_log,
        candidate_log=CandidateLog(args["log_candidates"]) if args["log_candidates"] else None,
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
        check_binaries(args)
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
    clog = logging.getLogger("cliped")
    if cliped.ignore_new:
        clog.info("ignore_new: keeping this prompt, whatever the verifier's is")
    elif not cliped.has_prompt:
        clog.info("no prompt: dropping every frame until the verifier's prompt heartbeat arrives")
    run(name, cliped, max_output_size=max_output_size(args["tile_size"]), stop=stop_on_signals())


def main_box_rerank(name):
    try:
        args = parse_rerank_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
        image = image_path(args)
        check_binaries(args)
    except (ConfigError, ValueError) as err:
        sys.exit(f"Bad args for the box_rerank engine: {err}")
    if args["quantized"] and args["npu"]:
        ensure_qnn_on_library_path()
    try:
        log = tile_log(args, name)
    except ConfigError as err:
        sys.exit(f"Bad args for the box_rerank engine: {err}")

    from cliped.engine import qualify
    from cliped.rerank import run

    detector = qualify(os.environ.get("STEELEAGLE_LOCAL_QUALIFIER", ""), args["detector"])
    verifier = qualify(os.environ.get("STEELEAGLE_REMOTE_QUALIFIER", ""), args["verifier"])
    reranker = build_rerank(args, detector, verifier, image=image, tile_log=log)
    rlog = logging.getLogger("cliped.rerank")
    rlog.info(
        "reporting %r: unverified boxes from confidence %.2f%s; a no vetoes after %d, a yes confirms after %d",
        args["class"], args["confidence"], " (never: require_verified)" if args["require_verified"] else "",
        args["confirmations"], args["good_confirmations"])
    if reranker.ignore_new:
        rlog.info("ignore_new: keeping this prompt, whatever the verifier's is")
    elif not reranker.has_prompt:
        rlog.info("no prompt: reporting the most confident box until the verifier's prompt heartbeat arrives")
    run(name, reranker, stop=stop_on_signals())


def main_verifier(name):
    from cliped.verifier import (DEFAULT_HOST, LiveDir, Ollama, OllamaError, PromptKeeper, Verifier, encode_png,
                                 run, shrink)

    def load_image(path):
        return encode_png(shrink(read_rgb(path)))

    try:
        args = parse_verifier_args(os.environ.get("STEELEAGLE_PLUGIN_ARGS"))
        image = image_path(args)
        reference = load_image(image) if image is not None else None
        distractors = [(name, load_image(resolve(files_dir(), name))) for name in args["distractors"]]
        log = tile_log(args, name)
        live = LiveDir(resolve_dir(files_dir(), args["live"])) if args["live"] else None
    except (ConfigError, FilesError, ValueError) as err:
        sys.exit(f"Bad args for the verifier engine: {err}")
    host = args["host"] or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
    ollama = Ollama(host, args["model"], timeout_s=args["timeout_ms"] / 1000, num_ctx=args["num_ctx"])
    try:
        ollama.ensure_model()
    except OllamaError as err:
        sys.exit(f"Ollama at {ollama.host} can't serve {args['model']}: {err}")
    vlog = logging.getLogger("cliped.verifier")
    vlog.info("asking %s at %s", args["model"], ollama.host)
    if "image_descriptive" in args:
        vlog.warning("image_descriptive is no longer an arg: image_mode = \"descriptive\" is what true was; "
                     "ignoring image_descriptive = %r", args["image_descriptive"])
    if distractors and args["image_mode"] != "contrastive":
        vlog.warning("distractors are only shown with image_mode = \"contrastive\"; this is %r",
                     args["image_mode"])
    verifier = Verifier(ollama.ask, label=args["image"] or args["prompt"], reference=reference, tile_log=log,
                        question=args["question"] or None, min_side=args["min_side"],
                        image_mode=args["image_mode"], distractors=distractors, yes_conf=args["yes_conf"],
                        no_conf=args["no_conf"])
    keeper = PromptKeeper(verifier, live=live, load_image=load_image, interval_s=args["heartbeat"])
    if live is not None:
        vlog.info("live: watching %s", live.path)
        # What the directory holds now wins over the args
        keeper.update()
    if not verifier.has_prompt:
        sys.exit("Bad args for the verifier engine: no prompt: set 'prompt' or 'image', or put "
                 "prompt.txt or prompt.jpg in the live directory")
    run(name, verifier, stop=stop_on_signals(), keeper=keeper)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    plugin = os.environ.get("STEELEAGLE_PLUGIN", CLIPED_PLUGIN)
    plugins = {CLIPED_PLUGIN: main_cliped, VERIFIER_PLUGIN: main_verifier, BOX_RERANK_PLUGIN: main_box_rerank}
    if plugin not in plugins:
        sys.exit(f"Unrecognized plugin {plugin!r}; expected one of {sorted(plugins)}")
    name = os.environ.get("STEELEAGLE_ENGINE_ENDPOINT")
    if not name:
        sys.exit("STEELEAGLE_ENGINE_ENDPOINT is not set")
    plugins[plugin](name)


if __name__ == "__main__":
    main()
