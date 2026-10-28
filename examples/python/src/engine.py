import json

# Default args, matching engine.plugin.toml. The daemon passes only the args
# set in its config, so any arg left unset is defaulted here.
DEFAULT_ARGS = {
    "confidence": 0.75,
}

def run_engine(iceoryx, args):
    print("Ran engine")
    if not iceoryx:
        print("No iceoryx provided")
        return
    print(f"Serving at endpoint {iceoryx}")
    args = json.loads(args) if args else {}
    for name, default in DEFAULT_ARGS.items():
        if args.get(name) is None:
            args[name] = default
    print("Using confidence " + str(args['confidence']))
