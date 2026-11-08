#!/usr/bin/env python3
"""Quantize the MobileCLIP2-S0 image encoder to INT8 TFLite for the Tachyon NPU.

Runs entirely on the device:

    pixi run -e tools quantize                  # all steps, from the plugin folder
    pixi run -e tools quantize --steps verify   # just one step

  calibrate  sample tiles from the videos, cut exactly as cliped cuts them
  export     PyTorch -> ONNX (no L2 normalization, which cliped does on CPU)
  convert    ONNX -> SavedModel -> full-integer TFLite, percentile-calibrated,
             then patched as in the patch step
  patch      give the model's Gathers int32 indices, which the HTP runs, in
             place of the int64 ones the export leaves, which it does not
  verify     compare the INT8 model on CPU with FP32 on tiles it did not see

The input is 256x256 RGB int8 (the uint8 pixel minus 128) and the output is the
int8 unnormalized 512-d embedding. QNN's converters are x86-only, which is why
this goes through TFLite: the QNN TFLite delegate prepares the graph for the
HTP on the device itself.
"""
import argparse
import os
from pathlib import Path

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import numpy as np

from cliped.clip import MODELS, tile_boxes
from evaluate import read_frames

HERE = Path(__file__).resolve().parent
VIDEOS = ["street.mp4", "traintrack.mp4", "parking_lot.mp4"]
# In the order they run; patch is part of convert, and on its own patches an
# existing model
STEPS = ["calibrate", "export", "convert", "patch", "verify"]


