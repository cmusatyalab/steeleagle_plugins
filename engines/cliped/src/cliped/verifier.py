"""The verifier engine: asks a vision-language model served by Ollama (Qwen3-VL
30B-A3B Instruct by default) whether each RawFrame matches the prompt, forwards the frame
if it does and drops it if not.

The prompt is always text. A reference image is turned into text once, when
it is set, by image_mode: "class", just what kind of thing it is
(CATEGORY_QUESTION, e.g. "a quadruped robot dog"); "descriptive", a short
description of what it shows with its noticeable colors and distinguishing
features visible from far above (DESCRIPTIVE_QUESTION, e.g. "Mannequin in dark
green shirt and black pants"); or "contrastive", a description that tells it
apart from distractors, images of other things of its kind to avoid
(CONTRASTIVE_QUESTION; descriptive's while there are none). The model is never
shown the image with a frame.

The answer is read from the model's probabilities of yes and no for its
first token: a yes at least yes_conf likely, a no at least no_conf likely,
and anything between is unsure. A yes is forwarded with the GenericResult
{"match": true}, a no dropped with {"match": false}, and an unsure frame
forwarded with {"match": null, "unsure": true}, so cliped's send queue sees a
RESULT event for each tile it sent; cliped and box_rerank learn from a yes or
a no, but not from an unsure answer, so a hard view of the target the model
hesitates on isn't taught to them as background. Each result also holds
"p_yes" and "p_no". If the server gives no probabilities, the model's word
decides.

With a live directory (LiveDir), the question and the prompt can change while
it runs: question.txt, and prompt.txt or prompt.jpg/.jpeg, are read when it
starts and again whenever they change, and so are the distractors d<N>.jpg,
which join the distractors arg's. class.txt, written with an image (the GCS
writes the tracked box's detector class there just before prompt.jpg), is
that image's kind, so the model isn't asked it. The text prompt it verifies against (the
image's text for an image) is written there as description.txt whenever it
changes, so the GCS checks a tracked box against the same text. Every heartbeat (PromptKeeper), it
publishes a DERIVED event of type PROMPT_TYPE holding, as UTF-8 JSON, the
prompt's class (an image's kind, from CATEGORY_QUESTION; null for a text
prompt) and its description (the text prompt, or an image's description with
image_mode "descriptive" or "contrastive"; null with "class"). box_rerank
follows it: CLIP ranks its boxes by the description, else the class, its gate
measures likeness to that over the class, and it starts learning over when
either changes.

Nothing here imports torch: the verifier runs on a server without the edge
filter's models.
"""
import base64
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import uriel

from cliped.engine import GENERIC_RESULT_TYPE, POLL_INTERVAL_S, RAW_FRAME_TYPE, FrameError, read_frame
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped.verifier")

