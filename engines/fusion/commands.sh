# Manual runs; the daemon starts the plugins itself.
STEELEAGLE_PLUGIN=fusion_detection.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"local": "detection", "remote": "detection", "grace": 200}' STEELEAGLE_ENGINE_ENDPOINT=fusion pixi run start
