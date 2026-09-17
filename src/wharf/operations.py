"""Orchestrates the deploy/down/reload actions across a config's targets.

Targets are always processed sequentially, in ``order`` -- there is no
parallel rollout strategy (mirrors the original scripts' "sequential"
strategy, which was the only one ever used). The first target that
fails stops the run: later targets are left untouched rather than
piling more changes on top of a broken deploy.

Each action also has a dry-run mode that prints, per target, exactly what
would be pushed and piped to the target's shell, without connecting.

``status``, ``logs`` and ``history`` are read-only views of a target.
``rollback`` re-deploys an earlier revision, chosen from the target's own
deploy history (which every successful ``up`` records).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Target, render_repo_template
from .healthcheck import wait_healthy
from .remote_script import (
    render_down,
    render_history,
    render_logs,
    render_reload,
    render_status,
    render_up,
)
from .ssh import SessionAuth, capture_remote_script, remote_command, run_remote_script
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


_HISTORY_LINE_RE = re.compile(r"^(\S+) ([0-9a-f]{40,64}) (deploy|rollback)(?:\t(.*))?$")


@dataclass(frozen=True)
class DeployRecord:
    """One entry of a target's deploy history (see remote_script.render_history)."""

    timestamp: str
    revision: str
    kind: str
    subject: str = ""

    @property
    def short(self) -> str:
        return self.revision[:7]

    def describe(self) -> str:
        return f"{self.short} {self.subject}".rstrip()


def parse_history(text: str) -> list[DeployRecord]:
    """Turn `render_history` output into records, oldest first.

    Only lines shaped like history entries count: the script runs in a
    login shell, so anything a profile script prints is skipped rather
    than mistaken for a deploy.
    """
    records = []
    for line in text.splitlines():
        match = _HISTORY_LINE_RE.match(line)
        if match:
            timestamp, revision, kind, subject = match.groups()
            records.append(DeployRecord(timestamp, revision, kind, subject or ""))
    return records


def rollback_target(records: list[DeployRecord], steps: int = 1) -> tuple[DeployRecord, DeployRecord]:
    """The ``(current, previous)`` pair a rollback of ``steps`` moves between.

    Consecutive deploys of the same revision count once, so a rollback
    always lands on a *different* revision than the current one. Raises
    RuntimeError, with the reason, when the history can't support it.
    """
    distinct: list[DeployRecord] = []
    for record in reversed(records):
        if not distinct or distinct[-1].revision != record.revision:
            distinct.append(record)
    if not distinct:
        raise RuntimeError(
            "no deploy history on this target (deployed by an older wharf, or never deployed); "
            "use `wharf deploy --revision <sha>` instead"
        )
    if steps >= len(distinct):
        raise RuntimeError(
            f"the history only goes back {len(distinct) - 1} distinct revision(s) before "
            f"the current one, so it can't roll back {steps}"
        )
    return distinct[0], distinct[steps]


def _read_history(
    target: Target, auth: SessionAuth, remote_repo: str, remote_dir: str, limit: int | None,
) -> list[DeployRecord]:
    script = render_history(remote_repo=remote_repo, remote_dir=remote_dir, limit=limit)
    output = capture_remote_script(target, auth, script, {}, description=f"read deploy history on {target.name}")
    return parse_history(output)


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


def history(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    limit: int | None = None,
    force_ci: bool | None = None,
    identity: str | None = None,
) -> None:
    """Print each selected target's deploy history, newest first. Read-only."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("History of", target, False))
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            records = _read_history(target, auth, remote_repo, remote_dir, limit)
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc
        if not records:
            print("  no deploy history recorded (deployed by an older wharf, or never deployed)")
            continue
        for index, record in enumerate(reversed(records)):
            marker = "  <- current" if index == 0 else ""
            print(f"  {record.timestamp}  {record.short}  {record.kind:<8}  {record.subject}{marker}")


def rollback(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    steps: int = 1,
    force_ci: bool | None = None,
    identity: str | None = None,
    dry_run: bool = False,
) -> None:
    """Re-deploy, on each selected target, the revision deployed before the current one.

    Each target's own history decides what "before" means (see
    :func:`rollback_target`), and the revision is checked out from the
    bare repo already on that target: nothing is pushed, so this works
    even where the commit no longer exists locally (a CI runner, say).
    Otherwise it's a deploy -- the same script, ``pre_up`` included, then
    the healthcheck -- recorded in the history as a ``rollback``.

    Unlike the other dry runs, this one has to read each target's history
    to know what it would deploy, so it does connect (read-only).
    """
    _check_branch(config)
    for target in config.select_targets(only):
        print(_header("Rolling back", target, dry_run))
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
            records = _read_history(target, auth, remote_repo, remote_dir, None)
            current, previous = rollback_target(records, steps)
            print(f"{current.describe()} -> {previous.describe()} (deployed {previous.timestamp})")
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
                pre_up=target.pre_up,
                kind="rollback",
            )
            env_vars = {"REVISION": previous.revision}
            if dry_run:
                _show_remote_script(target, script, env_vars)
                if target.healthcheck:
                    print(f"Would then poll {target.healthcheck} until it responds")
                continue
            run_remote_script(target, auth, script, env_vars, description=f"rollback on {target.name}")
            if target.healthcheck:
                wait_healthy(target.healthcheck)
        except Exception as exc:  # noqa: BLE001
            raise OperationError(target.name, exc) from exc
