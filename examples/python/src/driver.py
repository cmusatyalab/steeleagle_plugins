import json

def run_driver(args):
    print("Ran driver")
    if not args:
        print("No args provided")
        return
    # This should output the message argument
    args = json.loads(args)
    if 'message' not in args:
        print("Args could not be parsed or don't contain message")
        return
    print("Using message "+ args['message'])
