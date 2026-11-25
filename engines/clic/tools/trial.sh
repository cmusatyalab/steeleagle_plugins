#!/bin/sh
# Usage: tools/trial.sh name weights.pt - build an INT8 model from these image
# encoder weights (e.g. models/distill_visual.pt) in tools/model_<name>, with
# the calibration tiles of tools/model, and run all three videos on the NPU.
cd "$(dirname "$0")/.." || exit 1
n=$1; mkdir -p tools/model_$n tools/runs
cp tools/model/tiles.npz tools/model/calibration_image_sample_data_20x128x128x3_float32.npy tools/model_$n/
pixi run -e tools quantize --out tools/model_$n --weights "$2" --steps export convert verify > tools/model_$n/quantize.log 2>&1 || exit 1
for v in street traintrack parking_lot; do mkdir -p tools/runs/${n}_$v
  tools/run_int8.sh tools/model_$n/image_encoder_int8.tflite $v.mp4 tools/labels/labels_$v.txt tools/runs/${n}_$v > tools/runs/${n}_$v/log.txt 2>&1; done
echo DONE > tools/runs/${n}_done.txt