# A mixture of experts: 30B parameters, ~3B active per token, so it answers as
# fast as the 8B (317 ms against 358 ms per crop on an RTX 4090) and fits in
# 19 GB at NUM_CTX. On 147 labeled live crops of a robot dog it said yes to
# 46 of 67 dog crops (the 8B: 18) and to 4 of 80 others (the 8B: 2), and kept
# finding the dog in poor light, where the 8B found none
DEFAULT_MODEL = "qwen3-vl:30b-a3b-instruct"
DEFAULT_HOST = "http://localhost:11434"
# Room for the result; the verifier forwards its input as is.
MAX_OUTPUT_SIZE = 64 << 10
# How long pulling the model may take, the first time it runs on a server
PULL_TIMEOUT_S = 3600
# The type of the heartbeat's DERIVED events: UTF-8 JSON {"class": ...,
# "description": ...} (heartbeat_payload)
PROMPT_TYPE = "cliped/Prompt"
# Seconds between heartbeats
HEARTBEAT_S = 1.0
# The live directory's files: the question, and the prompt, an image (which
# wins over the text when both are there) or text
QUESTION_FILE = "question.txt"
PROMPT_TEXT_FILE = "prompt.txt"
PROMPT_IMAGE_FILES = ("prompt.jpg", "prompt.jpeg")
# An image prompt's class (e.g. "person"), written with it; taken as its kind
# when it is at most CLASS_FILE_MAX_AGE_S older than the image
CLASS_FILE = "class.txt"
CLASS_FILE_MAX_AGE_S = 10.0
# The verifier's text prompt, which it writes to the live directory
DESCRIPTION_FILE = "description.txt"
# The live directory's distractors: d1.jpg, d2.jpg, ...
DISTRACTOR_PATTERN = re.compile(r"d(\d+)\.jpe?g")
# The modes of turning an image prompt into text
IMAGE_MODES = ("class", "descriptive", "contrastive")
# The most distractors shown with the prompt image, the newest: each costs
# the model an image's tokens, and NUM_CTX holds a few at REFERENCE_MAX_SIDE
MAX_DISTRACTORS = 5
# Asked about an image prompt once, with image_mode "descriptive": what it shows, with
# its noticeable colors and the features visible from far above, as the text
# prompt of both the verifier and CLIP (box_rerank, through the heartbeat).
# What it is doing, where it is and small details are left out: a drone
# can't see them, and CLIP ranks by a long phrase poorly (on mannequin tiles,
# a 25-word description with brands and shoes made cliped send almost
# nothing, where "mannequin wearing dark green shirt and black pants" doubled
# the precision of the image prompt). Qwen3-VL 30B-A3B gave "Mannequin in
# dark green shirt and black pants with white stripes", "Gray robotic dog with
# four legs and a camera on its head" and "Person in red shirt and dark
# shorts walking".
DESCRIPTIVE_QUESTION = (
    "Describe the main person or object in this image as a short phrase for finding it in a low-resolution photo "
    "taken by a drone from far above: what it is, then its most noticeable colors and any distinguishing features "
    "large enough to see from far away. Leave out what it is doing, where it is and the background, and small "
    "details such as logos, brands, text, small patterns, shoes and accessories. Answer with the phrase only, "
    "under 12 words."
)
# Asked about an image prompt once, with image_mode "class": the kind of
# thing it shows, in a few words. Asking whether a crop is a kind of thing
# rejects look-alikes of other kinds: on 147 labeled live crops of a robot dog
# (Qwen3-VL 30B-A3B), a photo of the dog was named "a quadruped robot dog",
# which said yes to 48 of 67 dog crops and to 4 of 80 others; the photo
# itself, shown with each crop, to 53 and 25, and a detailed description
# ("a gray quadruped robot dog with white markings and black feet") to 67 and
# 43. It can't tell individuals of one kind apart (two people are both "a
# person"): "descriptive" and "contrastive" are for that.
CATEGORY_QUESTION = (
    "Name what kind of object or person this image shows, in 2 to 5 words, e.g. 'a person' or "
    "'a quadruped robot dog'. Name only its kind, not its colors, markings or parts. "
    "Answer with the phrase only."
)
# Asked about an image prompt once, with image_mode "contrastive" and
# distractors: the first image is the target, the rest ({n} of them) other
# things of its kind the drone must not confuse with it. The description is
# CLIP's prompt too, and CLIP can't read negation ("not in a gray shirt" ranks
# gray shirts up), so it names what sets the target apart, never what it isn't.
CONTRASTIVE_QUESTION = (
    "The first image shows the target: a person or object to find in low-resolution photos taken by a drone from "
    "far above. The other {n} image(s) show distractors: other people or objects of the same kind nearby, which "
    "must not be confused with the target. Write a short phrase describing the target for CLIP, an image-text "
    "model that finds images matching a phrase. CLIP does not understand negation, so never say what the target "
    "is not and never mention the distractors. Instead, say what the target is, then name the colors and "
    "features of the target that differ from the distractors, as specifically as you can (for example 'blue "
    "jeans' rather than 'light pants'), only ones large enough to see from far away. Leave out what it is doing, "
    "where it is and the background, and small details such as logos, brands, text, shoes and accessories. "
    "Answer with the phrase only, under 12 words."
)
# Tokens a description may take
DESCRIBE_TOKENS = 40
# The defaults of yes_conf and no_conf. Qwen3-VL 30B leans to no: on 273
# mannequin tiles, every tile between P(yes) 0.3 and 0.7 was a real
# mannequin, and 117 of 120 other tiles had P(no) at least 0.9
YES_CONF = 0.7
NO_CONF = 0.9
# Tokens of context the model is loaded with. A request is one or two small
# images, a question and an answer of a few words, well under 4096; Ollama's
# default (the model's full context, 262144 for Qwen3-VL) reserves a KV cache
# that took qwen3-vl:8b-instruct to 42 GB of GPU memory, against 5.8 GB at
# 4096, and leaves no room for a larger model on lambda's two 24 GB GPUs
NUM_CTX = 4096
# Longest side of the reference image sent to the model, in pixels. A phone
# photo at full size costs a 30 MB PNG and twice the prompt tokens on every
# frame, for no better answer.
REFERENCE_MAX_SIDE = 448

