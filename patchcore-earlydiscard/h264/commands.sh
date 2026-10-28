echo "Running h264 encoder command string!"
STEELEAGLE_PLUGIN=encoder.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"decoder_name": "h264_decoder", "encoder": "qtic2venc quant-i-frames=40 quant-p-frames=40 b-frames=0", "log_frames": true}' STEELEAGLE_ENGINE_ENDPOINT=h264_encoder STEELEAGLE_REMOTE_QUALIFIER=100.64.0.1 pixi run start

echo "Running h264 decoder command string!"
STEELEAGLE_PLUGIN=decoder.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"log_frames": true}' STEELEAGLE_ENGINE_ENDPOINT=h264_decoder pixi run start
