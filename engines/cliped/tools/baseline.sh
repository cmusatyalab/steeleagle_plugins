#!/bin/sh
# Rebuild the 300-tile INT8 model of the original weights in tools/model, then
# run all three videos on the NPU. Needs tools/model/tiles.npz from the
# calibrate step (pixi run -e tools quantize --steps calibrate).
cd "$(dirname "$0")/.." || exit 1
mkdir -p tools/model tools/runs
pixi run -e tools quantize --out tools/model --steps export convert verify > tools/model/quantize.log 2>&1 || exit 1
for v in street traintrack parking_lot; do
  mkdir -p tools/runs/base_$v
  tools/run_int8.sh tools/model/image_encoder_int8.tflite $v.mp4 tools/labels/labels_$v.txt tools/runs/base_$v > tools/runs/base_$v/log.txt 2>&1
done
echo BASELINE_DONE > tools/runs/done.txt