# Most likely first tokens asked for, to read the probability of yes and of no
TOP_LOGPROBS = 20
# The verdicts
YES, NO, UNSURE = "yes", "no", "unsure"

TEXT_QUESTION = (
    "This is a tile cut from an aerial drone photo. Does it show {prompt}? "
    "Answer with only yes or no."
)
# For box_rerank's crops: a detection with context around it, its box drawn
# in red. The context helps the model recognize a small object, and the box
# says which object is asked about when another (e.g. the target next to a
# distractor) is in the crop too.
BOX_QUESTION = (
    "This image is cut from an aerial drone video. Is the object inside the red box {prompt}? "
    "Answer with only yes or no."
)

class OllamaError(Exception):
    pass


class Ollama:
    """A client of the Ollama server at `host`, for `model`, loaded with a
    context of `num_ctx` tokens (0 for the server's default)."""

    def __init__(self, host=DEFAULT_HOST, model=DEFAULT_MODEL, timeout_s=30.0, num_ctx=NUM_CTX):
        self.host = host.rstrip("/")
        if "://" not in self.host:
            self.host = f"http://{self.host}"
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx

    def _post(self, path, body, timeout_s):
        request = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace").strip()
            raise OllamaError(f"{path}: HTTP {err.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise OllamaError(f"{path}: {err}") from None

    def ensure_model(self):
        """Pulls the model if the server does not have it."""
        try:
            self._post("/api/show", {"model": self.model}, self.timeout_s)
            return
        except OllamaError as err:
            if "HTTP 404" not in str(err):
                raise
        logger.info("pulling %s from the Ollama registry; this can take a while", self.model)
        status = self._post("/api/pull", {"model": self.model, "stream": False}, PULL_TIMEOUT_S)
        if status.get("status") != "success":
            raise OllamaError(f"pulling {self.model}: {status}")

    def ask(self, question, images, max_tokens=8, scores=False):
        """The model's answer to `question` about `images` (encoded image
        files, e.g. PNG or JPEG bytes), of at most `max_tokens` tokens. With
        `scores`, (answer, p_yes, p_no): the answer and the probabilities of
        its first token being yes and no (answer_scores), or None for both if
        the server gave no probabilities (Ollama before 0.12.11)."""
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": question,
                "images": [base64.b64encode(image).decode() for image in images],
            }],
            "stream": False,
            "keep_alive": -1,  # keep it loaded between frames
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if scores:
            body["logprobs"] = True
            body["top_logprobs"] = TOP_LOGPROBS
        if self.num_ctx:
            # Every request asks for the same context: Ollama reloads the
            # model when it changes
            body["options"]["num_ctx"] = self.num_ctx
        reply = self._post("/api/chat", body, self.timeout_s)
        try:
            answer = reply["message"]["content"]
        except (KeyError, TypeError):
            raise OllamaError(f"/api/chat: unexpected reply {reply!r}") from None
        if not scores:
            return answer
        return (answer, *answer_scores(reply.get("logprobs")))


