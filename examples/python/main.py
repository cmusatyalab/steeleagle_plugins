import os
from driver import run_driver
from engine import run_engine

if __name__ == "__main__":
    plugin = os.environ.get('STEELEAGLE_PLUGIN')
    args = os.environ.get('STEELEAGLE_PLUGIN_ARGS')
    iceoryx = os.environ.get('STEELEAGLE_ENGINE_ENDPOINT')
    if plugin == "driver.plugin.toml":
        run_driver(args)
    elif plugin == "engine.plugin.toml":
        run_engine(iceoryx, args)
    else:
        print("Unrecognized plugin!")
