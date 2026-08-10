"""The `wharf setup` bootstrap command.

Provisions what a config file assumes already exists: a dedicated deploy
SSH keypair, a bare git repo per target, and that keypair's public half
installed in each target's ``authorized_keys``. It uses the *operator's*
own existing SSH access (default agent/identity) to provision targets --
there's no chicken-and-egg problem because you need working SSH access
to a host already to be able to set anything up on it.

Anything wharf can't safely automate (adding the private key as a CI
secret) is printed as an instruction rather than attempted -- shelling
out to `gh` (or another provider's CLI) on the operator's behalf is more
magic than a bootstrap command should have.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .config import Config, Target, render_repo_template
from .identity import (
    DEFAULT_KEY_DIR,
    generate_keypair,
    key_comment,
    key_paths,
    resolve_identity,
)
from .operations import _check_branch
from .ssh import SessionAuth, build_ssh_argv, is_ci, run_streaming


def ensure_deploy_keypair(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]:
    """Generate an ed25519 deploy keypair for `identity` if one doesn't
    already exist.

    `identity="default"` keeps writing to `.wharf/deploy_key`, exactly
    as `wharf setup` has always done, for anyone who never names an
    identity. Other identities get their own file under `.wharf/keys/`
    (see `identity.key_paths`).
    """
    private_key, public_key = key_paths(identity, key_dir)
    if private_key.exists():
        print(f"Deploy key for identity '{identity}' already exists at {private_key}, leaving it as-is.")
        return private_key, public_key

    generate_keypair(private_key, key_comment(identity))
    print(f"Generated deploy keypair for identity '{identity}' at {private_key}")
    return private_key, public_key


def _provision_target(config: Config, target: Target, repo: str, public_key: str) -> None:
    remote_repo = render_repo_template(config.remote_repo, repo)
    remote_dir = render_repo_template(target.remote_dir, repo)
    auth = SessionAuth(batch=False)  # operator's own identity, prompts allowed
    argv = build_ssh_argv(target, auth) + ["bash", "-s"]

    remote_repo_q = shlex.quote(remote_repo)
    remote_dir_q = shlex.quote(remote_dir)
    public_key_q = shlex.quote(public_key)
    target_name_q = shlex.quote(target.name)
    # Interpolating remote_repo/target.name straight into a double-quoted
    # echo string would still let $(...) / backticks inside them execute on
    # the target (double quotes don't stop command substitution) -- so the
    # already-shell-quoted variables are passed as separate, unquoted-by-us
    # echo arguments instead, the same way the *_q values are used elsewhere.
    script = f"""\
set -euo pipefail
if [ ! -d {remote_repo_q} ]; then
  git init --bare {remote_repo_q}
  echo "Created bare repo at" {remote_repo_q}
else
  echo "Bare repo already exists at" {remote_repo_q}
fi
mkdir -p {remote_dir_q}
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
if grep -qxF {public_key_q} ~/.ssh/authorized_keys; then
  echo "Deploy key already authorized on" {target_name_q}
else
  echo {public_key_q} >> ~/.ssh/authorized_keys
  echo "Authorized deploy key on" {target_name_q}
fi
"""
    run_streaming(
        argv,
        description=f"setup on {target.name}",
        input_text=script,
    )


def setup(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    identity: str | None = None,
    force_ci: bool | None = None,
) -> None:
    """Bootstrap every selected target: bare repo, remote dir, deploy key."""
    _check_branch(config)
    ci = is_ci() if force_ci is None else force_ci
    resolved_identity = resolve_identity(identity, is_ci=ci)
    private_key, public_key_path = ensure_deploy_keypair(resolved_identity)
    public_key = public_key_path.read_text().strip()

    for target in config.select_targets(only):
        print(f"==> Setting up {target.name} ({target.host}:{target.port})")
        _provision_target(config, target, repo, public_key)

    print()
    print("Setup complete. Remaining manual step:")
    print(f"  Add the contents of {private_key} as your CI's DEPLOY_SSH_KEY secret, e.g.:")
    print(f"    gh secret set DEPLOY_SSH_KEY < {private_key}")
    print(f"  Make sure {private_key.parent} is in your .gitignore.")
