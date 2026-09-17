"""Pushing the deploy revision to a target's bare git repo.

This push is what stands in for a container registry: after it
succeeds, the target has the exact source tree at ``revision`` sitting
in its bare repo, ready for the remote script to check out and build
locally. No image is ever pushed or pulled.
"""

from __future__ import annotations

from .config import Target
from .ssh import SessionAuth, build_git_ssh_command, run_streaming


def push_url(target: Target, remote_repo: str) -> str:
    """The ``ssh://`` URL of ``remote_repo`` on ``target``.

    Built from :attr:`Target.address`, which brackets an IPv6 host:
    unbracketed, git can't tell where the address ends and the port
    begins, and hands ssh a mangled destination (``deploy@2001:db8::1:22``)
    with no ``-p``.
    """
    return f"ssh://{target.user}@{target.address}{remote_repo}"


def push_refspec(revision: str, branch: str) -> str:
    """Push ``revision`` as ``branch`` on the target's bare repo."""
    return f"{revision}:refs/heads/{branch}"


def push_revision(
    target: Target,
    remote_repo: str,
    branch: str,
    revision: str,
    auth: SessionAuth,
) -> None:
    """`git push` ``revision`` to ``remote_repo`` on ``target``."""
    argv = ["git", "push", push_url(target, remote_repo), push_refspec(revision, branch)]
    env = {"GIT_SSH_COMMAND": build_git_ssh_command(target, auth)}
    run_streaming(argv, description=f"git push to {target.name}", env=env)
