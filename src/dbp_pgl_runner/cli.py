"""Coordinator CLI with deliberate hard stops before execution or synchronization."""

import argparse
import getpass
import json
from pathlib import Path
import socket
import sys
import warnings

from .api import ApiError, RunnerApi
from .config import RunnerConfig, preflight_config_directory, save_pairing, validate_origin
from .models import ContractError
from .prepare import prepare_subject, status_subject


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help. Secret values are never accepted as arguments.\n")


def _parser():
    parser = _Parser(description="DBP integration preparation only; pgl_ready=false")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/dbp-pgl")
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".local/share/dbp-pgl/cache")
    parser.add_argument("--work-root", type=Path, default=Path.home() / ".local/share/dbp-pgl/work")
    commands = parser.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("connect", help="Pair using a hidden one-time code prompt")
    connect.add_argument("--server", help="HTTPS origin (prompted if omitted)")
    connect.add_argument("--device-name", default=socket.gethostname())
    connect.add_argument("--allow-file-token", action="store_true",
                         help="Opt in to mode-0600 integration-only token storage")
    for name in ("prepare", "status", "run", "sync"):
        command = commands.add_parser(name)
        command.add_argument("subject_alias")
    return parser


def _summary(prepared):
    return {"preparation_ready": True, "pgl_ready": False, "integration_status": "STARTED",
            "experiment_id": prepared.package.experiment_id,
            "subject_id": prepared.package.subject_id, "package_id": prepared.package.package_id,
            "package_sha256": prepared.package.package_sha256,
            "trial_count": len(prepared.package.trials), "prepared_root": str(prepared.root),
            "limitation": "Offline integrity only; execution, journal, leases and synchronization pending"}


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command in ("run", "sync"):
        print(f"{args.command} not implemented: approved trial journal/adapter, leases, recovery and "
              "result synchronization are pending. No experiment starts; pgl_ready=false.", file=sys.stderr)
        return 2
    try:
        if args.command == "connect":
            origin = validate_origin(args.server or input("Server origin (HTTPS or local loopback): "))
            if not args.allow_file_token:
                consent = input("Integration-only: store device secret in a private 0600 file (Keychain pending)? [yes/no]: ")
                if consent != "yes":
                    raise ContractError("No credential storage approved; pairing cancelled")
            preflight_config_directory(args.config_dir)
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                code = getpass.getpass("One-time pairing code (hidden): ")
            response = RunnerApi.pair(origin, code, args.device_name)
            config = save_pairing(args.config_dir, origin, response, allow_file_token=True)
            print(json.dumps({"connected": True, "device_id": config.device_id,
                              "experiment_id": config.experiment_id, "pgl_ready": False}))
            return 0
        config = RunnerConfig.load(args.config_dir)
        if args.command == "prepare":
            api = RunnerApi(config, config.read_token(args.config_dir))
            api.identity()
            prepared = prepare_subject(api, config, args.subject_alias, args.cache_root, args.work_root)
        else:
            prepared = status_subject(config, args.subject_alias, args.work_root)
        print(json.dumps(_summary(prepared), sort_keys=True))
        return 0
    except (ContractError, ApiError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, EOFError, getpass.GetPassWarning):
        print("Operation failed: private storage, network, or a hidden-input terminal is unavailable.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled; no experiment was started.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
