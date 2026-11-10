# Manual runs; the daemon starts the plugins itself.
STEELEAGLE_PLUGIN=swiftmap.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"server": "localhost", "server_port": 43322}' STEELEAGLE_ENGINE_ENDPOINT=swiftmap pixi run start
STEELEAGLE_PLUGIN=keyframe.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"send_distance": 5, "min_disparity": 40}' STEELEAGLE_ENGINE_ENDPOINT=keyframe pixi run start
