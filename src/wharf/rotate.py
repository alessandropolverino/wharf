"""The `wharf rotate` command.

Unlike `setup` (purely additive, idempotent "leave it as-is"), `rotate`
always produces a new key and actively removes the old one from every
selected target's `authorized_keys` -- using the identity's comment
marker (see `identity.key_comment`) as the sole bookkeeping for "which
lines belong to this identity," with no separate state file.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .config import Config, Target
from .identity import (
    DEFAULT_KEY_DIR,
    generate_keypair,
    key_comment,
    key_paths,
    resolve_identity,
    staged_key_paths,
)
from .operations import _check_branch
from .ssh import SessionAuth, build_ssh_argv, is_ci, run_streaming


def _ensure_staged_keypair(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]:
    """Generate `identity`'s replacement keypair to a staged path, or
    reuse one already staged from a previous interrupted `rotate` run
    instead of generating another."""
    live_private, _ = key_paths(identity, key_dir)
    staged_private, staged_public = staged_key_paths(live_private)
    if staged_private.exists():
        print(f"Staged rotation key for '{identity}' already exists at {staged_private}, reusing it.")
        return staged_private, staged_public

    generate_keypair(staged_private, key_comment(identity))
    print(f"Generated staged rotation key for '{identity}' at {staged_private}")
    return staged_private, staged_public


def _render_rotate_script(new_public_key: str, marker_pattern: str, target_name: str) -> str:
    """The remote script for one target: drop every `authorized_keys`
    line matching `marker_pattern` (this identity's old key, and -- as a
    harmless no-op -- the about-to-be-superseded copy of the new key's
    own line, which carries the same marker), then unconditionally
    re-append the new key. Filter-then-append instead of the more
    obvious add-then-remove: doing it in one pass means there's never a
    write where the live file has neither key, and it's naturally
    idempotent on retry since the re-append always happens regardless of
    whether the key was already present.

    `marker_pattern` must be a `grep` basic-regex pattern with no
    metacharacters needing escaping -- identity names are restricted to
    `[a-z0-9-]` by `identity.validate_identity_name`, so ` wharf:<name>$`
    is always safe to use unescaped.

    The temp file is created with `mktemp` *inside* `~/.ssh` (not the
    default `/tmp`) so the final `mv` is a same-filesystem rename: a true
    atomic replace rather than a cross-device copy+unlink that could
    leave `authorized_keys` truncated if interrupted, and the file
    inherits `~/.ssh`'s permissions/SELinux context instead of `/tmp`'s
    (which would make sshd refuse to read it on SELinux-enforcing
    hosts). `grep -v`'s exit code 1 ("no lines matched", i.e. nothing to
    filter) is the only failure tolerated -- any other exit code aborts
    the script under `set -e` rather than risk `mv`-ing an empty or
    partial file over the live one.
    """
    new_public_key_q = shlex.quote(new_public_key)
    marker_pattern_q = shlex.quote(marker_pattern)
    target_name_q = shlex.quote(target_name)
    return f"""\
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
tmp_file=$(mktemp ~/.ssh/authorized_keys.XXXXXX)
grep -v -- {marker_pattern_q} ~/.ssh/authorized_keys > "$tmp_file" || [ $? -eq 1 ]
echo {new_public_key_q} >> "$tmp_file"
chmod 600 "$tmp_file"
mv "$tmp_file" ~/.ssh/authorized_keys
echo "Rotated deploy key on" {target_name_q}
"""


def _rotate_target(target: Target, new_public_key: str, marker_pattern: str) -> None:
    auth = SessionAuth(batch=False)  # operator's own identity, same as setup
    argv = build_ssh_argv(target, auth) + ["bash", "-s"]
    script = _render_rotate_script(new_public_key, marker_pattern, target.name)
    run_streaming(argv, description=f"rotate on {target.name}", input_text=script)


def rotate(
    config: Config,
    *,
    repo: str,
    only: tuple[str, ...] = (),
    identity: str | None = None,
    force_ci: bool | None = None,
) -> None:
    """Replace an identity's deploy key across every selected target.

    Generates the replacement to a staged path first; only promotes it
    to the live key file (deleting the old one) after every selected
    target has confirmed the swap. If interrupted partway, the live key
    is untouched and the staged key persists for the next `rotate` call
    to pick up and continue from.
    """
    _check_branch(config)
    ci = is_ci() if force_ci is None else force_ci
    resolved_identity = resolve_identity(identity, is_ci=ci)

    live_private, live_public = key_paths(resolved_identity)
    staged_private, staged_public = _ensure_staged_keypair(resolved_identity)
    new_public_key = staged_public.read_text().strip()
    marker_pattern = f" {key_comment(resolved_identity)}$"

    for target in config.select_targets(only):
        print(f"==> Rotating '{resolved_identity}' on {target.name} ({target.host}:{target.port})")
        _rotate_target(target, new_public_key, marker_pattern)

    staged_private.rename(live_private)
    staged_public.rename(live_public)

    print()
    print("Rotation complete. Remaining manual step:")
    print("  Update your CI's DEPLOY_SSH_KEY secret with the new key, e.g.:")
    print(f"    gh secret set DEPLOY_SSH_KEY < {live_private}")
