# pre_up Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a wharf target declare `pre_up: [service, ...]` — compose service names run via `docker compose run --rm --build <service>` before the target's `up`, for one-off migration/bootstrap commands (e.g. janus-infra's `core` unit).

**Architecture:** `config.py` gains a validated `pre_up` field on `Target`. `remote_script.py`'s secrets-wrapping is split into a one-time `infisical login` (`_secrets_login`) and a reusable per-command `infisical run` prefix (`_secrets_run_prefix`), combined by a new `_wrapped_commands_block` helper that both `render_up` (pre_up commands + `up`) and `render_reload` (`up`, no pre_up) call. `operations.py` passes `target.pre_up` through to `render_up`.

**Tech Stack:** Python 3.10+, pytest, PyYAML, bash (rendered remote scripts).

## Global Constraints

- `pre_up` entries must be valid compose service identifiers: non-empty, matching `^[A-Za-z0-9][A-Za-z0-9._-]*$` (no leading `-`, so a malformed entry can't be parsed as a `docker compose run` flag).
- Every `pre_up` entry is `shlex.quote`d at render time, in addition to the validator above (defense in depth, not a substitute for it).
- Every `pre_up` command uses `docker compose run --rm --build <service>` — `--build` is mandatory so migrations run against the freshly-built image, not a stale cached one.
- `pre_up` commands run in list order, after checkout, before the final `up -d --build --remove-orphans`.
- `infisical login` must be emitted at most once per rendered script, regardless of how many `pre_up` entries exist (reuse `$infisical_token`).
- `render_down` and `render_reload` are unaffected by `pre_up` — only `render_up` (the deploy action) runs pre-up hooks, matching the old per-repo scripts.
- Reference spec: `docs/superpowers/specs/2026-08-04-pre-up-hook-design.md`.

---

### Task 1: `pre_up` config schema

**Files:**
- Modify: `src/wharf/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Target.pre_up: tuple[str, ...] | None` (new field, default `None`). Consumed by Task 3/4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_pre_up_defaults_to_none(write_config):
    config = load_config(write_config(VALID_MINIMAL))
    assert config.targets[0].pre_up is None


def test_pre_up_accepts_valid_service_names(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n",
        '    order: 10\n    pre_up: ["migrate-janus", "bootstrap-dashboard-admin"]\n',
    )
    config = load_config(write_config(text))
    assert config.targets[0].pre_up == ("migrate-janus", "bootstrap-dashboard-admin")


def test_pre_up_rejects_leading_dash(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: ["--rm"]\n'
    )
    with pytest.raises(ConfigError, match="compose service name"):
        load_config(write_config(text))


def test_pre_up_rejects_empty_list(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", "    order: 10\n    pre_up: []\n"
    )
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(write_config(text))


def test_pre_up_rejects_shell_metacharacters(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: ["svc; rm -rf /"]\n'
    )
    with pytest.raises(ConfigError, match="compose service name"):
        load_config(write_config(text))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest tests/test_config.py -v -k pre_up`
Expected: FAIL — `pre_up` isn't a recognized field yet (`_exact_keys` will reject it as "unexpected"), and `Target` has no `pre_up` attribute.

- [ ] **Step 3: Add the `pre_up` field, validator, and parsing**

In `src/wharf/config.py`, add `import re` to the top-level imports (alongside the existing `from dataclasses import dataclass` etc.):

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
```

Add the compose-service-name pattern near the other module-level constants (after `DEFAULT_COMPOSE_FILE = "docker-compose.yml"`):

```python
_COMPOSE_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
```

Add `pre_up` to the `Target` dataclass (after `paths: tuple[str, ...] | None = None`):

```python
@dataclass(frozen=True)
class Target:
    """A single deploy destination within a config file."""

    name: str
    remote_dir: str
    host: str
    port: int
    user: str
    host_key: str
    order: int
    healthcheck: str | None = None
    compose_file: str | None = None
    paths: tuple[str, ...] | None = None
    pre_up: tuple[str, ...] | None = None

    @property
    def uses_secrets(self) -> bool:
        """A target opts into secrets injection purely by declaring paths."""
        return bool(self.paths)
```

Add the validator functions near `_string_list` (after it):

```python
def _compose_service_name(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    if not _COMPOSE_SERVICE_NAME_RE.match(text):
        raise ConfigError(
            f"{label} must be a valid compose service name "
            "(letters, digits, '.', '_', '-', not starting with '-')"
        )
    return text


def _compose_service_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty list of strings")
    return tuple(_compose_service_name(item, f"{label}[{i}]") for i, item in enumerate(value))
```

In `_load_target`, add `"pre_up"` to the optional keys and parse it:

```python
def _load_target(value: object, index: int, *, secrets_configured: bool) -> Target:
    label = f"targets[{index}]"
    required = {"name", "remote_dir", "host", "port", "user", "host_key", "order"}
    optional = frozenset({"healthcheck", "compose_file", "paths", "pre_up"})
    _exact_keys(value, required, optional, label=label)
    assert isinstance(value, dict)

    paths = _string_list(value["paths"], f"{label}.paths") if "paths" in value else None
    if paths and not secrets_configured:
        raise ConfigError(
            f"{label} declares paths but no top-level secrets block is configured"
        )

    return Target(
        name=_nonempty_string(value["name"], f"{label}.name"),
        remote_dir=_nonempty_string(value["remote_dir"], f"{label}.remote_dir"),
        host=_nonempty_string(value["host"], f"{label}.host"),
        port=_port(value["port"], f"{label}.port"),
        user=_nonempty_string(value["user"], f"{label}.user"),
        host_key=_host_key(value["host_key"], f"{label}.host_key"),
        order=_integer(value["order"], f"{label}.order"),
        healthcheck=_url(value["healthcheck"], f"{label}.healthcheck") if "healthcheck" in value else None,
        compose_file=_nonempty_string(value["compose_file"], f"{label}.compose_file") if "compose_file" in value else None,
        paths=paths,
        pre_up=_compose_service_list(value["pre_up"], f"{label}.pre_up") if "pre_up" in value else None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest tests/test_config.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones — confirms no regression).

- [ ] **Step 5: Commit**

```bash
cd /home/aless/UrbanMIS/repos/wharf
git add src/wharf/config.py tests/test_config.py
git commit -m "Add pre_up field to Target config schema"
```

---

### Task 2: Characterization tests for remote_script.py (pre-refactor baseline)

This task adds regression tests for `render_up`/`render_down`/`render_reload`'s **current** behavior before Task 3 refactors their internals. `remote_script.py` has no test coverage today — these tests must pass unmodified against both the current code and the refactored code from Task 3, proving the refactor didn't change observable behavior (aside from the new `pre_up` feature).

**Files:**
- Create: `tests/test_remote_script.py`

**Interfaces:**
- Consumes: `wharf.config.SecretsDefaults`, `wharf.remote_script.render_up/render_down/render_reload` (all pre-existing).

- [ ] **Step 1: Write the tests**

Create `tests/test_remote_script.py`:

```python
from wharf.config import SecretsDefaults
from wharf.remote_script import render_down, render_reload, render_up

SECRETS = SecretsDefaults(
    provider="infisical",
    project_id="proj-123",
    domain="https://eu.infisical.com",
    environment="prod",
)


def test_render_up_without_secrets_has_no_infisical():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" up -d --build --remove-orphans' in script


def test_render_up_with_secrets_wraps_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/app/",),
    )
    assert script.count("infisical login") == 1
    assert "infisical run --env=prod --path=/app/" in script
    assert '--token "$infisical_token"' in script
    assert 'docker compose -f "$compose_file" up -d --build --remove-orphans' in script
    assert "INFISICAL_MACHINE_IDENTITY_ID" in script


