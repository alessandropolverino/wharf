"""wharf's command-line interface.

    wharf deploy     <config.yml> [--only NAME...] [--repo NAME] [--revision SHA] [--identity NAME] [--dry-run]
    wharf down       <config.yml> [--only NAME...] [--volumes] [--identity NAME] [--dry-run]
    wharf reload     <config.yml> [--only NAME...] [--identity NAME] [--dry-run]
    wharf status     <config.yml> [--only NAME...] [--identity NAME]
    wharf logs       <config.yml> [SERVICE...] [--only NAME...] [--follow] [--tail N] [--since WHEN] [--identity NAME]
    wharf ls         <config.yml>
    wharf setup      <config.yml> [--only NAME...] [--identity NAME]
    wharf rotate     <config.yml> [--only NAME...] [--identity NAME]
    wharf identities
    wharf --version

Every subcommand except ``ls`` and ``identities`` accepts
``--ci``/``--interactive`` to override wharf's automatic CI-vs-local
detection (see wharf.ssh.is_ci).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

from . import __version__, operations, rotate as rotate_mod, setup as setup_mod
from .config import Config, ConfigError, compose_service_name, load_config
from .identity import InvalidIdentityError, list_identities, validate_identity_name
from .operations import BranchMismatchError, OperationError
from .ssh import RemoteCommandError, is_ci
from .update_check import check_for_update


def _add_common(parser: argparse.ArgumentParser, *, needs_only: bool = True) -> None:
    parser.add_argument("config", type=Path, help="path to a wharf config yml file")
    if needs_only:
        parser.add_argument(
            "--only", action="append", default=[], metavar="NAME",
            help="deploy/act on just this target (repeatable); default is all targets",
        )
    parser.add_argument(
        "--repo", default=None,
        help="project name used to fill {repo} in path templates; "
             "defaults to the origin remote's name (or the cwd's name)",
    )


def _add_ci_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ci", action="store_true", help="force CI/non-interactive auth mode")
    group.add_argument("--interactive", action="store_true", help="force local/interactive auth mode")


def _force_ci(args: argparse.Namespace) -> bool | None:
    if args.ci:
        return True
    if args.interactive:
        return False
    return None  # autodetect


def _add_identity_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--identity", default=None, metavar="NAME",
        help="named identity for the deploy key (default: 'ci' when running in CI, 'default' otherwise)",
    )


def _add_dry_run_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be pushed and run on each target, without connecting to any",
    )


def _tail_count(text: str) -> str:
    if text != "all" and not text.isdigit():
        raise argparse.ArgumentTypeError(f"expected a line count or 'all', got {text!r}")
    return text


def _compose_service(text: str) -> str:
    try:
        return compose_service_name(text, "SERVICE")
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wharf", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_deploy = subparsers.add_parser("deploy", help="push, build, and start each target")
    _add_common(p_deploy)
    p_deploy.add_argument("--revision", default=None, help="commit SHA to deploy; defaults to HEAD")
    _add_ci_flags(p_deploy)
    _add_identity_flag(p_deploy)
    _add_dry_run_flag(p_deploy)

    p_down = subparsers.add_parser("down", help="stop each target")
    _add_common(p_down)
    p_down.add_argument("--volumes", action="store_true", help="also remove named/anonymous volumes")
    _add_ci_flags(p_down)
    _add_identity_flag(p_down)
    _add_dry_run_flag(p_down)

    p_reload = subparsers.add_parser("reload", help="re-apply compose without rebuilding")
    _add_common(p_reload)
    _add_ci_flags(p_reload)
    _add_identity_flag(p_reload)
    _add_dry_run_flag(p_reload)

    p_status = subparsers.add_parser(
        "status", help="show each target's checked-out revision, last deploy, deploy lock, and compose services",
    )
    _add_common(p_status)
    _add_ci_flags(p_status)
    _add_identity_flag(p_status)

    p_logs = subparsers.add_parser("logs", help="show, or follow, a target's docker compose logs")
    _add_common(p_logs)
    p_logs.add_argument(
        "services", nargs="*", type=_compose_service, metavar="SERVICE",
        help="compose service(s) to show; default is all of them",
    )
    p_logs.add_argument(
        "-f", "--follow", action="store_true",
        help="keep streaming new output until Ctrl-C (one target at a time: pick it with --only)",
    )
    p_logs.add_argument(
        "--tail", default="100", type=_tail_count, metavar="N",
        help="lines to show from the end of each service's log, or 'all' (default: 100)",
    )
    p_logs.add_argument(
        "--since", default=None, metavar="WHEN",
        help="only output since this time, as docker accepts it: a timestamp, or relative like 30m or 2h",
    )
    _add_ci_flags(p_logs)
    _add_identity_flag(p_logs)

    p_ls = subparsers.add_parser("ls", help="list a config's targets")
    _add_common(p_ls, needs_only=False)

    p_setup = subparsers.add_parser("setup", help="bootstrap a config's deploy keys and remote repos")
    _add_common(p_setup)
    _add_ci_flags(p_setup)
    _add_identity_flag(p_setup)

    p_rotate = subparsers.add_parser(
        "rotate",
        help="replace an identity's deploy key, removing the old authorized_keys entry once every target has the new one",
    )
    _add_common(p_rotate)
    _add_ci_flags(p_rotate)
    _add_identity_flag(p_rotate)

    p_identities = subparsers.add_parser(
        "identities",
        help="list locally known deploy-key identities and their key files (local only, not verified against targets)",
    )

    return parser


def _load(config_path: Path, only: list[str] | None = None) -> Config:
    try:
        config = load_config(config_path)
        # Checked up front so an --only typo is a clean config error before
        # anything runs (or any key is generated), not a traceback.
        config.select_targets(tuple(only or ()))
    except (ConfigError, OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        print(f"wharf: {config_path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return config


def _fingerprint(public_key: Path) -> str:
    try:
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(public_key)], capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "(no public key)"
    # ssh-keygen -lf prints "<bits> <fingerprint> <comment> (<type>)"
    return result.stdout.split()[1]


def _resolve_repo(args: argparse.Namespace) -> str:
    return args.repo or operations.infer_repo_name()


def _run_operation(fn, *args, **kwargs) -> int:
    try:
        fn(*args, **kwargs)
    except BranchMismatchError as exc:
        print(f"wharf: {exc}", file=sys.stderr)
        return 2
    except OperationError as exc:
        print(f"wharf: {exc}", file=sys.stderr)
        if isinstance(exc.cause, RemoteCommandError):
            return exc.cause.returncode
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nwharf: interrupted", file=sys.stderr)
        return 130


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "identity", None) is not None:
        try:
            validate_identity_name(args.identity)
        except InvalidIdentityError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return 2

    if args.command == "ls":
        config = _load(args.config)
        for target in config.targets:
            secrets_note = " [secrets]" if target.uses_secrets else ""
            healthcheck_note = f" -> {target.healthcheck}" if target.healthcheck else ""
            print(f"{target.order:>4}  {target.name:<20} {target.user}@{target.address}{secrets_note}{healthcheck_note}")
        return 0

    if args.command == "identities":
        identities = list_identities()
        if not identities:
            print("No local deploy-key identities found. Run `wharf setup` to create one.")
            return 0
        print(f"{'IDENTITY':<12}{'KEY PATH':<28}{'FINGERPRINT':<52}{'MARKER':<16}STATUS")
        for info in identities:
            fingerprint = _fingerprint(info.public_key)
            status = "rotation in progress (staged key pending)" if info.staged_pending else "ok"
            print(f"{info.name:<12}{str(info.private_key):<28}{fingerprint:<52}{info.comment:<16}{status}")
        print()
        print("Local key files only -- not checked against any target's authorized_keys.")
        return 0

    # Best-effort, silent-on-failure; skipped in CI (and for `ls`, which is
    # documented as never touching the network) to avoid adding a network
    # call (and GitHub rate-limit exposure) where it isn't expected.
    if not is_ci() and not os.environ.get("WHARF_NO_UPDATE_CHECK"):
        notice = check_for_update()
        if notice:
            print(notice, file=sys.stderr)

    config = _load(args.config, args.only)
    repo = _resolve_repo(args)
    force_ci = _force_ci(args)

    if args.command in ("setup", "rotate"):
        bootstrap = setup_mod.setup if args.command == "setup" else rotate_mod.rotate
        try:
            bootstrap(
                config, repo=repo, only=tuple(args.only),
                identity=args.identity, force_ci=force_ci,
            )
        except BranchMismatchError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return 2
        except RemoteCommandError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return exc.returncode
        except (subprocess.CalledProcessError, OSError) as exc:
            # local failures, e.g. ssh-keygen missing or failing
            print(f"wharf: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "status":
        return _run_operation(
            operations.status, config,
            repo=repo, only=tuple(args.only), force_ci=force_ci, identity=args.identity,
        )

    if args.command == "logs":
        if args.follow and len(config.select_targets(tuple(args.only))) != 1:
            print("wharf: --follow streams one target at a time; pick it with --only NAME", file=sys.stderr)
            return 2
        try:
            return _run_operation(
                operations.logs, config,
                repo=repo, only=tuple(args.only), services=tuple(args.services),
                follow=args.follow, tail=args.tail, since=args.since,
                force_ci=force_ci, identity=args.identity,
            )
        except KeyboardInterrupt:
            if args.follow:
                return 0  # Ctrl-C is how following ends, not an interruption
            raise

    if args.command == "deploy":
        try:
            revision = args.revision or operations.infer_revision()
        except (subprocess.CalledProcessError, OSError):
            print(
                "wharf: could not determine the revision to deploy -- run wharf from a git "
                "checkout with at least one commit, or pass --revision SHA",
                file=sys.stderr,
            )
            return 2
        return _run_operation(
            operations.deploy, config,
            repo=repo, revision=revision, only=tuple(args.only), force_ci=force_ci, identity=args.identity,
            dry_run=args.dry_run,
        )

    if args.command == "down":
        return _run_operation(
            operations.down, config,
            repo=repo, only=tuple(args.only), volumes=args.volumes, force_ci=force_ci, identity=args.identity,
            dry_run=args.dry_run,
        )

    if args.command == "reload":
        return _run_operation(
            operations.reload, config,
            repo=repo, only=tuple(args.only), force_ci=force_ci, identity=args.identity,
            dry_run=args.dry_run,
        )

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
