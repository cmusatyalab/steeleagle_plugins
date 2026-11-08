# Manual run; the daemon starts the plugin itself.
STEELEAGLE_PLUGIN_ARGS='{"prompt": "person lying down", "verifier": "verifier"}' STEELEAGLE_ENGINE_ENDPOINT=cliped pixi run start
# Off the Tachyon, run the INT8 model on the CPU:
# STEELEAGLE_PLUGIN_ARGS='{"prompt": "person lying down", "verifier": "verifier", "npu": false}' STEELEAGLE_ENGINE_ENDPOINT=cliped pixi run start
