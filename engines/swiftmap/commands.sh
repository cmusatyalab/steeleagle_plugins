# Manual runs; the daemon starts the plugins itself.
STEELEAGLE_PLUGIN=swiftmap.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"server": "localhost", "server_port": 43322}' STEELEAGLE_ENGINE_ENDPOINT=swiftmap pixi run start
# Needs the IP of the remote daemon the swiftmap engine runs on
STEELEAGLE_PLUGIN=reachback.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"remote": "swiftmap", "send_distance": 5, "min_disparity": 40}' STEELEAGLE_ENGINE_ENDPOINT=reachback STEELEAGLE_REMOTE_QUALIFIER=128.2.208.19 pixi run start
