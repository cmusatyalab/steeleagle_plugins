echo "Running patchcore-retrain engine command string!"
STEELEAGLE_PLUGIN=patchcore-retrain.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"daemon_addr": "fermi.local:50051"}' STEELEAGLE_ENGINE_ENDPOINT=patchcore_retrain pixi run start
