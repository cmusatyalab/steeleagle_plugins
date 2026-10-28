import json

def run_engine(iceoryx, args):
    print("Ran engine")
    if not args:
        print("No args provided")
        return
    elif not iceoryx:
        print("No iceoryx provided")
        return
    print(f"Serving at endpoint {iceoryx}")
    args = json.loads(args)
    if 'confidence' not in args:
        print("Args could not be parsed or don't contain confidence")
        return
    print("Using confidence " + str(args['confidence']))
