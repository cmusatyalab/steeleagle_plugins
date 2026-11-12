# Manual runs; the daemon starts the plugins itself.
STEELEAGLE_PLUGIN=swiftmap.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"server": "localhost", "server_port": 43322}' STEELEAGLE_ENGINE_ENDPOINT=swiftmap STEELEAGLE_FILES=$HOME/.local/share/steeleagle/files pixi run start
STEELEAGLE_PLUGIN=keyframe.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"send_distance": 5, "min_disparity": 40}' STEELEAGLE_ENGINE_ENDPOINT=keyframe pixi run start
# Needs a daemon: its socket, and the IP of the remote daemon the swiftmap engine runs on
STEELEAGLE_PLUGIN=reachback.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"remote": "swiftmap"}' STEELEAGLE_ENGINE_ENDPOINT=reachback STEELEAGLE_CLIENT_SOCKET=/run/user/$(id -u)/steeleagle/plugins/reachback/run/in.sock STEELEAGLE_REMOTE_QUALIFIER=128.2.208.19 pixi run start
