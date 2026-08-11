"""wharf's command-line interface.

    wharf deploy     <config.yml> [--only NAME...] [--repo NAME] [--revision SHA] [--identity NAME]
    wharf down       <config.yml> [--only NAME...] [--volumes] [--identity NAME]
    wharf reload     <config.yml> [--only NAME...] [--identity NAME]
    wharf ls         <config.yml>
    wharf setup      <config.yml> [--only NAME...] [--identity NAME]
    wharf rotate     <config.yml> [--only NAME...] [--identity NAME]
    wharf identities

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

from . import operations, rotate as rotate_mod, setup as setup_mod
from .config import Config, ConfigError, load_config
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wharf", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_deploy = subparsers.add_parser("deploy", help="push, build, and start each target")
    _add_common(p_deploy)
    p_deploy.add_argument("--revision", default=None, help="commit SHA to deploy; defaults to HEAD")
    _add_ci_flags(p_deploy)
    _add_identity_flag(p_deploy)

    p_down = subparsers.add_parser("down", help="stop each target")
    _add_common(p_down)
    p_down.add_argument("--volumes", action="store_true", help="also remove named/anonymous volumes")
    _add_ci_flags(p_down)
    _add_identity_flag(p_down)

    p_reload = subparsers.add_parser("reload", help="re-apply compose without rebuilding")
    _add_common(p_reload)
    _add_ci_flags(p_reload)
    _add_identity_flag(p_reload)

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


def _load(config_path: Path) -> Config:
    try:
        return load_config(config_path)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        print(f"wharf: {config_path}: {exc}", file=sys.stderr)
        raise SystemExit(2)


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
            print(f"{target.order:>4}  {target.name:<20} {target.user}@{target.host}:{target.port}{secrets_note}{healthcheck_note}")
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

    if args.command == "setup":
        config = _load(args.config)
        repo = _resolve_repo(args)
        try:
            setup_mod.setup(
                config, repo=repo, only=tuple(args.only),
                identity=args.identity, force_ci=_force_ci(args),
            )
        except BranchMismatchError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return 2
        except RemoteCommandError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return exc.returncode
        return 0

    if args.command == "rotate":
        config = _load(args.config)
        repo = _resolve_repo(args)
        try:
            rotate_mod.rotate(
                config, repo=repo, only=tuple(args.only),
                identity=args.identity, force_ci=_force_ci(args),
            )
        except BranchMismatchError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return 2
        except RemoteCommandError as exc:
            print(f"wharf: {exc}", file=sys.stderr)
            return exc.returncode
        return 0

    config = _load(args.config)
    repo = _resolve_repo(args)
    force_ci = _force_ci(args)

    if args.command == "deploy":
        revision = args.revision or operations.infer_revision()
        return _run_operation(
            operations.deploy, config,
            repo=repo, revision=revision, only=tuple(args.only), force_ci=force_ci, identity=args.identity,
        )

    if args.command == "down":
        return _run_operation(
            operations.down, config,
            repo=repo, only=tuple(args.only), volumes=args.volumes, force_ci=force_ci, identity=args.identity,
        )

    if args.command == "reload":
        return _run_operation(
            operations.reload, config,
            repo=repo, only=tuple(args.only), force_ci=force_ci, identity=args.identity,
        )

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
