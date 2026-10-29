echo "Running verifier engine command string!"
STEELEAGLE_PLUGIN=verifier.plugin.toml STEELEAGLE_PLUGIN_ARGS='{}' STEELEAGLE_ENGINE_ENDPOINT=verifier pixi run start

# Manual run with a CLI override, e.g. to try a smaller/faster variant:
# STEELEAGLE_PLUGIN=verifier.plugin.toml STEELEAGLE_PLUGIN_ARGS='{}' STEELEAGLE_ENGINE_ENDPOINT=verifier pixi run start -- --size nano