def test_render_up_checkout_happens_before_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    checkout_index = script.index('checkout -f "$REVISION"')
    up_index = script.index("up -d --build --remove-orphans")
    assert checkout_index < up_index


def test_render_reload_without_secrets_has_no_infisical():
    script = render_reload(
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" up -d --remove-orphans' in script
    assert "--build" not in script


def test_render_reload_with_secrets_wraps_up_command():
    script = render_reload(
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/app/",),
    )
    assert script.count("infisical login") == 1
    assert "infisical run --env=prod --path=/app/" in script
    assert 'docker compose -f "$compose_file" up -d --remove-orphans' in script


def test_render_down_has_no_secrets_wrapping():
    script = render_down(remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", volumes=False)
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" down' in script


def test_render_down_with_volumes_adds_flag():
    script = render_down(remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", volumes=True)
    assert 'docker compose -f "$compose_file" down --volumes' in script
```

- [ ] **Step 2: Run tests to verify they pass against current code**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest tests/test_remote_script.py -v`
Expected: PASS — these characterize existing behavior, they are not meant to fail. If any fails, the assertion doesn't match current `remote_script.py` output; fix the assertion (not the source) to match reality before proceeding.

- [ ] **Step 3: Commit**

```bash
cd /home/aless/UrbanMIS/repos/wharf
git add tests/test_remote_script.py
git commit -m "Add characterization tests for remote_script.py before pre_up refactor"
```

---

### Task 3: Refactor secrets wrapping and add pre_up execution to render_up

**Files:**
- Modify: `src/wharf/remote_script.py`
- Test: `tests/test_remote_script.py` (extend)

**Interfaces:**
- Consumes: `Target.pre_up: tuple[str, ...] | None` (from Task 1).
- Produces: `render_up(..., pre_up: tuple[str, ...] | None = None)` — new optional keyword parameter. Consumed by Task 4.

- [ ] **Step 1: Write the new failing tests**

Add to `tests/test_remote_script.py`:

```python
def test_render_up_with_pre_up_runs_before_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
        pre_up=("migrate-janus", "bootstrap-dashboard-admin"),
    )
    migrate_index = script.index("run --rm --build migrate-janus")
    bootstrap_index = script.index("run --rm --build bootstrap-dashboard-admin")
    up_index = script.index("up -d --build --remove-orphans")
    assert migrate_index < bootstrap_index < up_index


def test_render_up_pre_up_commands_are_shlex_quoted():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
        pre_up=("migrate-janus",),
    )
    assert 'docker compose -f "$compose_file" run --rm --build migrate-janus' in script


def test_render_up_with_pre_up_and_secrets_calls_login_once():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/core/",),
        pre_up=("migrate-janus", "bootstrap-dashboard-admin", "migrate-janusdashboard"),
    )
    assert script.count("infisical login") == 1
    assert script.count("infisical run --env=prod --path=/core/") == 4  # 3 pre_up + 1 up
    for service in ("migrate-janus", "bootstrap-dashboard-admin", "migrate-janusdashboard"):
        assert f"run --rm --build {service}" in script


def test_render_up_without_pre_up_matches_no_pre_up_behavior():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "run --rm" not in script
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest tests/test_remote_script.py -v -k pre_up`
Expected: FAIL — `render_up` doesn't accept a `pre_up` keyword yet.

- [ ] **Step 3: Replace `src/wharf/remote_script.py`**

Replace the full file contents with:

```python
"""Built-in remote-side bash for wharf's up/down/reload actions.

These replace the per-repo ``deploy_prod.sh`` script from wharf's
predecessor: the locking, checkout, secrets-wrapping, and image-cleanup
logic now lives once, inside wharf itself, driven entirely by the config
file rather than committed to (and duplicated across) each project's repo.

Each ``render_*`` function returns a complete bash script, meant to be
piped to `ssh ... bash -s` via :func:`wharf.ssh.run_remote_script`. Config
values (paths, compose file, secrets location) are embedded as
shell-quoted literals at render time; only ``REVISION`` travels as an
environment variable, so the rendered script text stays identical across
deploys of the same target and only the environment changes -- useful
when eyeballing what actually ran in a log.
"""

from __future__ import annotations

import shlex

from .config import SecretsDefaults

_LOCK_FILE_NAME = ".wharf-deploy.lock"


def _secrets_login(secrets: SecretsDefaults) -> str:
    """The one-time `infisical login` call for a script.

    Emitted once per script regardless of how many commands reuse the
    resulting ``$infisical_token`` -- see :func:`_secrets_run_prefix`. A
    naive reuse of a single combined "login + run" string (the old
    ``_secrets_wrap``) once per wrapped command would call `infisical
    login` once per command instead.
    """
    return f"""\
: "${{INFISICAL_MACHINE_IDENTITY_ID:?INFISICAL_MACHINE_IDENTITY_ID is required on this host}}"
: "${{INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET:?INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET is required on this host}}"
infisical_token=$(infisical login --method=universal-auth --domain={shlex.quote(secrets.domain)} --client-id="$INFISICAL_MACHINE_IDENTITY_ID" --client-secret="$INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET" --plain)
"""


def _secrets_run_prefix(secrets: SecretsDefaults, paths: tuple[str, ...]) -> str:
    """The `infisical run ... --` prefix for one command.

    Reuses the ``$infisical_token`` set by :func:`_secrets_login`, so this
    can prefix any number of commands in the same script.
    """
    path_flags = " ".join(f"--path={shlex.quote(p)}" for p in paths)
    return (
        f"infisical run --env={shlex.quote(secrets.environment)} {path_flags} "
        f"--projectId={shlex.quote(secrets.project_id)} --domain={shlex.quote(secrets.domain)} "
        f'--token "$infisical_token" -- '
    )


def _wrapped_commands_block(
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
    *,
    extra_commands: list[str],
    final_command: str,
) -> str:
    """Build the indented command sequence for the body of a `(...)` block.

    ``extra_commands`` run in order before ``final_command`` (used by
    :func:`render_up` for ``pre_up``). Every command is wrapped with the
    same Infisical injection when the target has secrets configured
    (``secrets`` and ``paths`` both set); `infisical login` is emitted at
    most once regardless of how many commands there are.
    """
    has_secrets = bool(secrets and paths)
    login_lines = _secrets_login(secrets).rstrip("\n").splitlines() if has_secrets else []
    run_prefix = _secrets_run_prefix(secrets, paths) if has_secrets else ""
    command_lines = [f"{run_prefix}{cmd}" for cmd in (*extra_commands, final_command)]
    return "\n".join(f"  {line}" for line in login_lines + command_lines)


def render_up(
    *,
    remote_repo: str,
    remote_dir: str,
    compose_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
    pre_up: tuple[str, ...] | None = None,
) -> str:
    """Deploy action: checkout $REVISION, run pre_up hooks, build, start, prune old images.

    Each ``pre_up`` entry runs as ``docker compose run --rm --build
    <service>`` before the final ``up`` -- for one-off migration/bootstrap
    commands. ``--build`` is mandatory: `docker compose run` reuses a
    cached image by default, so without it a migration could run against
    the previous release's image while `up --build` builds and starts the
    new one.
    """
    pre_up_commands = [
        f'docker compose -f "$compose_file" run --rm --build {shlex.quote(service)}'
        for service in (pre_up or ())
    ]
    commands_block = _wrapped_commands_block(
        secrets,
        paths,
        extra_commands=pre_up_commands,
        final_command='docker compose -f "$compose_file" up -d --build --remove-orphans',
    )
    return f"""\
set -euo pipefail
: "${{REVISION:?REVISION is required}}"
remote_repo={shlex.quote(remote_repo)}
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
lock_file="$remote_dir/{_LOCK_FILE_NAME}"
mkdir -p "$remote_dir"

(
  flock -x 200 || {{ echo "ERROR: deploy lock is held, aborting" >&2; exit 1; }}

  old_images=()
  if [ -f "$remote_dir/$compose_file" ]; then
    mapfile -t old_images < <(cd "$remote_dir" && \\
      docker compose -f "$compose_file" images -q 2>/dev/null | sort -u || true)
  fi

  git --work-tree="$remote_dir" --git-dir="$remote_repo" checkout -f "$REVISION"
  echo "Code deployed to $remote_dir (revision ${{REVISION:0:7}})"

  cd "$remote_dir"
{commands_block}
  echo "Services started"

  for img_id in "${{old_images[@]+"${{old_images[@]}}"}}"; do
    docker inspect "$img_id" >/dev/null 2>&1 || continue
    [ -n "$(docker ps -q --filter "ancestor=$img_id")" ] && continue
    docker rmi "$img_id" 2>/dev/null && echo "  Removed old image $img_id" || true
  done
) 200>>"$lock_file"
"""


def render_down(*, remote_dir: str, compose_file: str, volumes: bool) -> str:
    """Down action: stop and remove containers (and optionally volumes)."""
    flag = " --volumes" if volumes else ""
    return f"""\
set -euo pipefail
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
lock_file="$remote_dir/{_LOCK_FILE_NAME}"
mkdir -p "$remote_dir"

(
  flock -x 200 || {{ echo "ERROR: deploy lock is held, aborting" >&2; exit 1; }}
  cd "$remote_dir"
  docker compose -f "$compose_file" down{flag}
  echo "Stopped"
) 200>>"$lock_file"
"""


def render_reload(
    *,
    remote_dir: str,
    compose_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
) -> str:
    """Reload action: re-apply compose against the checked-out revision, no rebuild.

    Useful after rotating a secret, or just to restart services without a
    code change -- ``up -d`` without ``--build`` is a no-op for images
    that haven't changed. Never runs ``pre_up`` hooks -- reload doesn't
    check out a new revision, so there's nothing new to migrate.
    """
    commands_block = _wrapped_commands_block(
        secrets,
        paths,
        extra_commands=[],
        final_command='docker compose -f "$compose_file" up -d --remove-orphans',
    )
    return f"""\
set -euo pipefail
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
lock_file="$remote_dir/{_LOCK_FILE_NAME}"
mkdir -p "$remote_dir"

(
  flock -x 200 || {{ echo "ERROR: deploy lock is held, aborting" >&2; exit 1; }}
  cd "$remote_dir"
{commands_block}
  echo "Reloaded"
) 200>>"$lock_file"
"""
```

- [ ] **Step 4: Run all remote_script tests to verify everything passes**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest tests/test_remote_script.py -v`
Expected: PASS — both the Task 2 characterization tests (proving no regression) and the new Task 3 `pre_up` tests.

- [ ] **Step 5: Run the full test suite**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest -v`
Expected: PASS — nothing else in the codebase calls `remote_script.py`'s private `_secrets_wrap` (it's being removed), so no other module should break. If anything imports `_secrets_wrap` directly, update it to use `_secrets_login`/`_secrets_run_prefix`/`_wrapped_commands_block` instead.

- [ ] **Step 6: Commit**

```bash
cd /home/aless/UrbanMIS/repos/wharf
git add src/wharf/remote_script.py tests/test_remote_script.py
git commit -m "Split secrets wrapping into login+run-prefix, add pre_up execution to render_up"
```

---

### Task 4: Wire pre_up through operations.py and document the schema field

**Files:**
- Modify: `src/wharf/operations.py:97-127` (the `deploy` function)
- Modify: `docs/configuration.md`

**Interfaces:**
- Consumes: `render_up(..., pre_up: tuple[str, ...] | None = None)` (Task 3), `Target.pre_up` (Task 1).

- [ ] **Step 1: Pass `target.pre_up` into the `render_up` call**

In `src/wharf/operations.py`, inside `deploy()`, the `render_up(...)` call currently reads:

```python
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
            )
```

Change it to:

```python
            script = render_up(
                remote_repo=remote_repo,
                remote_dir=remote_dir,
                compose_file=config.compose_file_for(target),
                secrets=config.secrets,
                paths=target.paths,
                pre_up=target.pre_up,
            )
```

- [ ] **Step 2: Run the full test suite**

Run: `cd /home/aless/UrbanMIS/repos/wharf && python -m pytest -v`
Expected: PASS. This is a one-line pass-through of an already-validated (Task 1) and already-tested (Task 3) field, so no new test is added here — `test_render_up_with_pre_up_and_secrets_calls_login_once` and friends already cover `render_up`'s behavior given a `pre_up` value; this step only confirms `operations.py` actually supplies it.

- [ ] **Step 3: Document `pre_up` in the configuration reference**

In `docs/configuration.md`, in the "Full schema" block, add a `pre_up` line right after the `paths:` line (currently the last field shown for a target):

```yaml
    paths: ["/etl/", "/shared/"]        # optional. Infisical secret
                                         # paths to inject for this
                                         # target. A target uses secrets
                                         # if and only if it sets `paths`
                                         # — requires a top-level
                                         # `secrets:` block to exist.
    pre_up: [migrate, bootstrap]        # optional. Compose service names
                                         # run via `docker compose run
                                         # --rm --build <service>`, in
                                         # order, after checkout and
                                         # before `up`. Wrapped with the
                                         # same secrets injection as `up`
                                         # when this target sets `paths`.
                                         # Not run by `wharf reload`.
```

- [ ] **Step 4: Commit**

```bash
cd /home/aless/UrbanMIS/repos/wharf
git add src/wharf/operations.py docs/configuration.md
git commit -m "Wire pre_up through operations.deploy, document the schema field"
```

---

## Out of scope (explicitly, per the design spec)

Writing janus-infra's actual `.janus/deploy.yml` / `.janus/deploy.staging.yml` `core` target with its real `pre_up` service list (`migrate-janus`, `bootstrap-dashboard-admin`, `migrate-janusdashboard`, `bootstrap-dashboard-admin` again — the duplicate is intentional, see the spec's verification note) is a separate follow-up task in the `janus-infra` repo, not part of this plan.
