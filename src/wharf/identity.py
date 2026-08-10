"""Named deploy-key identities: resolution, on-disk layout, and the
`authorized_keys` comment marker used as rotation bookkeeping.

Shared by `setup.py`, `rotate.py`, `cli.py`'s `identities` command, and
`ssh.py`'s `SessionAuth` -- resolving an identity name and finding its
key files happens in exactly one place so those consumers can't drift
apart on what a marker or a file path looks like.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IDENTITY = "default"
CI_IDENTITY = "ci"
DEFAULT_KEY_DIR = Path(".wharf")
DEFAULT_KEY_NAME = "deploy_key"

_IDENTITY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class InvalidIdentityError(ValueError):
    """Raised when an identity name fails validation."""


def validate_identity_name(identity: str) -> str:
    """Restrict identity names to a charset safe to embed directly in a
    filename and in a `grep` pattern on the remote host, with no
    metacharacters to escape."""
    if not _IDENTITY_NAME_RE.fullmatch(identity):
        raise InvalidIdentityError(
            f"identity {identity!r} must match {_IDENTITY_NAME_RE.pattern}"
        )
    return identity


def resolve_identity(explicit: str | None, *, is_ci: bool) -> str:
    """`--identity` wins if given; otherwise `ci` in CI, `default` locally.

    `default` exists solely so that anyone who never passes --identity
    gets exactly today's behavior, unchanged.
    """
    if explicit is not None:
        return validate_identity_name(explicit)
    return CI_IDENTITY if is_ci else DEFAULT_IDENTITY


def key_paths(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]:
    """Private/public key file paths for `identity`.

    `default` keeps the exact path wharf has always used
    (`.wharf/deploy_key`) -- existing installs need no migration. Any
    other identity gets its own file under `.wharf/keys/`.
    """
    if identity == DEFAULT_IDENTITY:
        private_key = key_dir / DEFAULT_KEY_NAME
    else:
        private_key = key_dir / "keys" / f"{identity}_key"
    return private_key, private_key.with_name(private_key.name + ".pub")


def key_comment(identity: str) -> str:
    """The `ssh-keygen -C` comment for `identity`.

    This string ends up verbatim in the target's `authorized_keys` line,
    which is the entire mechanism `rotate` uses to find "lines that
    belong to this identity" -- no separate state file. `default` keeps
    the literal legacy comment so existing installs stay recognizable.
    """
    if identity == DEFAULT_IDENTITY:
        return "wharf-deploy"
    return f"wharf:{identity}"


def staged_key_paths(private_key: Path) -> tuple[Path, Path]:
    """Where `rotate` stages a replacement keypair before promoting it."""
    return (
        private_key.with_name(private_key.name + ".new"),
        private_key.with_name(private_key.name + ".new.pub"),
    )


def generate_keypair(private_key: Path, comment: str) -> None:
    """Shell out to `ssh-keygen` to write an ed25519 keypair at
    `private_key` (and `private_key`.pub), creating parent directories
    as needed.

    Shells out rather than adding a crypto library dependency -- SSH is
    already a hard requirement for wharf to do anything at all.
    """
    private_key.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment,
            "-f", str(private_key),
        ],
        check=True,
    )


@dataclass(frozen=True)
class IdentityInfo:
    """One identity's local key-file state, as reported by `wharf identities`."""

    name: str
    private_key: Path
    public_key: Path
    comment: str
    staged_pending: bool


def _identity_info(name: str, private_key: Path, public_key: Path) -> IdentityInfo:
    staged_private, _ = staged_key_paths(private_key)
    return IdentityInfo(
        name=name,
        private_key=private_key,
        public_key=public_key,
        comment=key_comment(name),
        staged_pending=staged_private.exists(),
    )


def list_identities(key_dir: Path = DEFAULT_KEY_DIR) -> list[IdentityInfo]:
    """Every identity with a local key file: `default` (if present) plus
    every named identity under `.wharf/keys/`.

    Local-only by design -- reads nothing but the current checkout's
    disk, never SSHes anywhere. See `wharf identities --help`.
    """
    found: list[IdentityInfo] = []

    default_private, default_public = key_paths(DEFAULT_IDENTITY, key_dir)
    if default_private.exists():
        found.append(_identity_info(DEFAULT_IDENTITY, default_private, default_public))

    keys_dir = key_dir / "keys"
    if keys_dir.is_dir():
        for private in sorted(keys_dir.glob("*_key")):
            # Skip staged keys (.new, .new.pub) which are tracked separately
            if private.name.endswith(".new") or private.name.endswith(".pub"):
                continue
            public = private.with_name(private.name + ".pub")
            name = private.name[: -len("_key")]
            found.append(_identity_info(name, private, public))

    return found
