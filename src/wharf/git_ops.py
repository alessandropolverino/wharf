"""Pushing the deploy revision to a target's bare git repo.

This push is what stands in for a container registry: after it
succeeds, the target has the exact source tree at ``revision`` sitting
in its bare repo, ready for the remote script to check out and build
locally. No image is ever pushed or pulled.
"""

from __future__ import annotations

from .config import Target
from .ssh import SessionAuth, build_git_ssh_command, run_streaming


def push_revision(
    target: Target,
    remote_repo: str,
    branch: str,
    revision: str,
    auth: SessionAuth,
) -> None:
    """`git push` ``revision`` to ``remote_repo`` on ``target``."""
    url = f"ssh://{target.user}@{target.host}:{target.port}{remote_repo}"
    argv = ["git", "push", url, f"{revision}:refs/heads/{branch}"]
    env = {"GIT_SSH_COMMAND": build_git_ssh_command(target, auth)}
    run_streaming(argv, description=f"git push to {target.name}", env=env)
