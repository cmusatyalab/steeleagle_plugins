echo "Running driver command string!"
STEELEAGLE_PLUGIN=driver.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"message": "Hi there!"}' pixi run start
echo "Running engine command string!"
STEELEAGLE_PLUGIN=engine.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"confidence": 0.55}' ICEORYX_SERVICE=engine pixi run start
