"""Orchestrates the deploy/down/reload actions across a config's targets.

Targets are always processed sequentially, in ``order`` -- there is no
parallel rollout strategy (mirrors the original scripts' "sequential"
strategy, which was the only one ever used). The first target that
fails stops the run: later targets are left untouched rather than
piling more changes on top of a broken deploy.

Each action also has a dry-run mode that prints, per target, exactly what
would be pushed and piped to the target's shell, without connecting.

``status`` and ``logs`` are read-only views of a target.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config, Target, render_repo_template
from .healthcheck import wait_healthy
from .remote_script import render_down, render_logs, render_reload, render_status, render_up
from .ssh import SessionAuth, remote_command, run_remote_script
from .git_ops import push_refspec, push_revision, push_url


class OperationError(RuntimeError):
    """Raised when a target fails; carries which target for the caller."""

    def __init__(self, target_name: str, cause: Exception):
        super().__init__(f"target '{target_name}': {cause}")
        self.target_name = target_name
        self.cause = cause


class BranchMismatchError(RuntimeError):
    """Raised when a config's ``ensure_branch`` doesn't match the checkout."""

    def __init__(self, expected: str, actual: str):
        super().__init__(
            f"ensure_branch: this config requires branch '{expected}', "
            f"but the current checkout is on '{actual}'"
        )


def infer_current_branch(cwd: Path | None = None) -> str:
    """The current checkout's branch name, for ``ensure_branch`` checks."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _check_branch(config: Config, cwd: Path | None = None) -> None:
    """No-op unless the config sets ``ensure_branch``; guards against
    e.g. running a prod config from a feature branch by accident."""
    if config.ensure_branch is None:
        return
    try:
        current = infer_current_branch(cwd)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BranchMismatchError(config.ensure_branch, f"<could not determine current branch: {exc}>") from exc
    if current != config.ensure_branch:
        raise BranchMismatchError(config.ensure_branch, current)


def infer_repo_name(cwd: Path | None = None) -> str:
    """The project name used to fill ``{repo}`` in path templates.

    Prefers the ``origin`` remote's URL basename (works the same way
    locally and in CI, where the checkout is a clone of that remote);
    falls back to the working directory's name if there's no remote
    configured (e.g. a fresh local-only repo) or no `git` to ask.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True,
        )
    except OSError:
        result = None
    url = result.stdout.strip() if result is not None and result.returncode == 0 else ""
    if url:
        # The last component of ".../owner/name.git" or scp-style "host:name.git".
        name = re.split(r"[/:]", url.rstrip("/"))[-1]
        return name[:-4] if name.endswith(".git") else name
    return Path(cwd or Path.cwd()).resolve().name


def infer_revision(cwd: Path | None = None) -> str:
    """The commit SHA to deploy: HEAD of the local checkout."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _remote_repo_and_dir(config: Config, target: Target, repo: str) -> tuple[str, str]:
    remote_repo = render_repo_template(config.remote_repo, repo)
    remote_dir = render_repo_template(target.remote_dir, repo)
    return remote_repo, remote_dir


def _header(verb: str, target: Target, dry_run: bool) -> str:
    prefix = "[dry run] " if dry_run else ""
    return f"==> {prefix}{verb} {target.name} ({target.address})"


def _show_remote_script(target: Target, script: str, env_vars: dict[str, str]) -> None:
    """Dry run: print what :func:`run_remote_script` would pipe to ``target``.

    Framed as a heredoc because that's exactly what happens: the script is
    fed to the remote shell's stdin.
    """
    command = " ".join(remote_command(env_vars))
    print(f"Would run on {target.user}@{target.host} (port {target.port}): {command} <<'WHARF_SCRIPT'")
    print(script, end="")
    print("WHARF_SCRIPT")


def deploy(
    config: Config,
    *,
    repo: str,
    revision: str,
    only: tuple[str, ...] = (),
    force_ci: bool | None = None,
    identity: str | None = None,
    dry_run: bool = False,
) -> None:
    """Push, checkout, build, and healthcheck each selected target in order.

    With ``dry_run``, print each target's push and deploy script instead:
    nothing is connected to, so no credentials are needed either.
    """
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("Deploying", target, dry_run))
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
                pre_up=target.pre_up,
            )
            env_vars = {"REVISION": revision}
            if dry_run:
                push = f"git push {push_url(target, remote_repo)} {push_refspec(revision, config.branch)}"
                print(f"Would run locally: {push}")
                _show_remote_script(target, script, env_vars)
                if target.healthcheck:
                    print(f"Would then poll {target.healthcheck} until it responds")
                continue
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            push_revision(target, remote_repo, config.branch, revision, auth)
            run_remote_script(
                target, auth, script, env_vars,
                description=f"deploy on {target.name}",
            )
            if target.healthcheck:
                wait_healthy(target.healthcheck)
        except Exception as exc:  # noqa: BLE001 - re-raised with target context below
            raise OperationError(target.name, exc) from exc


def down(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    volumes: bool = False,
    force_ci: bool | None = None,
    identity: str | None = None,
    dry_run: bool = False,
) -> None:
    """Stop (and optionally wipe volumes for) each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("Stopping", target, dry_run))
        _, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_down(
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                volumes=volumes,
            )
            if dry_run:
                _show_remote_script(target, script, {})
                continue
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            run_remote_script(
                target, auth, script, {},
                description=f"down on {target.name}",
            )
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc


def reload(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    force_ci: bool | None = None,
    identity: str | None = None,
    dry_run: bool = False,
) -> None:
    """Re-apply compose (no rebuild) for each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("Reloading", target, dry_run))
        _, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_reload(
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
            )
            if dry_run:
                _show_remote_script(target, script, {})
                if target.healthcheck:
                    print(f"Would then poll {target.healthcheck} until it responds")
                continue
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            run_remote_script(
                target, auth, script, {},
                description=f"reload on {target.name}",
            )
            if target.healthcheck:
                wait_healthy(target.healthcheck)
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc


def status(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    force_ci: bool | None = None,
    identity: str | None = None,
) -> None:
    """Report each selected target's checked-out revision, last deploy, lock, and services. Read-only."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("Status of", target, False))
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_status(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
            )
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            run_remote_script(target, auth, script, {}, description=f"status on {target.name}")
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc


def logs(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    services: tuple[str, ...] = (),
    follow: bool = False,
    tail: str = "100",
    since: str | None = None,
    force_ci: bool | None = None,
    identity: str | None = None,
) -> None:
    """Show (or follow) each selected target's compose logs. Read-only.

    Following never returns on its own, so it's only allowed for a single
    target -- the CLI checks that before getting here.
    """
    _check_branch(config)
    targets = config.select_targets(only)
    if follow and len(targets) != 1:
        raise ValueError("--follow streams one target at a time")
    for target in targets:
        print(_header("Logs from", target, False))
        _, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_logs(
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
                services=services,
                follow=follow,
                tail=tail,
                since=since,
            )
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            run_remote_script(target, auth, script, {}, description=f"logs on {target.name}")
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc

