#!/bin/sh
# Usage: tools/run_int8.sh model.tflite video labels outdir
# Runs the offline evaluation of an INT8 model on the NPU. The video is in
# $VIDEO_DIR (default /home/particle); paths are relative to the plugin folder.
cd "$(dirname "$0")/.." || exit 1
VIDEO_DIR=${VIDEO_DIR:-/home/particle}
LD_LIBRARY_PATH=$PWD/libs exec pixi run -e tools evaluate "$VIDEO_DIR/$2" "person lying down" --npu "$1" --labels "$3" \
  --tps-dir "$4/tps" --sent-dir "$4/sent" --stats "$4/stats.txt" --overwrite
