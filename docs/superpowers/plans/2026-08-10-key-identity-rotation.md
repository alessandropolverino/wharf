# Named Key Identities and `wharf rotate` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give wharf named, rotatable deploy-key identities — replacing the single undifferentiated `.wharf/deploy_key` with a `default`/`ci`/named-identity model, a `wharf rotate` command that actually removes a stale key instead of only ever appending, and a `wharf identities` command to see what's on disk.

**Architecture:** A new `identity.py` module owns identity-name resolution, on-disk key paths, and the `authorized_keys` comment marker used as self-describing rotation bookkeeping (no separate state file). `setup.py` and a new `rotate.py` both build on it; `ssh.py`'s `SessionAuth` gains an `identity` parameter so a locally-run named identity (not just CI) can use a dedicated key file. `cli.py` wires up two new subcommands and an `--identity` flag on the existing ones.

**Tech Stack:** Python 3.10+, stdlib only (`subprocess`, `pathlib`, `re`, `dataclasses`), shelling out to `ssh-keygen`/`bash` exactly as the existing code already does. No new dependencies.

## Global Constraints

- Identity names must match `^[a-z0-9][a-z0-9-]*$`.
- `"default"` identity → `.wharf/deploy_key` / `.wharf/deploy_key.pub`, `authorized_keys` comment `wharf-deploy` (byte-identical to today's behavior — no migration for existing users).
- Any other identity `<name>` → `.wharf/keys/<name>_key` / `.wharf/keys/<name>_key.pub`, comment `wharf:<name>`.
- Identity resolution order everywhere: explicit `--identity` > `"ci"` if CI-detected > `"default"`.
- CI auth (`DEPLOY_SSH_KEY` env var) is unaffected by identity naming — CI always reads that one env var regardless of which identity resolved.
- No per-target key files, no automated cross-identity retirement, no human/teammate key management — all explicitly out of scope (see spec's Non-goals).
- Every new/changed file gets its test updated or created in the same task, not deferred.
- Full spec: `docs/superpowers/specs/2026-08-10-key-identity-rotation-design.md`.

---

### Task 1: `identity.py` — resolution, paths, comment marker

**Files:**
- Create: `src/wharf/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Produces: `DEFAULT_IDENTITY = "default"`, `CI_IDENTITY = "ci"`, `DEFAULT_KEY_DIR = Path(".wharf")`, `DEFAULT_KEY_NAME = "deploy_key"`; `InvalidIdentityError(ValueError)`; `validate_identity_name(identity: str) -> str`; `resolve_identity(explicit: str | None, *, is_ci: bool) -> str`; `key_paths(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]`; `key_comment(identity: str) -> str`; `staged_key_paths(private_key: Path) -> tuple[Path, Path]`; `generate_keypair(private_key: Path, comment: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_identity.py`:

```python
import subprocess

import pytest

from wharf.identity import (
    CI_IDENTITY,
    DEFAULT_IDENTITY,
    InvalidIdentityError,
    generate_keypair,
    key_comment,
    key_paths,
    resolve_identity,
    staged_key_paths,
    validate_identity_name,
)


def test_resolve_identity_prefers_explicit_over_everything():
    assert resolve_identity("release-bot", is_ci=True) == "release-bot"
    assert resolve_identity("release-bot", is_ci=False) == "release-bot"


def test_resolve_identity_defaults_to_ci_when_detected():
    assert resolve_identity(None, is_ci=True) == CI_IDENTITY


def test_resolve_identity_defaults_to_default_when_not_ci():
    assert resolve_identity(None, is_ci=False) == DEFAULT_IDENTITY


@pytest.mark.parametrize("bad_name", ["", "CI", "my_bot", "-leading-hyphen", "has space", "wharf:ci"])
def test_validate_identity_name_rejects_invalid_names(bad_name):
    with pytest.raises(InvalidIdentityError):
        validate_identity_name(bad_name)


@pytest.mark.parametrize("good_name", ["default", "ci", "release-bot", "ci-staging", "a"])
def test_validate_identity_name_accepts_valid_names(good_name):
    assert validate_identity_name(good_name) == good_name


def test_resolve_identity_validates_explicit_name():
    with pytest.raises(InvalidIdentityError):
        resolve_identity("Not Valid", is_ci=False)


def test_key_paths_default_identity_uses_legacy_layout(tmp_path):
    private, public = key_paths(DEFAULT_IDENTITY, tmp_path)
    assert private == tmp_path / "deploy_key"
    assert public == tmp_path / "deploy_key.pub"


def test_key_paths_named_identity_uses_keys_subdir(tmp_path):
    private, public = key_paths("ci", tmp_path)
    assert private == tmp_path / "keys" / "ci_key"
    assert public == tmp_path / "keys" / "ci_key.pub"


def test_key_comment_default_identity_is_unchanged_legacy_string():
    assert key_comment(DEFAULT_IDENTITY) == "wharf-deploy"


def test_key_comment_named_identity_is_namespaced():
    assert key_comment("ci") == "wharf:ci"


def test_staged_key_paths_appends_new_suffix(tmp_path):
    private = tmp_path / "keys" / "ci_key"
    staged_private, staged_public = staged_key_paths(private)
    assert staged_private == tmp_path / "keys" / "ci_key.new"
    assert staged_public == tmp_path / "keys" / "ci_key.new.pub"


def test_generate_keypair_creates_parent_dir_and_ed25519_key(tmp_path):
    private_key = tmp_path / "keys" / "ci_key"
    generate_keypair(private_key, "wharf:ci")
    assert private_key.exists()
    public_key = private_key.with_name(private_key.name + ".pub")
    assert public_key.exists()
    assert public_key.read_text().strip().endswith("wharf:ci")
    result = subprocess.run(["ssh-keygen", "-lf", str(public_key)], capture_output=True, text=True, check=True)
    assert "ED25519" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wharf.identity'`

- [ ] **Step 3: Write the implementation**

Create `src/wharf/identity.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: PASS (all tests green)

- [ ] **Step 5: Commit**

```bash
git add src/wharf/identity.py tests/test_identity.py
git commit -m "Add identity.py: resolution, key paths, and authorized_keys marker"
```

---

### Task 2: `identity.py` — `list_identities` for the `identities` command

**Files:**
- Modify: `src/wharf/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `Task 1`'s `key_paths`, `key_comment`, `staged_key_paths`, `DEFAULT_IDENTITY`, `DEFAULT_KEY_DIR`, `generate_keypair`.
- Produces: `IdentityInfo` (frozen dataclass: `name: str`, `private_key: Path`, `public_key: Path`, `comment: str`, `staged_pending: bool`); `list_identities(key_dir: Path = DEFAULT_KEY_DIR) -> list[IdentityInfo]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_identity.py`:

```python
from wharf.identity import IdentityInfo, list_identities


def test_list_identities_empty_when_no_wharf_dir(tmp_path):
    assert list_identities(tmp_path / ".wharf") == []


def test_list_identities_finds_default_and_named_keys(tmp_path):
    key_dir = tmp_path / ".wharf"
    default_private, _ = key_paths(DEFAULT_IDENTITY, key_dir)
    generate_keypair(default_private, key_comment(DEFAULT_IDENTITY))
    ci_private, _ = key_paths("ci", key_dir)
    generate_keypair(ci_private, key_comment("ci"))

    identities = list_identities(key_dir)

    assert {info.name for info in identities} == {"default", "ci"}
    by_name = {info.name: info for info in identities}
    assert by_name["default"].private_key == default_private
    assert by_name["default"].comment == "wharf-deploy"
    assert by_name["ci"].comment == "wharf:ci"
    assert by_name["default"].staged_pending is False


def test_list_identities_flags_staged_pending_rotation(tmp_path):
    key_dir = tmp_path / ".wharf"
    ci_private, _ = key_paths("ci", key_dir)
    generate_keypair(ci_private, key_comment("ci"))
    staged_private, staged_public = staged_key_paths(ci_private)
    staged_private.write_text("fake private key material")
    staged_public.write_text("fake public key material")

    [info] = list_identities(key_dir)

    assert info.staged_pending is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL with `ImportError: cannot import name 'IdentityInfo'`

- [ ] **Step 3: Write the implementation**

Append to `src/wharf/identity.py`:

```python
from dataclasses import dataclass


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
        for public in sorted(keys_dir.glob("*_key.pub")):
            private = public.with_name(public.name[: -len(".pub")])
            name = private.name[: -len("_key")]
            found.append(_identity_info(name, private, public))

    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/wharf/identity.py tests/test_identity.py
git commit -m "Add identity.list_identities for the wharf identities command"
```

---

### Task 3: `setup.py` — identity-aware key generation and provisioning

**Files:**
- Modify: `src/wharf/setup.py`
- Create: `tests/test_setup.py`

**Interfaces:**
- Consumes: `identity.key_paths`, `identity.key_comment`, `identity.generate_keypair`, `identity.resolve_identity`, `identity.DEFAULT_KEY_DIR`, `identity.DEFAULT_IDENTITY`; `ssh.is_ci`.
- Produces: `ensure_deploy_keypair(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]`; `setup(config, *, repo, only=(), identity: str | None = None, force_ci: bool | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_setup.py`:

```python
from wharf import setup as setup_module
from wharf.config import load_config
from wharf.setup import ensure_deploy_keypair

CONFIG_TEXT = """\
version: 1
remote_repo: /srv/git/{repo}.git
targets:
  - name: app
    remote_dir: /opt/deploys/{repo}/app
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
"""


def test_ensure_deploy_keypair_default_identity_uses_legacy_path(tmp_path):
    key_dir = tmp_path / ".wharf"
    private, public = ensure_deploy_keypair("default", key_dir=key_dir)
    assert private == key_dir / "deploy_key"
    assert public == key_dir / "deploy_key.pub"
    assert "wharf-deploy" in public.read_text()


def test_ensure_deploy_keypair_named_identity_uses_keys_subdir(tmp_path):
    key_dir = tmp_path / ".wharf"
    private, public = ensure_deploy_keypair("ci", key_dir=key_dir)
    assert private == key_dir / "keys" / "ci_key"
    assert "wharf:ci" in public.read_text()


def test_ensure_deploy_keypair_is_idempotent(tmp_path, capsys):
    key_dir = tmp_path / ".wharf"
    private1, _ = ensure_deploy_keypair("ci", key_dir=key_dir)
    content1 = private1.read_text()

    private2, _ = ensure_deploy_keypair("ci", key_dir=key_dir)

    assert private2.read_text() == content1
    assert "leaving it as-is" in capsys.readouterr().out


def test_setup_defaults_to_default_identity_when_not_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(setup_module, "_provision_target", lambda *a, **k: None)

    setup_module.setup(config, repo="app")

    assert (tmp_path / ".wharf" / "deploy_key").exists()
    assert not (tmp_path / ".wharf" / "keys" / "ci_key").exists()


def test_setup_resolves_ci_identity_when_running_in_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(setup_module, "_provision_target", lambda *a, **k: None)

    setup_module.setup(config, repo="app")

    assert (tmp_path / ".wharf" / "keys" / "ci_key").exists()
    assert not (tmp_path / ".wharf" / "deploy_key").exists()


def test_setup_explicit_identity_overrides_ci_detection(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(setup_module, "_provision_target", lambda *a, **k: None)

    setup_module.setup(config, repo="app", identity="release-bot")

    assert (tmp_path / ".wharf" / "keys" / "release-bot_key").exists()


def test_setup_passes_resolved_identitys_public_key_to_provision_target(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}

    def fake_provision(config, target, repo, public_key):
        seen["public_key"] = public_key

    monkeypatch.setattr(setup_module, "_provision_target", fake_provision)
    setup_module.setup(config, repo="app")

    assert seen["public_key"].endswith("wharf-deploy")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_setup.py -v`
Expected: FAIL — `ensure_deploy_keypair()` currently takes no `identity` positional arg (`TypeError`), and `setup()` has no `identity`/`force_ci` params.

- [ ] **Step 3: Write the implementation**

In `src/wharf/setup.py`, replace the imports, module-level constants, `ensure_deploy_keypair`, and `setup` function:

```python
from __future__ import annotations

import shlex
from pathlib import Path

from .config import Config, Target, render_repo_template
from .identity import (
    DEFAULT_IDENTITY,
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
```

Leave `_provision_target` exactly as-is — it only ever receives an already-formed `public_key` string, so it needs no identity awareness of its own. Then replace `setup`:

```python
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
```

`DEFAULT_KEY_NAME` is no longer defined in `setup.py` itself — anything that referenced `setup.DEFAULT_KEY_NAME` should now use `identity.DEFAULT_KEY_NAME`. Grep the repo for `setup.DEFAULT_KEY_NAME` / `setup_mod.DEFAULT_KEY_NAME` to confirm nothing else uses it (nothing does, as of this plan).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_setup.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS (no other module imports `setup.DEFAULT_KEY_NAME` or calls `ensure_deploy_keypair()` with zero args)

- [ ] **Step 6: Commit**

```bash
git add src/wharf/setup.py tests/test_setup.py
git commit -m "Make setup.py identity-aware: ensure_deploy_keypair and setup() take an identity"
```

---

### Task 4: `ssh.py` — `SessionAuth` resolves a named identity locally

**Files:**
- Modify: `src/wharf/ssh.py`
- Test: `tests/test_ssh.py`

**Interfaces:**
- Consumes: `identity.resolve_identity`, `identity.key_paths`, `identity.DEFAULT_IDENTITY`.
- Produces: `SessionAuth.resolve(*, force_ci: bool | None = None, identity: str | None = None) -> SessionAuth` (signature change — new keyword-only `identity` param, default `None` preserves every existing call site's behavior unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ssh.py`:

```python
import pytest

from wharf.ssh import SessionAuth


def test_resolve_local_default_identity_uses_ambient_agent(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    auth = SessionAuth.resolve(force_ci=False)
    assert auth.batch is False
    assert auth.identity_file is None


def test_resolve_local_named_identity_uses_its_own_key_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_file = tmp_path / ".wharf" / "keys" / "release-bot_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("fake private key material")

    auth = SessionAuth.resolve(force_ci=False, identity="release-bot")

    assert auth.batch is False
    assert auth.identity_file == key_file


def test_resolve_local_named_identity_without_key_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="release-bot"):
        SessionAuth.resolve(force_ci=False, identity="release-bot")


def test_resolve_ci_ignores_identity_name_reads_same_env_var(monkeypatch):
    monkeypatch.setenv("DEPLOY_SSH_KEY", "fake-private-key-content\n")

    auth_default = SessionAuth.resolve(force_ci=True)
    auth_named = SessionAuth.resolve(force_ci=True, identity="release-bot")

    assert auth_default.batch is True and auth_named.batch is True
    assert auth_default.identity_file is not None
    assert auth_named.identity_file is not None
    assert auth_default.identity_file.read_text() == auth_named.identity_file.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ssh.py -v`
Expected: FAIL — `SessionAuth.resolve()` doesn't accept an `identity` keyword yet (`TypeError`).

- [ ] **Step 3: Write the implementation**

In `src/wharf/ssh.py`, add the import and replace `SessionAuth.resolve`:

```python
from .identity import DEFAULT_IDENTITY, key_paths, resolve_identity
```

```python
    @classmethod
    def resolve(cls, *, force_ci: bool | None = None, identity: str | None = None) -> "SessionAuth":
        """Decide the auth mode for this run.

        ``force_ci`` overrides autodetection -- this is what the CLI's
        ``--ci``/``--interactive`` flags set. ``identity`` selects which
        named identity's key to use for a *local* run; it has no effect
        on CI's own auth, which always reads ``DEPLOY_SSH_KEY``
        regardless of which identity that secret happens to belong to
        (a CI job only ever has one secret populated per run).
        """
        ci = is_ci() if force_ci is None else force_ci
        resolved_identity = resolve_identity(identity, is_ci=ci)

        if not ci:
            if resolved_identity == DEFAULT_IDENTITY:
                return cls(batch=False, identity_file=None)
            private_key, _ = key_paths(resolved_identity)
            if not private_key.exists():
                raise RuntimeError(
                    f"identity '{resolved_identity}' has no local key at {private_key} "
                    f"(run `wharf setup --identity {resolved_identity}` first)"
                )
            return cls(batch=False, identity_file=private_key)

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
```

(This replaces the body of the existing `resolve` classmethod; the rest of `ssh.py` — `_known_hosts_line`, `_pin_known_hosts`, `_ssh_option_argv`, etc. — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ssh.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/wharf/ssh.py tests/test_ssh.py
git commit -m "SessionAuth.resolve: support a named identity's key file for local runs"
```

---

### Task 5: `rotate.py` — the `rotate()` command

**Files:**
- Create: `src/wharf/rotate.py`
- Create: `tests/test_rotate.py`

**Interfaces:**
- Consumes: `identity.{resolve_identity, key_paths, key_comment, staged_key_paths, generate_keypair, DEFAULT_KEY_DIR}`; `config.{Config, Target}`; `ssh.{SessionAuth, build_ssh_argv, run_streaming, is_ci, RemoteCommandError}`.
- Produces: `rotate(config, *, repo, only=(), identity: str | None = None, force_ci: bool | None = None) -> None`; `_render_rotate_script(new_public_key: str, marker_pattern: str, target_name: str) -> str` (module-private, directly importable by tests, mirrors `remote_script.py`'s pure-rendering functions); `_rotate_target(target: Target, new_public_key: str, marker_pattern: str) -> None`; `_ensure_staged_keypair(identity: str, key_dir: Path = DEFAULT_KEY_DIR) -> tuple[Path, Path]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rotate.py`:

```python
import subprocess

import pytest

from wharf import rotate as rotate_module
from wharf.config import load_config
from wharf.identity import generate_keypair, key_comment, key_paths, staged_key_paths
from wharf.rotate import _render_rotate_script
from wharf.ssh import RemoteCommandError

CONFIG_TEXT = """\
version: 1
remote_repo: /srv/git/{repo}.git
targets:
  - name: app
    remote_dir: /opt/deploys/{repo}/app
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
"""


# --- _render_rotate_script: pure string rendering, checked for shape ---

def test_render_rotate_script_orders_filter_before_append_before_move():
    script = _render_rotate_script(
        new_public_key="ssh-ed25519 AAAANEW wharf:ci",
        marker_pattern=" wharf:ci$",
        target_name="app",
    )
    filter_pos = script.index("grep -v --")
    append_pos = script.index("wharf:ci$", filter_pos)  # the marker inside the grep line
    move_pos = script.index("mv \"$tmp_file\"")
    assert filter_pos < append_pos < move_pos
    assert "'ssh-ed25519 AAAANEW wharf:ci'" in script


# --- _render_rotate_script executed for real, no SSH: this is where the
# actual grep/marker logic gets verified, since a substring match on the
# wrong identity's marker (e.g. "wharf:ci" matching inside "wharf:ci-staging")
# would silently strand or duplicate access. ---

def test_rotate_script_replaces_old_key_without_touching_other_identities(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    old_key = "ssh-ed25519 AAAAOLD wharf:ci"
    sibling_identity_key = "ssh-ed25519 AAAASIB wharf:ci-staging"
    unrelated_human_key = "ssh-ed25519 AAAAHUMAN someone@laptop"
    (ssh_dir / "authorized_keys").write_text(
        "\n".join([old_key, sibling_identity_key, unrelated_human_key]) + "\n"
    )
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    subprocess.run(["bash", "-s"], input=script, text=True, check=True)

    lines = (ssh_dir / "authorized_keys").read_text().splitlines()
    assert old_key not in lines
    assert new_key in lines
    assert sibling_identity_key in lines
    assert unrelated_human_key in lines


def test_rotate_script_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    subprocess.run(["bash", "-s"], input=script, text=True, check=True)
    subprocess.run(["bash", "-s"], input=script, text=True, check=True)

    lines = (tmp_path / ".ssh" / "authorized_keys").read_text().splitlines()
    assert lines.count(new_key) == 1


# --- rotate(): staging/promotion orchestration, with _rotate_target
# faked out so no real SSH happens ---

def test_rotate_promotes_staged_key_after_all_targets_succeed(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    old_content = live_private.read_text()
    calls = []
    monkeypatch.setattr(
        rotate_module, "_rotate_target",
        lambda target, new_public_key, marker_pattern: calls.append(target.name),
    )

    rotate_module.rotate(config, repo="app", identity="ci")

    assert calls == ["app"]
    assert live_private.read_text() != old_content  # promoted to the new key
    staged_private, _ = staged_key_paths(live_private)
    assert not staged_private.exists()


def test_rotate_leaves_live_key_untouched_when_a_target_fails(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    old_content = live_private.read_text()

    def boom(target, new_public_key, marker_pattern):
        raise RemoteCommandError(f"rotate on {target.name}", 1)

    monkeypatch.setattr(rotate_module, "_rotate_target", boom)

    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")

    assert live_private.read_text() == old_content
    staged_private, _ = staged_key_paths(live_private)
    assert staged_private.exists()


def test_rotate_reuses_staged_key_on_retry_instead_of_regenerating(tmp_path, monkeypatch, write_config, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))

    def boom(target, new_public_key, marker_pattern):
        raise RemoteCommandError(f"rotate on {target.name}", 1)

    monkeypatch.setattr(rotate_module, "_rotate_target", boom)
    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")
    staged_private, _ = staged_key_paths(live_private)
    first_staged_content = staged_private.read_text()

    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")

    assert staged_private.read_text() == first_staged_content
    assert "reusing it" in capsys.readouterr().out


def test_rotate_defaults_to_ci_identity_in_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    monkeypatch.setattr(rotate_module, "_rotate_target", lambda *a, **k: None)

    rotate_module.rotate(config, repo="app")

    assert not (tmp_path / ".wharf" / "deploy_key").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rotate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wharf.rotate'`

- [ ] **Step 3: Write the implementation**

Create `src/wharf/rotate.py`:

```python
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

from .config import Config, Target, render_repo_template
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
    """
    new_public_key_q = shlex.quote(new_public_key)
    marker_pattern_q = shlex.quote(marker_pattern)
    target_name_q = shlex.quote(target_name)
    return f"""\
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
tmp_file=$(mktemp)
grep -v -- {marker_pattern_q} ~/.ssh/authorized_keys > "$tmp_file" || true
echo {new_public_key_q} >> "$tmp_file"
mv "$tmp_file" ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
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

    if live_private.exists():
        live_private.unlink()
    if live_public.exists():
        live_public.unlink()
    staged_private.rename(live_private)
    staged_public.rename(live_public)

    print()
    print("Rotation complete. Remaining manual step:")
    print(f"  Update your CI's DEPLOY_SSH_KEY secret with the new key, e.g.:")
    print(f"    gh secret set DEPLOY_SSH_KEY < {live_private}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rotate.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/wharf/rotate.py tests/test_rotate.py
git commit -m "Add wharf rotate: swap an identity's key without leaving the old one behind"
```

---

### Task 6: `operations.py` — thread `--identity` through `deploy`/`down`/`reload`

**Files:**
- Modify: `src/wharf/operations.py:97-187`
- Create: `tests/test_operations_identity.py`

**Interfaces:**
- Produces: `deploy(config, *, repo, revision, only=(), force_ci=None, identity: str | None = None)`; `down(config, *, repo, only=(), volumes=False, force_ci=None, identity: str | None = None)`; `reload(config, *, repo, only=(), force_ci=None, identity: str | None = None)` — each now passes `identity` through to its `SessionAuth.resolve()` call.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operations_identity.py`:

```python
import pytest

from wharf import operations
from wharf.config import load_config
from wharf.ssh import SessionAuth

CONFIG_TEXT = """\
version: 1
remote_repo: /srv/git/{repo}.git
targets:
  - name: app
    remote_dir: /opt/deploys/{repo}/app
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
"""


class _StopEarly(Exception):
    pass


def _capture_identity(monkeypatch, seen):
    def fake_resolve(*, force_ci=None, identity=None):
        seen["identity"] = identity
        raise _StopEarly

    monkeypatch.setattr(SessionAuth, "resolve", staticmethod(fake_resolve))


def test_deploy_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.deploy(config, repo="app", revision="abc123", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_down_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.down(config, repo="app", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_reload_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.reload(config, repo="app", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_deploy_defaults_identity_to_none(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.deploy(config, repo="app", revision="abc123")

    assert seen["identity"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_operations_identity.py -v`
Expected: FAIL — `TypeError: deploy() got an unexpected keyword argument 'identity'`

- [ ] **Step 3: Write the implementation**

In `src/wharf/operations.py`, update all three function signatures and their `SessionAuth.resolve` calls:

```python
def deploy(
    config: Config,
    *,
    repo: str,
    revision: str,
    only: tuple[str, ...] = (),
    force_ci: bool | None = None,
    identity: str | None = None,
) -> None:
    """Push, checkout, build, and healthcheck each selected target in order."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Deploying {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
        remote_repo, remote_dir = _remote_repo_and_dir(config, target, repo)
        try:
            push_revision(target, remote_repo, config.branch, revision, auth)
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
                pre_up=target.pre_up,
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
    identity: str | None = None,
) -> None:
    """Stop (and optionally wipe volumes for) each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Stopping {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
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
    identity: str | None = None,
) -> None:
    """Re-apply compose (no rebuild) for each selected target."""
    _check_branch(config)
    for target in config.select_targets(only):
        print(f"==> Reloading {target.name} ({target.host}:{target.port})")
        auth = SessionAuth.resolve(force_ci=force_ci, identity=identity)
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
```

Note the test's `fake_resolve` is patched in as `staticmethod`, not `classmethod` — it replaces the bound `SessionAuth.resolve` call site's behavior without needing the `cls` argument, since the test never calls it through a subclass.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_operations_identity.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/wharf/operations.py tests/test_operations_identity.py
git commit -m "Thread --identity through deploy/down/reload's SessionAuth.resolve calls"
```

---

### Task 7: `cli.py` — `rotate`, `identities`, `--identity`, and richer help text

**Files:**
- Modify: `src/wharf/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `identity.list_identities`; `rotate.rotate`; `setup.setup` (now takes `identity`/`force_ci`); `operations.{deploy, down, reload}` (now take `identity`); `ssh.RemoteCommandError`.
- Produces: `build_parser()` returns a parser with new `rotate` and `identities` subcommands and `--identity` on `setup`/`rotate`/`deploy`/`down`/`reload`; `main()` dispatches both.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
from pathlib import Path

from wharf.cli import build_parser


def test_setup_accepts_identity_and_ci_flags():
    args = build_parser().parse_args(["setup", "deploy.yml", "--identity", "ci", "--ci"])
    assert args.identity == "ci"
    assert args.ci is True


def test_setup_identity_defaults_to_none():
    args = build_parser().parse_args(["setup", "deploy.yml"])
    assert args.identity is None


def test_rotate_parses_like_setup():
    args = build_parser().parse_args(["rotate", "deploy.yml", "--identity", "release-bot", "--only", "app"])
    assert args.command == "rotate"
    assert args.config == Path("deploy.yml")
    assert args.identity == "release-bot"
    assert args.only == ["app"]


def test_identities_takes_no_config_argument():
    args = build_parser().parse_args(["identities"])
    assert args.command == "identities"


def test_deploy_down_reload_all_accept_identity():
    for command in ("deploy", "down", "reload"):
        args = build_parser().parse_args([command, "deploy.yml", "--identity", "ci"])
        assert args.identity == "ci"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `rotate`/`identities` aren't recognized subcommands yet, and `--identity`/`--ci` aren't accepted by `setup`.

- [ ] **Step 3: Write the implementation**

In `src/wharf/cli.py`:

1. Update the module docstring's usage summary:

```python
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
```

2. Add the imports:

```python
from . import operations, rotate as rotate_mod, setup as setup_mod
from .config import Config, ConfigError, load_config
from .identity import list_identities
from .operations import BranchMismatchError, OperationError
from .ssh import RemoteCommandError, is_ci
from .update_check import check_for_update
```

3. Add a shared `--identity` flag helper next to `_add_ci_flags`:

```python
def _add_identity_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--identity", default=None, metavar="NAME",
        help="named identity for the deploy key (default: 'ci' when running in CI, 'default' otherwise)",
    )
```

4. In `build_parser`, update `p_deploy`, `p_down`, `p_reload`, `p_setup`, and add `p_rotate`/`p_identities`:

```python
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
```

5. Add dispatch for `identities` **immediately after the existing `if args.command == "ls": ... return 0` block, and before** the `if not is_ci() and not os.environ.get("WHARF_NO_UPDATE_CHECK"): ...` update-check section that follows it. `identities` is local-only by design (see `identity.md`), so it should skip the update-check network call exactly the way `ls` already does by returning early before that section runs:

```python
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
```

6. Add the `_fingerprint` helper near `_load`:

```python
def _fingerprint(public_key: Path) -> str:
    result = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key)], capture_output=True, text=True, check=True,
    )
    # ssh-keygen -lf prints "<bits> <fingerprint> <comment> (<type>)"
    return result.stdout.split()[1]
```

Add `import subprocess` to `cli.py`'s existing import block (`argparse`, `os`, `sys`, `Path`, `yaml`).

7. **Leave the update-check section itself untouched.** In place, replace the existing `if args.command == "setup": ...` block (which runs *after* the update-check section, same as today — unlike `identities`, `setup` is not being changed to skip it) with a version that passes `identity`/`force_ci`, and add the new `rotate` block immediately after it, still in that same post-update-check position, still before the generic `config = _load(args.config)` / `repo = _resolve_repo(args)` block used by `deploy`/`down`/`reload`:

```python
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
```

(This replaces the existing `if args.command == "setup":` block in place, and adds the new `rotate` block right after it — both still come before the generic `deploy`/`down`/`reload` handling further down in `main()`.)

8. Thread `identity` into the existing `deploy`/`down`/`reload` calls:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `pytest -v`
Expected: PASS

- [ ] **Step 6: Manual smoke test**

```bash
python -m wharf --help
python -m wharf rotate --help
python -m wharf identities --help
python -m wharf identities
```

Expected: `--help` output for `rotate`/`identities` shows the new help text; `identities` (run from a directory with no `.wharf/`) prints "No local deploy-key identities found...".

- [ ] **Step 7: Commit**

```bash
git add src/wharf/cli.py tests/test_cli.py
git commit -m "Wire up wharf rotate/identities, --identity flag, and richer help text"
```

---

### Task 8: Documentation — per-file code reference and user-facing docs

**Files:**
- Create: `src/wharf/identity.md`
- Create: `src/wharf/rotate.md`
- Modify: `src/wharf/README.md`
- Modify: `src/wharf/setup.md`
- Modify: `src/wharf/ssh.md`
- Modify: `src/wharf/cli.md`
- Modify: `docs/configuration.md`
- Modify: `README.md`

**Interfaces:** None — documentation only, no code.

- [ ] **Step 1: Create `src/wharf/identity.md`**

```markdown
# `identity.py`

Named deploy-key identities: resolution, on-disk layout, and the
`authorized_keys` comment marker used as rotation bookkeeping. Shared by
[`setup.py`](setup.md), [`rotate.py`](rotate.md), `cli.py`'s
`identities` command, and [`ssh.py`](ssh.md)'s `SessionAuth` — resolving
an identity name and finding its key files happens in exactly one place
so those consumers can't drift apart on what a marker or a file path
looks like.

## `resolve_identity(explicit, *, is_ci)`

`--identity` wins if given; otherwise `"ci"` in CI, `"default"`
locally. `"default"` exists solely so anyone who never passes
`--identity` gets exactly wharf's original single-key behavior,
unchanged.

Identity names are restricted to `^[a-z0-9][a-z0-9-]*$`
(`validate_identity_name`) — a charset with no regex metacharacters, so
it's always safe to embed directly in a filename or in the `grep`
pattern [`rotate.py`](rotate.md) builds, with nothing to escape.

## `key_paths(identity, key_dir=.wharf)` / `key_comment(identity)`

- `identity == "default"` → `.wharf/deploy_key` / `.wharf/deploy_key.pub`,
  comment `wharf-deploy` — the exact path and comment wharf has always
  used, so existing installs need no migration.
- any other identity → `.wharf/keys/<identity>_key` /
  `.wharf/keys/<identity>_key.pub`, comment `wharf:<identity>`.

The comment is what `ssh-keygen -C` writes into the public key file, so
it travels verbatim into the target's `authorized_keys` line once
provisioned. That string *is* the bookkeeping `rotate` reads back from
the target to know which lines belong to which identity — there's no
separate state file.

## `generate_keypair(private_key, comment)`

Shells out to `ssh-keygen -t ed25519` rather than adding a crypto
library dependency, and creates the key's parent directory as needed.
Used by both `setup.ensure_deploy_keypair` (the live key) and
`rotate._ensure_staged_keypair` (the staged replacement).

## `staged_key_paths(private_key)`

Where `rotate` stages a replacement keypair (`<name>.new` /
`<name>.new.pub`) before promoting it to the live path — see
[`rotate.md`](rotate.md).

## `list_identities(key_dir=.wharf)`

Backs `wharf identities`. Scans `.wharf/deploy_key` plus every
`.wharf/keys/*_key` file and returns an `IdentityInfo` per identity
found (name, key paths, comment, and whether a staged `.new` file is
sitting there from an interrupted `rotate`). **Local-only, deliberately**
— reads only the current checkout's disk, never SSHes anywhere, so it
cannot detect a manually-edited `authorized_keys` or a partial rotation
that hasn't been retried yet.
```

- [ ] **Step 2: Create `src/wharf/rotate.md`**

```markdown
# `rotate.py`

Implements `wharf rotate`: replaces an identity's deploy key across
every selected target, removing the old `authorized_keys` entry instead
of `setup`'s purely-additive behavior. See [`identity.md`](identity.md)
for how an identity resolves to key paths and its `authorized_keys`
marker.

## Staging

`_ensure_staged_keypair` generates the replacement to a **staged** path
(`identity.staged_key_paths`) rather than overwriting the live key file
immediately. If a staged pair already exists — from a previous `rotate`
run that got interrupted partway through the targets — it's reused
instead of generating another, the same "if it exists, leave it"
idempotency `setup.ensure_deploy_keypair` already uses.

## Per-target script

`_render_rotate_script` builds one script per target: filter every
`authorized_keys` line matching the identity's marker pattern
(` wharf:<identity>$`, anchored so e.g. `wharf:ci` can never match a
sibling `wharf:ci-staging` line) out to a temp file, unconditionally
append the new key, then atomically `mv` the temp file over
`authorized_keys`. Filtering and re-appending in one pass — rather than
adding the new key and separately removing the old one — means there's
never an intermediate write where the live file has neither key, and
running the same script twice converges to the same state (the new
key's own line also carries the marker, so it gets filtered out and
unconditionally re-added on every run).

`_rotate_target` runs that script over `SessionAuth(batch=False)` — the
operator's own identity, same as `setup._provision_target` — never the
key being rotated, for the same anti-chicken-and-egg reason `setup`
already relies on.

## Promotion

`rotate()` only promotes the staged keypair to the live key path
(deleting the old private key, renaming the staged files into place)
**after every selected target succeeds**. Targets are processed
sequentially in config order, stopping at the first failure — if one
fails, the live key files are left untouched and the staged files
persist, so re-running `rotate` is the recovery path, not a separate
resume command.

Prints the same "update your CI secret" instruction
[`setup.py`](setup.md) does, pointing at the now-live new private key.
```

- [ ] **Step 3: Update `src/wharf/README.md`**

Replace the file table and call graph:

```markdown
| File | What it does |
|---|---|
| [`cli.md`](cli.md) | `argparse` CLI, subcommand dispatch, error → exit code mapping. |
| [`config.md`](config.md) | Parses and validates a config file into `Config`/`Target`/`SecretsDefaults`/`PreUpStep`. |
| [`operations.md`](operations.md) | Orchestrates `deploy`/`down`/`reload` across a config's targets, sequentially. |
| [`remote_script.md`](remote_script.md) | Renders the bash scripts that actually run on each target. |
| [`ssh.md`](ssh.md) | SSH argv construction, host-key pinning, CI vs. local auth, subprocess execution. |
| [`identity.md`](identity.md) | Named deploy-key identity resolution, on-disk key paths, and the `authorized_keys` marker used for rotation. |
| [`git_ops.md`](git_ops.md) | `git push`es the deploy revision to a target's bare repo — wharf's registry substitute. |
| [`healthcheck.md`](healthcheck.md) | Polls a target's `healthcheck` URL after `deploy`/`reload`. |
| [`setup.md`](setup.md) | `wharf setup`: generates a deploy keypair, bootstraps bare repos and `authorized_keys`. |
| [`rotate.md`](rotate.md) | `wharf rotate`: replaces an identity's deploy key without leaving the old one authorized. |
| [`update_check.md`](update_check.md) | Best-effort "a newer wharf release exists" notice. |
| [`__init__.md`](__init__.md) | Package version. |
| [`__main__.md`](__main__.md) | `python -m wharf` entry point. |

## Call graph

```
cli.py
 ├─ config.py            (load_config)
 ├─ identity.py           resolve_identity, list_identities
 ├─ operations.py         deploy / down / reload
 │   ├─ config.py         (select_targets, compose_file_for, render_repo_template)
 │   ├─ git_ops.py         push_revision  ──┐
 │   ├─ remote_script.py   render_up/down/reload
 │   ├─ ssh.py              SessionAuth, run_remote_script  ◄┘ (both go over SSH)
 │   │   └─ identity.py     key_paths, resolve_identity (named-identity local auth)
 │   └─ healthcheck.py      wait_healthy
 ├─ setup.py               setup
 │   ├─ identity.py         key_paths, key_comment, generate_keypair, resolve_identity
 │   └─ ssh.py              SessionAuth, build_ssh_argv, run_streaming
 ├─ rotate.py               rotate
 │   ├─ identity.py         key_paths, key_comment, staged_key_paths, generate_keypair, resolve_identity
 │   └─ ssh.py              SessionAuth, build_ssh_argv, run_streaming
 └─ update_check.py        check_for_update
```

Nothing in this package talks to Docker, Infisical, or a target host
directly except through `ssh.py`'s `run_streaming`/`run_remote_script` —
every remote effect is a bash script rendered by `remote_script.py` (or,
for `setup`/`rotate`, an inline script in `setup.py`/`rotate.py`) piped
over one SSH connection.
```

- [ ] **Step 4: Update `src/wharf/setup.md`**

Replace the `## \`ensure_deploy_keypair(key_dir=.wharf)\`` and `## \`setup(config, *, repo, only=())\`` headings and bodies:

```markdown
## `ensure_deploy_keypair(identity, key_dir=.wharf)`

Thin wrapper around [`identity.generate_keypair`](identity.md):
idempotent (if the identity's key already exists, it's left as-is and
reused), and prints which path it used. `identity == "default"` keeps
writing to `.wharf/deploy_key` exactly as `wharf setup` has always done.

## `setup(config, *, repo, only=(), identity=None, force_ci=None)`

Resolves the identity (`identity.resolve_identity` — explicit
`--identity`, else `"ci"` in CI, else `"default"`), ensures that
identity's keypair exists, then for each selected target,
`_provision_target`:
```

(the numbered list below that heading, describing `_provision_target`'s steps, is unchanged — it never needed identity awareness, since it only ever receives an already-formed public key string.)

- [ ] **Step 5: Update `src/wharf/ssh.md`**

Replace the `SessionAuth.resolve(force_ci=None)` paragraph:

```markdown
`SessionAuth.resolve(force_ci=None, identity=None)` picks the mode:
`force_ci` (the CLI's `--ci`/`--interactive` flags) overrides
autodetection via [`is_ci()`](#is_ci). `identity` (see
[`identity.md`](identity.md)) only matters for **local** runs: the
`"default"` identity (or no identity) keeps using the ambient SSH
agent, exactly as before; any other identity resolves to that
identity's own key file (`.wharf/keys/<identity>_key`), so a bot or
automation script invoked outside CI can say `--identity release-bot`
and get a dedicated key instead of the ambient agent. In CI, `identity`
has no effect — CI always reads the same `DEPLOY_SSH_KEY` secret
regardless of which identity that secret happens to belong to.
```

- [ ] **Step 6: Update `src/wharf/cli.md`**

Read the current file, then add a short paragraph (matching its existing style) noting the two new subcommands and the `--identity` flag:

```markdown
`rotate` and `identities` follow the same subparser pattern as the
other commands — `rotate` takes the same `<config.yml> [--only]
[--identity]` shape as `setup`; `identities` takes no config file
argument at all, since identity key files live under `.wharf/` at the
project root, not per config file. `--identity` is added to
`deploy`/`down`/`reload`/`setup`/`rotate` via the shared
`_add_identity_flag` helper, the same pattern `_add_ci_flags` already
uses.
```

- [ ] **Step 7: Update `docs/configuration.md`**

Replace the caveat paragraph added in the earlier CI-onboarding walkthrough (currently reads "Repeat steps 1-2 per config file if you deploy more than one environment... each currently shares the same `.wharf/deploy_key` on disk, so re-running `wharf setup` for a second config file against the same checkout won't generate a second key.") with:

```markdown
Repeat steps 1-2 per config file if you deploy more than one environment
(`deploy.yml`, `deploy.staging.yml`, ...) from CI. By default they'd
share one `"ci"` identity (and so one key) since identity key files live
under `.wharf/` at the project root, not per config file — name each
environment's identity explicitly if you want them independently
rotatable, e.g. `wharf setup deploy.yml --identity ci-prod` and `wharf
setup deploy.staging.yml --identity ci-staging`, with each `DEPLOY_SSH_KEY`
secret set in that environment's own CI configuration.

## Identities and rotation

Every deploy key belongs to a named **identity**. `--identity NAME` on
`setup`, `rotate`, `deploy`, `down`, or `reload` picks which one;
without it, wharf uses `"ci"` when running in CI and `"default"`
otherwise — `"default"` is exactly the single key `wharf setup` has
always generated, so nothing changes if you never pass `--identity`.

```
wharf setup deploy.yml --identity ci-staging   # generate/provision a named identity
wharf rotate deploy.yml --identity ci-staging  # replace that identity's key everywhere,
                                                # removing the old authorized_keys entry
wharf identities                               # list identities with a local key file
```

`wharf rotate` always produces a new key and removes the old one from
every target's `authorized_keys` — unlike `setup`, which only ever
appends. It's safe to re-run if interrupted partway: already-rotated
targets keep the new key, not-yet-rotated targets keep the old one
until you run it again.

`wharf identities` reads only local key files under `.wharf/` — it does
not check what's actually authorized on any target.

### Moving from a single shared key to a named identity

If you're already running `wharf setup` with no `--identity` (the
`"default"` identity) and want to name it going forward — e.g. so it's
independently rotatable from some other automation later — that's a
manual, one-time move, not something `rotate` itself does (`rotate`
only replaces a key *within* the same identity, it doesn't rename one
identity into another):

1. `wharf setup deploy.yml --identity ci` — additive, installs the new
   key alongside the old one.
2. Update your CI secret to the new key's contents.
3. Confirm a real CI deploy succeeds with the new key.
4. Manually remove the old `wharf-deploy`-commented line from each
   target's `authorized_keys`, and delete `.wharf/deploy_key` locally.
```

- [ ] **Step 8: Update `README.md`**

In the Commands table, add two rows after `wharf setup`:

```markdown
| `wharf rotate <config.yml>` | Replace an identity's deploy key everywhere, removing the old `authorized_keys` entry. |
| `wharf identities` | List locally known deploy-key identities and their key files (local only). |
```

And update the line describing common flags:

```markdown
All commands except `ls` and `identities` accept `--only NAME`
(repeatable) to act on a subset of targets, `--repo NAME` to override
the inferred project name, and `--identity NAME` to pick which named
deploy-key identity to use (default: `ci` in CI, `default` otherwise).
`deploy` also accepts `--revision SHA`. See
[docs/configuration.md](docs/configuration.md) for the full config
reference, worked examples, identities and rotation, and how local vs.
CI auth is handled.
```

- [ ] **Step 9: Review the rendered docs for consistency**

Read through `README.md`, `docs/configuration.md`, and `src/wharf/README.md` top to bottom once — confirm every cross-reference link (e.g. `identity.md`, `rotate.md`) resolves to a real file, and no stale text still describes the pre-identity single-key behavior as if it were the only option.

- [ ] **Step 10: Commit**

```bash
git add src/wharf/identity.md src/wharf/rotate.md src/wharf/README.md \
        src/wharf/setup.md src/wharf/ssh.md src/wharf/cli.md \
        docs/configuration.md README.md
git commit -m "Document identity.py, rotate.py, wharf rotate/identities, and migration"
```

---

## Final check

- [ ] Run `pytest -v` once more from the repo root — every test across all eight tasks passes together, not just per-task.
- [ ] Run `python -m wharf --help`, `python -m wharf setup --help`, `python -m wharf rotate --help`, `python -m wharf identities --help` — confirm the help text reads correctly end to end.
