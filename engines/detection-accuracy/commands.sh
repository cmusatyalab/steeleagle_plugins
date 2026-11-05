echo "Running detection-accuracy engine command string!"
STEELEAGLE_PLUGIN=detection_accuracy.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"rubric_path": "/home/particle/rubric.json"}' STEELEAGLE_ENGINE_ENDPOINT=detection_accuracy pixi run start

# Manual run with a CLI override, e.g. to point at a different rubric file:
# STEELEAGLE_PLUGIN=detection_accuracy.plugin.toml STEELEAGLE_PLUGIN_ARGS='{}' STEELEAGLE_ENGINE_ENDPOINT=detection_accuracy pixi run start -- --rubric /path/to/other_rubric.json