def answer_scores(logprobs):
    """The probabilities of yes and of no as the first token of an answer,
    from Ollama's `logprobs` (a list of tokens, each with its top_logprobs):
    the sum over the alternatives that are yes (or no) in any case or
    spacing. (None, None) without them."""
    try:
        top = logprobs[0]["top_logprobs"]
    except (IndexError, KeyError, TypeError):
        return None, None
    p = {YES: 0.0, NO: 0.0}
    for candidate in top:
        try:
            word = str(candidate["token"]).strip().lower()
            if word in p:
                p[word] += math.exp(float(candidate["logprob"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(1.0, p[YES]), min(1.0, p[NO])


def verdict(p_yes, p_no, yes_conf, no_conf):
    """YES if `p_yes` is at least `yes_conf`, NO if `p_no` is at least
    `no_conf`, else UNSURE."""
    if p_yes >= yes_conf:
        return YES
    if p_no >= no_conf:
        return NO
    return UNSURE


def clean_description(text):
    """The model's description as one line, without quotes or a full stop."""
    return " ".join(text.split()).strip("\"'`*. ")


def class_phrase(name):
    """A class name as a prompt phrase, as box_rerank's class_prompt is
    made: "person" -> "a person"; one with an article already as is."""
    name = " ".join(str(name).split())
    if not name or name.lower().split()[0] in ("a", "an", "the"):
        return name
    return f"a {name}"


def parse_answer(answer):
    """True for a yes, False for a no, None for anything else."""
    word = answer.strip().lstrip("*\"'`").lower()
    if word.startswith("yes"):
        return True
    if word.startswith("no"):
        return False
    return None


def shrink(rgb, max_side=REFERENCE_MAX_SIDE):
    """`rgb` scaled down, keeping its aspect ratio, so its longer side is at
    most `max_side` pixels; as is if it already fits."""
    height, width = rgb.shape[:2]
    scale = max_side / max(height, width)
    if scale >= 1:
        return rgb
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)


def enlarge(rgb, min_side):
    """`rgb` scaled up, keeping its aspect ratio, so its shorter side is at
    least `min_side` pixels; as is if it already is, or if `min_side` is 0. A
    crop is sent small to save the link, and enlarged here, where it costs
    nothing, so the model sees the object over more patches."""
    height, width = rgb.shape[:2]
    if not min_side or min(height, width) >= min_side:
        return rgb
    scale = min_side / min(height, width)
    return cv2.resize(rgb, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_CUBIC)


def encode_png(rgb):
    ok, data = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise FrameError("failed to encode the frame for the model")
    return data.tobytes()


class Verifier:
    """Decides whether inputs match the prompt. `ask(question, images,
    max_tokens=..., scores=...)` is the model (Ollama.ask). With `reference`,
    an encoded image file named `label`, the prompt is the model's text for
    the image, by `image_mode` (IMAGE_MODES): its kind (CATEGORY_QUESTION),
    a short description (DESCRIPTIVE_QUESTION), or a description that tells it
    apart from the distractors (CONTRASTIVE_QUESTION; descriptive's while there
    are none), asked as soon as the image is set, at startup or from the live
    directory, and with "contrastive" again when the distractors change; if
    the model can't be asked then, the next frame or heartbeat asks again.
    Without one, the prompt is the text `label`.

    `distractors` is a list of (name, encoded image) always shown with a
    contrastive question; set_live_distractors adds the live directory's.

    `tile_log` (files.TileLog), if given, gets each input and each frame
    forwarded. `question`, if given, replaces TEXT_QUESTION, with {prompt} in
    it replaced by the text prompt: wording for one target can tell it from
    look-alikes better than the generic question; "box" is BOX_QUESTION. Each
    frame is enlarged to at least `min_side` pixels on its shorter side
    before the model sees it (0 for as is).

    A frame is a yes when the model's P(yes) is at least `yes_conf`, a no when
    its P(no) is at least `no_conf`, and unsure otherwise (verdict): an unsure
    frame is forwarded with {"match": null, "unsure": true}, so it is not
    learned from. Both are over 0.5 and at most 1. If the model gives no
    probabilities, its word decides.

    `description` is the text prompt, for the question and CLIP: the label of
    a text prompt, or the image's text once the model has given it (None
    until then). `kind` is an image's kind (CATEGORY_QUESTION), CLIP's class
    prompt, asked in every image_mode; None for a text prompt."""

    def __init__(self, ask, *, label, reference=None, tile_log=None, question=None, min_side=0,
                 image_mode="class", distractors=(), yes_conf=YES_CONF, no_conf=NO_CONF):
        for name, value in (("yes_conf", yes_conf), ("no_conf", no_conf)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 < value <= 1:
                raise ValueError(f"{name} must be over 0.5 and at most 1, not {value!r}")
        if image_mode not in IMAGE_MODES:
            raise ValueError(f"image_mode must be one of {', '.join(IMAGE_MODES)}, not {image_mode!r}")
        self.ask = ask
        self.tile_log = tile_log
        self.min_side = min_side
        self.image_mode = image_mode
        self.distractors = list(distractors)
        self.live_distractors = []
        self.yes_conf = yes_conf
        self.no_conf = no_conf
        self.frames = 0
        self.matched = 0
        self.unsure = 0
        self._warned_no_scores = False
        self.set_prompt(label, reference)
        self.set_question(question)

    @property
    def has_prompt(self):
        return self.reference is not None or bool(self.label)

    def set_prompt(self, label, reference=None, kind=None):
        """Verifies against the text of the image `reference` (encoded), named
        `label`, from now on, or the text `label` without one. `kind` is the
        image's kind if known (e.g. "person", from the GCS's class.txt), so
        the model isn't asked it."""
        self.label = label
        self.reference = reference
        self.kind = class_phrase(kind) if reference is not None and kind else None
        if self.kind:
            logger.info("the image %s is %r: CLIP's class prompt (given)", label, self.kind)
        self.description = None if reference is not None else (label or None)
        if reference is not None:
            try:
                self.describe_image()
            except Exception as err:
                logger.warning("couldn't describe the image %s (%s); asking again at the next frame", label, err)

    def set_question(self, question):
        """Asks `question` from now on, or the default one for None or "". """
        self.custom_question = question or None

    def set_live_distractors(self, distractors):
        """The live directory's distractors from now on, a list of (name,
        encoded image); with "contrastive", the image is described again."""
        self.live_distractors = list(distractors)
        if self.image_mode == "contrastive" and self.reference is not None:
            self.description = None

    def shown_distractors(self):
        """The distractors a contrastive question shows: the arg's, then the
        live directory's, the newest MAX_DISTRACTORS of them."""
        return (self.distractors + self.live_distractors)[-MAX_DISTRACTORS:]

    def describe_image(self):
        """The text prompt, asking the model for the image's if it has not
        given it yet. Raises OllamaError if it gives none."""
        if self.description is None and self.reference is not None:
            if self.kind is None:
                # Its kind first, in every mode: CLIP's class prompt
                kind = clean_description(self.ask(CATEGORY_QUESTION, [self.reference], max_tokens=DESCRIBE_TOKENS))
                if not kind:
                    raise OllamaError(f"the model gave no kind for the image {self.label}")
                self.kind = kind
                if self.image_mode != "class":
                    logger.info("the image %s is %r: CLIP's class prompt", self.label, kind)
            if self.image_mode == "class":
                self.description = self.kind
                logger.info("the image %s is %r: verifying frames against that (its kind)", self.label, self.kind)
                return self.description
            images, how = [self.reference], ""
            if self.image_mode == "contrastive" and (shown := self.shown_distractors()):
                question = CONTRASTIVE_QUESTION.format(n=len(shown))
                images += [image for _, image in shown]
                how = f" (told apart from {', '.join(name for name, _ in shown)})"
            else:
                question = DESCRIPTIVE_QUESTION
                if self.image_mode == "contrastive":
                    how = " (no distractors yet: described alone)"
            text = clean_description(self.ask(question, images, max_tokens=DESCRIBE_TOKENS))
            if not text:
                raise OllamaError(f"the model gave no text for the image {self.label}")
            self.description = text
            logger.info("the image %s is %r: verifying frames against that%s", self.label, text, how)
        return self.description

    def heartbeat_payload(self):
        """The heartbeat's payload, UTF-8 JSON: "class", an image's kind (null
        for a text prompt), and "description", the text CLIP ranks by when
        set (null for an image with image_mode "class", whose kind is its
        text); None while there is no text prompt."""
        if not self.description:
            return None
        image = self.reference is not None
        payload = {"class": self.kind if image else None,
                   "description": None if image and self.image_mode == "class" else self.description}
        return json.dumps(payload).encode("utf-8")

    def question(self, png):
        """The question about the encoded frame `png`, and the images it
        shows: the frame alone."""
        custom = self.custom_question
        if custom == "box":
            custom = BOX_QUESTION
        return (custom or TEXT_QUESTION).format(prompt=self.describe_image()), [png]

    def on_input(self, input):
        """Whether to forward `input`, and the serialized GenericResult.
        Raises FrameError if `input` is not an RGB RawFrame, and OllamaError
        if the model could not be asked."""
        frame, rgb = read_frame(bytes(input.data))
        if self.tile_log is not None:
            self.tile_log.input(frame.id, rgb)
        started = time.perf_counter()
        question, images = self.question(encode_png(enlarge(rgb, self.min_side)))
        answer, p_yes, p_no = self.ask(question, images, scores=True)
        result = {}
        if p_yes is None:
            if not self._warned_no_scores:
                logger.warning("the model gave no probabilities of yes and no; taking its answers as they are")
                self._warned_no_scores = True
            match = parse_answer(answer)
            if match is None:
                logger.warning("frame %d: the model answered %r, not yes or no; dropping it", frame.id, answer)
            decided = YES if match else NO
        else:
            decided = verdict(p_yes, p_no, self.yes_conf, self.no_conf)
            result = {"p_yes": round(p_yes, 4), "p_no": round(p_no, 4)}
        unsure = decided == UNSURE
        forward = decided != NO
        self.frames += 1
        self.matched += decided == YES
        self.unsure += unsure
        if forward and self.tile_log is not None:
            self.tile_log.output(frame.id, 0, rgb)

        what = {YES: "match, forwarding it", NO: "no match, dropping it",
                UNSURE: "unsure, forwarding it unlearned"}[decided]
        if result:
            what += f" (p yes {result['p_yes']:.2f}, no {result['p_no']:.2f})"
        logger.info("frame %d (%dx%d): %s in %.0f ms; %d of %d matched, %d unsure", frame.id, rgb.shape[1],
                    rgb.shape[0], what, (time.perf_counter() - started) * 1e3, self.matched, self.frames,
                    self.unsure)
        if unsure:
            result = {"match": None, "unsure": True, **result}
        else:
            result = {"match": decided == YES, **result}
        return forward, result_pb2.GenericResult(payload=json.dumps(result).encode()).SerializeToString()


def valid_question(question):
    """Whether `question` is a usable question: only {prompt} in braces."""
    try:
        question.format(prompt="")
    except (KeyError, IndexError, ValueError):
        return False
    return True


class LiveDir:
    """The live directory `path`: the question and prompt files, as they
    change. A file that is removed changes nothing: the verifier keeps what it
    had."""

    def __init__(self, path):
        self.path = Path(path)
        self._question = None  # the (mtime_ns, size) of question.txt last read
        self._prompt = None  # (file name, (mtime_ns, size)) of the prompt last read

    def _signature(self, name):
        try:
            stat = (self.path / name).stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def poll(self):
        """What changed since the last poll: (question, prompt). question is
        question.txt's text if it is new or changed, else None. prompt is the
        prompt file's if it is new or changed, else None: ("image", path), the
        newest of prompt.jpg and prompt.jpeg if either is there, or ("text",
        text) of prompt.txt. A prompt the caller couldn't use can be read
        again with forget_prompt."""
        question = None
        signature = self._signature(QUESTION_FILE)
        if signature is not None and signature != self._question:
            try:
                question = (self.path / QUESTION_FILE).read_text(encoding="utf-8").strip()
                self._question = signature
            except (OSError, UnicodeDecodeError) as err:
                logger.warning("live: can't read %s: %s", QUESTION_FILE, err)
        images = [(signature, name) for name in PROMPT_IMAGE_FILES
                  if (signature := self._signature(name)) is not None]
        if images:
            signature, name = max(images)
        elif (signature := self._signature(PROMPT_TEXT_FILE)) is not None:
            name = PROMPT_TEXT_FILE
        else:
            return question, None
        if (name, signature) == self._prompt:
            return question, None
        self._prompt = (name, signature)
        if name == PROMPT_TEXT_FILE:
            try:
                text = (self.path / name).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as err:
                logger.warning("live: can't read %s: %s", name, err)
                self._prompt = None
                return question, None
            return question, (("text", text) if text else None)
        return question, ("image", self.path / name)

    def forget_prompt(self):
        self._prompt = None

    def poll_distractors(self):
        """The distractor files (d<N>.jpg), oldest first, if the set of them
        or any of them changed since the last poll; else None."""
        try:
            names = [p.name for p in self.path.iterdir() if DISTRACTOR_PATTERN.fullmatch(p.name)]
        except OSError:
            names = []
        found = sorted((self._signature(name), name) for name in names)
        found = [(signature, name) for signature, name in found if signature is not None]
        if found == getattr(self, "_distractors", []):
            return None
        self._distractors = found
        return [self.path / name for _, name in found]

    def forget_distractors(self):
        self._distractors = None

    def read_class(self, image):
        """The class written with the image prompt `image` (a path in the
        directory): class.txt's text if it is no more than
        CLASS_FILE_MAX_AGE_S older than the image, else None."""
        try:
            written = (self.path / CLASS_FILE).stat().st_mtime
            if written < Path(image).stat().st_mtime - CLASS_FILE_MAX_AGE_S:
                return None
            return " ".join((self.path / CLASS_FILE).read_text(encoding="utf-8").split()) or None
        except (OSError, UnicodeDecodeError):
            return None

    def write_description(self, text):
        """Writes `text` to description.txt, whole (through a temporary file
        renamed into place), so a reader never sees half of it."""
        self.path.mkdir(parents=True, exist_ok=True)
        temp = self.path / f".{DESCRIPTION_FILE}.tmp"
        temp.write_text(text + "\n", encoding="utf-8")
        temp.replace(self.path / DESCRIPTION_FILE)


class PromptKeeper:
    """Keeps the verifier's prompt and question up to date with the live
    directory `live` (a LiveDir, or None), and gives the heartbeat's payload
    every `interval_s` seconds (Verifier.heartbeat_payload). An image prompt is
    read with `load_image(path)`, which returns it encoded (and raises on a
    file it can't read); the verifier turns it into text, and if that fails,
    it is asked again at the next heartbeat."""

    def __init__(self, verifier, *, live=None, load_image=None, interval_s=HEARTBEAT_S):
        self.verifier = verifier
        self.live = live
        self.load_image = load_image
        self.interval_s = interval_s
        self._next = None
        self._written = None  # the description last written to the live directory

    def update(self):
        """Applies what changed in the live directory, if any; returns
        whether anything did."""
        if self.live is None:
            return False
        question, prompt = self.live.poll()
        changed = False
        distractors = self.live.poll_distractors()
        if distractors is not None:
            try:
                loaded = [(path.name, self.load_image(path)) for path in distractors]
            except Exception as err:
                logger.warning("live: can't read a distractor (%s); trying again", err)
                self.live.forget_distractors()
            else:
                self.verifier.set_live_distractors(loaded)
                logger.info("live: distractors now %s", ", ".join(name for name, _ in loaded) or "none")
                changed = True
        if question is not None:
            if valid_question(question):
                self.verifier.set_question(question)
                logger.info("live: question now %r", question or "(the default)")
                changed = True
            else:
                logger.warning("live: ignoring question %r: only {prompt} may be in braces", question)
        if prompt is not None:
            kind, value = prompt
            if kind == "text":
                self.verifier.set_prompt(value)
                logger.info("live: prompt now the text %r", value)
                changed = True
            else:
                try:
                    reference = self.load_image(value)
                except Exception as err:
                    logger.warning("live: can't read %s (%s); trying again", value.name, err)
                    self.live.forget_prompt()
                else:
                    kind = self.live.read_class(value)
                    self.verifier.set_prompt(value.name, reference, kind=kind)
                    logger.info("live: prompt now the image %s%s", value.name,
                                f", of class {kind!r}" if kind else "")
                    changed = True
        return changed

    def _describe(self):
        try:
            self.verifier.describe_image()
        except Exception as err:
            logger.warning("couldn't describe the image %s (%s); asking again at the next heartbeat",
                           self.verifier.label, err)

    def step(self, now):
        """The heartbeat's payload if one is due at `now` (seconds,
        time.monotonic()), else None: before it, the live directory is read
        and an image described. None too while there is no description."""
        if self._next is not None and now < self._next:
            return None
        self._next = now + self.interval_s
        self.update()
        self._describe()
        description = self.verifier.description
        if description and self.live is not None and description != self._written:
            try:
                self.live.write_description(description)
                self._written = description
            except OSError as err:
                logger.warning("live: can't write %s: %s", DESCRIPTION_FILE, err)
        return self.verifier.heartbeat_payload()


def run(name, verifier, *, stop, keeper=None, on_ready=None):
    """Serves the engine `name` until the threading.Event `stop` is set,
    publishing `keeper`'s heartbeat (a PromptKeeper; one without a live
    directory by default). A DERIVED event is about an input in a flow, so the
    heartbeat starts once the first input has come, and is about the latest;
    the engine subscribes to its own events to learn the flow."""
    keeper = keeper or PromptKeeper(verifier)
    with uriel.Engine(name, result_type=GENERIC_RESULT_TYPE, max_output_size=MAX_OUTPUT_SIZE) as engine:
        engine.subscribe(name)
        flow, last_id = None, None
        logger.info("engine %s ready; verifying %s %r; a heartbeat every %gs; a yes at p >= %g, a no at p >= %g, "
                    "else unsure", name, "image" if verifier.reference is not None else "prompt", verifier.label,
                    keeper.interval_s, verifier.yes_conf, verifier.no_conf)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                flow = event.flow or flow
            payload = keeper.step(time.monotonic())
            if payload is not None and flow is not None and last_id is not None:
                try:
                    engine.publish_derived(last_id, flow, payload, result_type=PROMPT_TYPE)
                except Exception:
                    logger.exception("failed to publish the prompt heartbeat")
            input = engine.poll()
            if input is None:
                continue
            last_id = input.id
            if input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
                logger.warning("input %d is a %r, not a RawFrame; dropping it", input.id.seq, input.payload_type)
                engine.drop()
                continue
            try:
                match, result = verifier.on_input(input)
            except FrameError as err:
                logger.warning("input %d: %s; dropping it", input.id.seq, err)
                engine.drop()
                continue
            except Exception:
                logger.exception("input %d: failed to verify it; dropping it", input.id.seq)
                engine.drop()
                continue
            if match:
                engine.forward(result=result)
            else:
                engine.drop(result=result)
