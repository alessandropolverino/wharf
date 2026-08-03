"""Orchestrates the deploy/down/reload actions across a config's targets.

Targets are always processed sequentially, in ``order`` -- there is no
parallel rollout strategy (mirrors the original scripts' "sequential"
strategy, which was the only one ever used). The first target that
fails stops the run: later targets are left untouched rather than
piling more changes on top of a broken deploy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config, Target, render_repo_template
from .healthcheck import wait_healthy
from .remote_script import render_down, render_reload, render_up
from .ssh import SessionAuth, run_remote_script
from .git_ops import push_revision


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
    current = infer_current_branch(cwd)
    if current != config.ensure_branch:
        raise BranchMismatchError(config.ensure_branch, current)


def infer_repo_name(cwd: Path | None = None) -> str:
    """The project name used to fill ``{repo}`` in path templates.

    Prefers the ``origin`` remote's URL basename (works the same way
    locally and in CI, where the checkout is a clone of that remote);
    falls back to the working directory's name if there's no remote
    configured (e.g. a fresh local-only repo).
    """
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        url = result.stdout.strip()
        name = url.rstrip("/").rsplit("/", 1)[-1]
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


def deploy(
    config: Config,
    *,
    repo: str,
    revision: str,
    only: tuple[str, ...] = (),
    force_ci: bool | None = None,
) -> None:
    """Push, checkout, build, and healthcheck each selected target in order."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Deploying {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci)
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            push_revision(target, remote_repo, config.branch, revision, auth)
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
            )
            run_remote_script(
                target, auth, script, {"REVISION": revision},
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
) -> None:
    """Stop (and optionally wipe volumes for) each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Stopping {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci)
        _, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_down(
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                volumes=volumes,
            )
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
) -> None:
    """Re-apply compose (no rebuild) for each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Reloading {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci)
        _, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            script = render_reload(
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
            )
            run_remote_script(
                target, auth, script, {},
                description=f"reload on {target.name}",
            )
            if target.healthcheck:
                wait_healthy(target.healthcheck)
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc
