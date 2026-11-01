# Manual runs; the daemon starts the plugins itself.
STEELEAGLE_PLUGIN=priority_reintegration.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"remote": "detection", "classifier": "classifier", "class": "person"}' STEELEAGLE_ENGINE_ENDPOINT=priority_reintegration pixi run start
# STEELEAGLE_PLUGIN=reachback.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"remote": "detection"}' STEELEAGLE_ENGINE_ENDPOINT=reachback pixi run start