def cut_tiles(video, frames, per_frame, size, tile, stride, height, fps, rng):
    """Random tiles from `frames` evenly spaced 1 fps frames of one video."""
    import cv2

    all_frames = list(read_frames(video, fps, None, height))
    picks = np.linspace(0, len(all_frames) - 1, frames).round().astype(int)
    tiles = []
    for pick in picks:
        frame = cv2.cvtColor(all_frames[pick][1], cv2.COLOR_BGR2RGB)
        boxes = tile_boxes(frame.shape[1], frame.shape[0], tile, stride)
        for i in rng.choice(len(boxes), per_frame, replace=False):
            x0, y0, x1, y1 = boxes[i]
            tiles.append(cv2.resize(frame[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA))
    return tiles


def calibrate(args):
    """Same frames and tiles as cliped. Calibration and check sets differ."""
    rng = np.random.default_rng(0)
    calib, check = [], []
    for name in VIDEOS:
        video = args.video_dir / name
        tiles = cut_tiles(video, args.frames, 12, args.size, args.tile, args.stride,
                          args.height, args.fps, rng)
        order = rng.permutation(len(tiles))
        n = args.samples // len(VIDEOS)
        calib += [tiles[i] for i in order[:n]]
        check += [tiles[i] for i in order[n:n + args.check // len(VIDEOS)]]
        print(f"{name}: {len(tiles)} tiles sampled")
    np.savez(args.out / "tiles.npz", calib=np.stack(calib), check=np.stack(check))
    print(f"calibration tiles: {len(calib)}, check tiles: {len(check)}")


def load_torch_model(args, weights=None):
    """The reparameterized model; weights optionally replaces its image encoder
    (e.g. from AdaRound or distillation). Verification always passes None, to
    compare against the original FP32 encoder."""
    import open_clip
    import torch
    from timm.utils import reparameterize_model

    name, pretrained = MODELS["S0"]
    model, _, _ = open_clip.create_model_and_transforms(name, pretrained=pretrained, device="cpu")
    cfg = open_clip.get_model_preprocess_cfg(model)
    if any(m != 0 for m in cfg["mean"]) or any(s != 1 for s in cfg["std"]):
        raise SystemExit(f"this script assumes mean 0, std 1; got {cfg['mean']}, {cfg['std']}")
    model = reparameterize_model(model.eval())
    if weights is not None:
        model.visual.load_state_dict(torch.load(weights, map_location="cpu"))
        print(f"image encoder weights from {weights}")
    return model


def export(args):
    import onnx
    import onnxslim
    import torch

    class ImageEncoder(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, x):  # NCHW RGB in [0, 1]
            return self.model.encode_image(x, normalize=False)

    wrapper = ImageEncoder(load_torch_model(args, args.weights)).eval()
    check = torch.from_numpy(np.load(args.out / "tiles.npz")["check"][:8]).permute(0, 3, 1, 2).float().div(255)
    with torch.inference_mode():
        before = wrapper(check)

    # Depthwise convs with more outputs than inputs took 99% of the HTP's time.
    class PlainDepthwise(torch.nn.Module):
        """Exact equal of a channel-multiplying depthwise conv, using multiplier 1."""

        def __init__(self, conv):
            super().__init__()
            multiplier = conv.out_channels // conv.in_channels
            self.register_buffer("order", torch.arange(conv.in_channels).repeat_interleave(multiplier))
            self.conv = torch.nn.Conv2d(conv.out_channels, conv.out_channels, conv.kernel_size, conv.stride,
                                        conv.padding, conv.dilation, conv.out_channels, conv.bias is not None)
            self.conv.weight.data.copy_(conv.weight.data)
            if conv.bias is not None:
                self.conv.bias.data.copy_(conv.bias.data)

        def forward(self, x):
            return self.conv(x[:, self.order])

    for parent in list(wrapper.modules()):
        for child_name, child in parent.named_children():
            if (isinstance(child, torch.nn.Conv2d) and child.groups == child.in_channels > 1
                    and child.out_channels > child.in_channels):
                setattr(parent, child_name, PlainDepthwise(child))
                print("rewrote", child_name, child.in_channels, "->", child.out_channels, child.kernel_size)
    with torch.inference_mode():
        error = (wrapper(check) - before).abs().max().item()
    print(f"max output change from the rewrite: {error:.2e}")
    if error > 1e-4:
        raise SystemExit("the depthwise rewrite changed the model")

    dummy = torch.zeros(1, 3, args.size, args.size)
    torch.onnx.export(wrapper, dummy, args.out / "image_encoder.onnx", input_names=["image"],
                      output_names=["embedding"], opset_version=20, dynamo=False)  # 20 has a real Gelu op
    onnx.save(onnxslim.slim(onnx.load(args.out / "image_encoder.onnx")), args.out / "image_encoder.onnx")
    print("wrote image_encoder.onnx")


def clip_ranges(calibrated, tiles, percentile):
    """Replace the min/max stats in a calibrated model with percentiles.

    This model's activations are long-tailed: the stem reaches about +-80 while
    most values stay under 1. Min/max gives such a tensor a step so coarse that
    the common values collapse to a few levels, and the embedding becomes noise.
    Clipping the outliers is the far smaller error.
    """
    import tensorflow as tf
    from tensorflow.lite.tools import flatbuffer_utils

    model = flatbuffer_utils.read_model_from_bytearray(bytearray(calibrated))
    graph = model.subgraphs[0]
    tracked = [i for i, tensor in enumerate(graph.tensors)
               if i not in graph.inputs and tensor.quantization is not None
               and tensor.quantization.min is not None and len(tensor.quantization.min) == 1]

    interpreter = tf.lite.Interpreter(
        model_content=bytes(calibrated), experimental_preserve_all_tensors=True,
        experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES)
    interpreter.allocate_tensors()
    input_index = interpreter.get_input_details()[0]["index"]
    rng = np.random.default_rng(0)
    samples = {i: [] for i in tracked}
    for tile in tiles:
        interpreter.set_tensor(input_index, tile[None].astype(np.float32) / 255.0)
        interpreter.invoke()
        for i in tracked:
            values = interpreter.get_tensor(i).ravel()
            samples[i].append(rng.choice(values, min(values.size, 512), replace=False))

    for i in tracked:
        low, high = np.percentile(np.concatenate(samples[i]), [100 - percentile, percentile])
        graph.tensors[i].quantization.min = [float(low)]
        graph.tensors[i].quantization.max = [float(high)]
    print(f"clipped {len(tracked)} tensors to the {percentile} percentile")
    return bytes(flatbuffer_utils.convert_object_to_bytearray(model))


def convert(args):
    import onnx2tf
    import tensorflow as tf
    from tensorflow.lite.python.optimize import calibrator

    calib = np.load(args.out / "tiles.npz")["calib"]

    # onnx2tf checks its tensor layouts on sample images, reading this exact
    # name from the working directory before trying to download its own.
    os.chdir(args.out)
    np.save("calibration_image_sample_data_20x128x128x3_float32.npy",
            calib[:20].astype(np.float32) / 255.0)

    saved_model = args.out / "saved_model"
    onnx2tf.convert(input_onnx_file_path=str(args.out / "image_encoder.onnx"),
                    output_folder_path=str(saved_model), output_signaturedefs=True,
                    non_verbose=True)

    def representative_dataset():
        for tile in calib:
            yield [tile[None].astype(np.float32) / 255.0]

    converter = tf.lite.TFLiteConverter.from_saved_model(str(saved_model))
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.int8
    converter.inference_output_type = tf.int8

    # The converter feeds these stats straight to its quantizer, so this is the
    # one place to swap them, and the rest of the conversion stays stock.
    plain_calibrate = calibrator.Calibrator.calibrate

    def calibrate(self, dataset_gen):
        return clip_ranges(plain_calibrate(self, dataset_gen), calib, args.percentile)

    if args.percentile < 100:
        calibrator.Calibrator.calibrate = calibrate
    args.model.write_bytes(int32_gather_indices(converter.convert()))
    calibrator.Calibrator.calibrate = plain_calibrate
    print(f"wrote {args.model} ({args.model.stat().st_size / 1e6:.1f} MB)")


def int32_gather_indices(model_bytes):
    """`model_bytes`, a TFLite model, with the constant indices of its GATHER
    ops as int32 instead of int64; the same model otherwise.

    The depthwise rewrite in export gathers channels with a torch.arange
    buffer, which is int64, and stays int64 through ONNX and onnx2tf. The HTP
    runs INT8 Gather only with int32 (or uint32) indices, so with int64 ones
    the QNN delegate leaves each Gather to the CPU, splitting the graph around
    it. The indices are channel numbers, so int32 holds them exactly.
    """
    from tensorflow.lite.python import schema_py_generated as schema
    from tensorflow.lite.tools import flatbuffer_utils

    model = flatbuffer_utils.read_model_from_bytearray(bytearray(model_bytes))
    gather = schema.BuiltinOperator.GATHER
    patched = 0
    for graph in model.subgraphs:
        for op in graph.operators:
            code = model.operatorCodes[op.opcodeIndex]
            if max(code.builtinCode, code.deprecatedBuiltinCode) != gather:
                continue
            tensor = graph.tensors[op.inputs[1]]
            buffer = model.buffers[tensor.buffer]
            if tensor.type != schema.TensorType.INT64 or buffer.data is None or len(buffer.data) == 0:
                continue
            indices = np.frombuffer(bytes(bytearray(buffer.data)), dtype="<i8")
            if indices.min() < np.iinfo(np.int32).min or indices.max() > np.iinfo(np.int32).max:
                raise SystemExit(f"gather indices {tensor.name!r} do not fit int32")
            buffer.data = np.frombuffer(indices.astype("<i4").tobytes(), dtype=np.uint8)
            tensor.type = schema.TensorType.INT32
            patched += 1
    print(f"gave {patched} gather ops int32 indices")
    return bytes(flatbuffer_utils.convert_object_to_bytearray(model))


def patch(args):
    args.model.write_bytes(int32_gather_indices(args.model.read_bytes()))
    print(f"patched {args.model}")


def verify(args):
    import torch
    from ai_edge_litert.interpreter import Interpreter

    check = np.load(args.out / "tiles.npz")["check"]
    model = load_torch_model(args)
    with torch.inference_mode():
        x = torch.from_numpy(check).permute(0, 3, 1, 2).float().div(255)
        fp32 = torch.cat([model.encode_image(b, normalize=True) for b in x.split(32)]).numpy()

    interpreter = Interpreter(str(args.model), num_threads=4)
    interpreter.allocate_tensors()
    inp, out = interpreter.get_input_details()[0], interpreter.get_output_details()[0]
    print("input ", inp["dtype"].__name__, inp["shape"].tolist(), inp["quantization"])
    print("output", out["dtype"].__name__, out["shape"].tolist(), out["quantization"])
    scale, zero = out["quantization"]
    int8 = []
    for tile in check:
        interpreter.set_tensor(inp["index"], (tile.astype(np.int16) - 128).astype(np.int8)[None])
        interpreter.invoke()
        int8.append(scale * (interpreter.get_tensor(out["index"])[0].astype(np.float32) - zero))
    int8 = np.stack(int8)
    int8 /= np.linalg.norm(int8, axis=1, keepdims=True)
    cosine = (int8 * fp32).sum(axis=1)
    print(f"cosine INT8 vs FP32 over {len(check)} tiles: mean {cosine.mean():.4f}, "
          f"min {cosine.min():.4f}, 5th percentile {np.percentile(cosine, 5):.4f}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--video-dir", type=Path, default=Path("/home/particle"))
    p.add_argument("--out", type=Path, default=HERE / "model", help="working directory")
    p.add_argument("--model", type=Path, help="output .tflite (default: <out>/image_encoder_int8.tflite)")
    p.add_argument("--steps", nargs="+", default=["calibrate", "export", "convert", "verify"],
                   choices=STEPS)
    p.add_argument("--samples", type=int, default=300, help="calibration tiles, split across the videos")
    p.add_argument("--weights", type=Path, help="image encoder state dict to export instead of the original")
    p.add_argument("--percentile", type=float, default=99.9, help="activation range; 100 is plain min/max")
    p.add_argument("--check", type=int, default=150, help="held-out tiles for the verify step")
    p.add_argument("--frames", type=int, default=40, help="frames sampled per video")
    p.add_argument("--size", type=int, default=256, help="model input size")
    p.add_argument("--tile", type=int, default=200, help="as in cliped")
    p.add_argument("--stride", type=int, default=150, help="as in cliped")
    p.add_argument("--height", type=int, default=720, help="as in cliped")
    p.add_argument("--fps", type=float, default=1.0, help="as in cliped")
    args = p.parse_args()
    args.out = args.out.resolve()
    args.model = (args.model or args.out / "image_encoder_int8.tflite").resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    return args


def main():
    args = parse_args()
    for step in STEPS:
        if step in args.steps:
            print(f"--- {step}", flush=True)
            globals()[step](args)


if __name__ == "__main__":
    main()
