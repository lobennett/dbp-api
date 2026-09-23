"""Coordinator commands for a non-participant PGL pilot and durable results."""

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
from .pgl_adapter import RunSettings
from .runner import StudyRunner


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help. Secret values are never accepted as arguments.\n")


def _parser():
    parser = _Parser(description="DBP/PGL non-participant pilot; scientific readiness remains unapproved")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/dbp-pgl")
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".local/share/dbp-pgl/cache")
    parser.add_argument("--work-root", type=Path, default=Path.home() / ".local/share/dbp-pgl/work")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("launch", help="Open the local coordinator window; no task starts automatically")
    connect = commands.add_parser("connect", help="Pair using a hidden one-time code prompt")
    connect.add_argument("--server", help="HTTPS origin (prompted if omitted)")
    connect.add_argument("--device-name", default=socket.gethostname())
    connect.add_argument("--allow-file-token", action="store_true",
                         help="Opt in to mode-0600 integration-only token storage")
    for name in ("prepare", "status", "run", "sync"):
        command = commands.add_parser(name)
        command.add_argument("subject_alias")
        if name == "run":
            command.add_argument("--integration-test", action="store_true", help="Acknowledge non-participant use")
            command.add_argument("--no-sync", action="store_true", help="Save locally; synchronize separately")
            command.add_argument("--ffmpeg", help="Local FFmpeg executable for full decode preflight")
            command.add_argument("--day", type=int, default=1)
            command.add_argument("--block", type=int, default=1)
            command.add_argument("--description-seconds", type=int, default=12)
            command.add_argument("--display-width", type=int, default=50)
            command.add_argument("--settings-name")
            command.add_argument("--display-name")
    recover = commands.add_parser("recover", help="End an interrupted attempt without replaying videos")
    recover.add_argument("subject_alias")
    recover.add_argument("--terminate", action="store_true", required=True)
    recover.add_argument("--repair-tail", action="store_true", help="Explicitly discard only an incomplete journal fragment")
    return parser


def _summary(prepared):
    return {"preparation_ready": True, "pgl_ready": False, "integration_status": "STARTED",
            "experiment_id": prepared.package.experiment_id,
            "subject_id": prepared.package.subject_id, "package_id": prepared.package.package_id,
            "package_sha256": prepared.package.package_sha256,
            "trial_count": len(prepared.package.trials), "prepared_root": str(prepared.root),
            "limitation": "Preparation verified; participant protocol and hardware validation remain separate"}


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "run" and not args.integration_test:
        print("Use --integration-test to acknowledge a non-participant pilot; pgl_ready=false.", file=sys.stderr)
        return 2
    try:
        if args.command == "launch":
            from .launcher import launch
            return launch(config_dir=args.config_dir, cache_root=args.cache_root, work_root=args.work_root)
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
        runner = StudyRunner(config_dir=args.config_dir, cache_root=args.cache_root, work_root=args.work_root)
        if args.command == "run":
            settings = RunSettings(args.day, args.block, args.description_seconds, args.display_width,
                                   args.settings_name, args.display_name)
            result = runner.run(args.subject_alias, integration_test=True, settings=settings,
                                ffmpeg=args.ffmpeg, synchronize=not args.no_sync)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "completed" else 1
        if args.command in ("sync", "recover", "status"):
            result = (runner.sync(args.subject_alias) if args.command == "sync" else
                      runner.recover(args.subject_alias, terminate=args.terminate, repair_tail=args.repair_tail)
                      if args.command == "recover" else runner.status(args.subject_alias))
            print(json.dumps(result, sort_keys=True))
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
        print("Interrupted. Inspect status and local results before recovery; never replay just to retry sync.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
