import argparse
import getpass
import json
from pathlib import Path
import sys

from .common import HubError
from .config import Configuration
from .hub import Hub
from .server import GatewayServer, connection_file
from .store import StateLock


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ksp-aihub")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True, help="Local state/DPAPI store directory; never package it")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--connection-file", type=Path, required=True)
    commands.add_parser("check")
    key = commands.add_parser("set-api-key")
    key.add_argument("credential")
    args = parser.parse_args(argv)
    try:
        config = Configuration.from_file(args.config)
        hub = Hub(config, args.state)
        if args.command == "check":
            print(json.dumps(hub.profiles(), ensure_ascii=False, indent=2))
        elif args.command == "set-api-key":
            hub.auth.set_api_key(args.credential, getpass.getpass("API key (not echoed): "))
            print("Credential stored using Windows user-bound encryption.")
        else:
            with StateLock(args.state):
                connection = connection_file(args.connection_file, config.port)
                server = GatewayServer(hub, connection["token"])
                print("KSP AI Hub listening at " + connection["endpoint"], flush=True)
                try: server.serve_forever(poll_interval=0.2)
                finally: server.server_close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (HubError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
