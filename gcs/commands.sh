#!/usr/bin/env bash
# Runs the GCS by hand, the way eagled starts it as an engine plugin.
#
# This needs a running daemon: the GCS calls the daemon's API on CLIENT_SOCKET,
# and reads frames and telemetry as the Uriel engine ICEORYX_SERVICE, which the
# daemon's Uriel server must serve. Without a daemon, run `pixi run mock`
# instead: a self-contained fake daemon with simulated vehicles, which starts
# the GCS itself (see "Mock harness" in README.md).
set -euo pipefail
cd "$(dirname "$0")"

# The plugin's name, as eagled sets it (the plugin file without ".plugin.toml")
export STEELEAGLE_PLUGIN=gcs
# The plugin's args, as JSON (see gcs.plugin.toml): where the web UI is served
export STEELEAGLE_PLUGIN_ARGS='{"http_addr":":8002"}'
# The Uriel engine the GCS reads frames and telemetry as: its daemon's IP, a
# dot and the service's name
export ICEORYX_SERVICE="${ICEORYX_SERVICE:-100.64.0.1.gcs}"
# The daemon's API socket for this service: the out.sock in the service's run
# directory, which the daemon creates. There is no default; pass it in
export CLIENT_SOCKET="${CLIENT_SOCKET:?set CLIENT_SOCKET to the daemon socket for this service}"

# Builds the web UI and the server if needed, then runs the server
pixi run start
