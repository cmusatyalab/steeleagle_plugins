import json

# Default args, matching driver.plugin.toml. The daemon passes only the args
# set in its config, so any arg left unset is defaulted here.
DEFAULT_ARGS = {
    "message": "Hello World!",
}

def run_driver(args):
    print("Ran driver")
    args = json.loads(args) if args else {}
    for name, default in DEFAULT_ARGS.items():
        if args.get(name) is None:
            args[name] = default
    # This should output the message argument
    print("Using message "+ args['message'])
