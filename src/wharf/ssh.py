"""SSH command construction and process execution for wharf.

Every remote operation goes through the same two building blocks:

- :class:`SessionAuth` figures out *how* to authenticate and whether
  prompts are allowed, based on the environment (local terminal vs CI
  runner).
- :func:`build_ssh_argv` / :func:`build_git_ssh_command` turn a target's
  ``host_key`` into a pinned, single-use known_hosts file, so we never
  fall back to "accept any host key" -- the config file itself is the
  source of truth for what a target's host key should be.

:func:`run_streaming` is the one place that shells out to a subprocess.
It deliberately does *not* capture stdout/stderr: leaving them attached
to the parent's means `git push` progress, `ssh` prompts, and
`docker compose build` output all show up live, exactly as if you'd
typed the command yourself.
"""

from __future__ import annotations

import atexit
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Target


class RemoteCommandError(RuntimeError):
    """Raised when a streamed subprocess exits non-zero."""

    def __init__(self, description: str, returncode: int):
        super().__init__(f"{description} failed (exit {returncode})")
        self.returncode = returncode


def is_ci() -> bool:
    """True when running under a CI runner.

    Relies on the ``CI`` environment variable, which GitHub Actions (and
    most other CI systems -- GitLab CI, CircleCI, Travis, ...) set to
    "true" automatically. This is the one signal that decides whether
    wharf can rely on an interactive terminal for prompts.
    """
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class SessionAuth:
    """Resolved auth/interactivity mode for the current invocation.

    - ``batch``: True disables SSH's interactive prompting
      (``BatchMode=yes``). Must be True in CI (nothing can answer a
      passphrase/password prompt there) and False locally so the
      operator's terminal can handle passphrase/password prompts
      normally.
    - ``identity_file``: an explicit private key to use (from
      ``DEPLOY_SSH_KEY`` in CI). None means "use the default SSH
      agent/identity," which is what local runs should do.
    """

    batch: bool
    identity_file: Path | None = None

    @classmethod
    def resolve(cls, *, force_ci: bool | None = None) -> "SessionAuth":
        """Decide the auth mode for this run.

        ``force_ci`` overrides autodetection -- this is what the CLI's
        ``--ci``/``--interactive`` flags set. Left as None, autodetection
        via :func:`is_ci` applies.
        """
        ci = is_ci() if force_ci is None else force_ci
        if not ci:
            return cls(batch=False, identity_file=None)

        key_material = os.environ.get("DEPLOY_SSH_KEY")
        if not key_material:
            raise RuntimeError(
                "DEPLOY_SSH_KEY is required when running in CI mode "
                "(pass --interactive to force local/interactive auth instead)"
            )
        # mkstemp (unlike a hand-built path) creates the file itself with
        # O_EXCL, so it can't follow a pre-existing symlink at a predictable
        # path, and the name isn't guessable. Cleaned up at process exit
        # since the file needs to outlive individual SSH invocations (it's
        # re-resolved once per target).
        fd, key_path_str = tempfile.mkstemp(prefix="wharf-deploy-key-")
        key_path = Path(key_path_str)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(key_material.rstrip("\n") + "\n")
            key_path.chmod(0o600)
        except BaseException:
            key_path.unlink(missing_ok=True)
            raise
        atexit.register(key_path.unlink, missing_ok=True)
        return cls(batch=True, identity_file=key_path)


def _known_hosts_line(target: Target) -> str:
    host = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
    return f"{host} {target.host_key}\n"


def _pin_known_hosts(target: Target) -> Path:
    """Write a single-purpose known_hosts file pinned to this target's key.

    Using a fresh file per invocation (rather than the operator's real
    ``~/.ssh/known_hosts``) means a stale or unrelated entry elsewhere on
    the machine can never substitute for the key committed in the config
    -- the config file is the sole source of truth for host identity.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="wharf-known-hosts-", delete=False
    )
    handle.write(_known_hosts_line(target))
    handle.close()
    return Path(handle.name)


def _ssh_option_argv(target: Target, auth: SessionAuth) -> list[str]:
    """The `ssh` argv up to (but not including) the destination.

    Split out from :func:`build_ssh_argv` because ``GIT_SSH_COMMAND``
    (see :func:`build_git_ssh_command`) must **not** include a
    destination itself -- git appends its own `[-p port] user@host
    <command>` onto whatever ``GIT_SSH_COMMAND`` contains, so baking the
    destination into that string too makes ssh see two destinations and
    treat the second one as a remote command to execute.
    """
    known_hosts = _pin_known_hosts(target)
    argv = [
        "ssh",
        "-p", str(target.port),
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
    ]
    if auth.batch:
        argv += ["-o", "BatchMode=yes"]
    if auth.identity_file:
        argv += ["-i", str(auth.identity_file), "-o", "IdentitiesOnly=yes"]
    return argv


def build_ssh_argv(target: Target, auth: SessionAuth) -> list[str]:
    """The argv for a standalone `ssh` invocation against ``target``."""
    return _ssh_option_argv(target, auth) + [f"{target.user}@{target.host}"]


def build_git_ssh_command(target: Target, auth: SessionAuth) -> str:
    """A ``GIT_SSH_COMMAND`` string: options only, no destination.

    Git appends `[-p port] user@host <command>` itself when it invokes
    this, based on the push URL -- see :func:`_ssh_option_argv`.
    """
    return " ".join(shlex.quote(part) for part in _ssh_option_argv(target, auth))


def run_streaming(
    argv: list[str],
    *,
    description: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> None:
    """Run ``argv`` with output streamed live; raise on non-zero exit.

    stdout/stderr are intentionally left attached to the parent process
    rather than captured -- see the module docstring.
    """
    full_env = {**os.environ, **env} if env else None
    kwargs: dict[str, object] = {}
    if input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = True
    result = subprocess.run(argv, env=full_env, **kwargs)
    if result.returncode != 0:
        raise RemoteCommandError(description, result.returncode)


def run_remote_script(
    target: Target,
    auth: SessionAuth,
    script: str,
    env_vars: dict[str, str],
    *,
    description: str,
) -> None:
    """Run ``script`` on ``target`` via a login `bash -s`, piped over stdin.

    ``env_vars`` are set as a shell-level prefix (``KEY=value bash -l -s``)
    on the remote command line, the same technique the original
    per-repo deploy scripts used -- it keeps the script text itself
    free of assumptions about how its inputs arrived, which is handy
    when eyeballing logs.

    ``-l`` (login shell) is required, not cosmetic: it's what makes bash
    source ``/etc/profile`` (and thus ``/etc/profile.d/*.sh``) before
    running the script, which is where docs/configuration.md says
    Infisical's machine-identity credentials are expected to live. A
    plain non-login ``bash -s`` would silently skip that and leave those
    variables unset.
    """
    argv = build_ssh_argv(target, auth)
    prefix = [f"{key}={shlex.quote(value)}" for key, value in env_vars.items()]
    argv += prefix + ["bash", "-l", "-s"]
    run_streaming(argv, description=description, input_text=script)
