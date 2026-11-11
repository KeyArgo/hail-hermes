import argparse
from .hub import main as hub_main
from .device import main as device_main

def main():
    ap = argparse.ArgumentParser(prog="pocket-sim")
    sub = ap.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("hub")
    h.add_argument("--host", default="0.0.0.0")
    h.add_argument("--port", type=int, default=8767)
    h.add_argument("--reply", default="Hello from hub.")
    d = sub.add_parser("device")
    d.add_argument("--url", default="ws://localhost:8767/pocket")
    d.add_argument("--kind", default="wear")
    d.add_argument("--name", default="sim-device")
    d.add_argument("--script", default=None)
    args = ap.parse_args()
    if args.mode == "hub":
        # re-invoke hub main with args
        import sys
        sys.argv = ["pocket-sim", "--host", args.host, "--port", str(args.port), "--reply", args.reply]
        hub_main()
    else:
        import sys
        sys.argv = ["pocket-sim", "--url", args.url, "--kind", args.kind, "--name", args.name] + (["--script", args.script] if args.script else [])
        device_main()

if __name__ == "__main__":
    main()
