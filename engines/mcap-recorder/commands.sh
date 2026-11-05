# Manual run; the daemon starts the plugin itself. Ctrl-C closes the recordings.
STEELEAGLE_PLUGIN=mcap_recorder.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"engines": ["detection"]}' STEELEAGLE_ENGINE_ENDPOINT=mcap_recorder pixi run start
