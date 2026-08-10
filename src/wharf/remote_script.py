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

from .config import PreUpStep, SecretsDefaults

_LOCK_FILE_NAME = ".wharf-deploy.lock"


def _secrets_login(secrets: SecretsDefaults) -> str:
    """The one-time `infisical login` call for a script.

    Emitted once per script regardless of how many commands reuse the
    resulting ``$infisical_token`` -- see :func:`_secrets_run_prefix`. A
    naive reuse of a single combined "login + run" string (the old
    ``_secrets_wrap``) once per wrapped command would call `infisical
    login` once per command instead.

    The client ID/secret are passed via env-var prefix
    (``INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=... infisical login``) rather
    than ``--client-id``/``--client-secret`` flags: flag values land in
    the process's argv, readable by any local user via `ps` for the life
    of the process, whereas an env-var prefix only sets the child's
    environment (readable only by the same user or root via
    /proc/<pid>/environ).
    """
    return f"""\
: "${{INFISICAL_MACHINE_IDENTITY_ID:?INFISICAL_MACHINE_IDENTITY_ID is required on this host}}"
: "${{INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET:?INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET is required on this host}}"
infisical_token=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$INFISICAL_MACHINE_IDENTITY_ID" INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET" infisical login --method=universal-auth --domain={shlex.quote(secrets.domain)} --plain)
"""


def _secrets_run_prefix(secrets: SecretsDefaults, paths: tuple[str, ...]) -> str:
    """The `infisical run ... --` prefix for one command.

    Reuses the ``$infisical_token`` set by :func:`_secrets_login`, so this
    can prefix any number of commands in the same script. The token is
    passed via ``INFISICAL_TOKEN=...`` env-var prefix rather than
    ``--token`` for the same argv-exposure reason as :func:`_secrets_login`.
    """
    path_flags = " ".join(f"--path={shlex.quote(p)}" for p in paths)
    return (
        f'INFISICAL_TOKEN="$infisical_token" infisical run --env={shlex.quote(secrets.environment)} {path_flags} '
        f"--projectId={shlex.quote(secrets.project_id)} --domain={shlex.quote(secrets.domain)} -- "
    )


def _wrapped_commands_block(
    secrets: SecretsDefaults | None,
    commands: list[tuple[str, tuple[str, ...] | None]],
) -> str:
    """Build the indented command sequence for the body of a `(...)` block.

    ``commands`` is an ordered ``(command, paths)`` list. A command is
    wrapped with Infisical injection scoped to its own ``paths`` when both
    ``secrets`` and that command's ``paths`` are set -- this lets one
    ``pre_up`` entry pull a narrower (or different) set of secrets than
    the target's own ``up`` command, via :class:`wharf.config.PreUpStep`.
    `infisical login` is emitted at most once regardless of how many
    commands need secrets or how their ``paths`` differ: login only
    establishes the machine identity's session token, which every
    `infisical run --path=...` call reuses -- ``paths`` only controls
    what each call injects, not what the token itself can authenticate.
    """
    has_secrets = secrets is not None and any(paths for _, paths in commands)
    login_lines = _secrets_login(secrets).rstrip("\n").splitlines() if has_secrets else []
    command_lines = [
        f"{_secrets_run_prefix(secrets, paths)}{cmd}" if (secrets and paths) else cmd
        for cmd, paths in commands
    ]
    return "\n".join(f"  {line}" for line in login_lines + command_lines)


def render_up(
    *,
    remote_repo: str,
    remote_dir: str,
    compose_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
    pre_up: tuple[PreUpStep, ...] | None = None,
) -> str:
    """Deploy action: checkout $REVISION, run pre_up hooks, build, start, prune old images.

    Each ``pre_up`` entry runs as ``docker compose run --rm -T --build
    <service> </dev/null`` before the final ``up`` -- for one-off
    migration/bootstrap commands. ``--build`` is mandatory: `docker
    compose run` reuses a cached image by default, so without it a
    migration could run against the previous release's image while `up
    --build` builds and starts the new one. ``-T`` and ``</dev/null`` are
    both mandatory: the whole script is piped into `ssh ... bash -s` over
    stdin (see :func:`wharf.ssh.run_remote_script`). ``-T`` only disables
    pseudo-TTY allocation -- it does not disable `docker compose run`'s
    ``--interactive`` default, so without ``</dev/null`` the container can
    still attach to and drain that same stdin pipe, silently swallowing
    the rest of the script (including the final `up`) while the process
    still exits 0.

    Each step's own ``paths`` (if set) scope its secrets injection
    independently of the target's ``paths``, which is what the final
    ``up`` command always uses.
    """
    pre_up_commands = [
        (
            f'docker compose -f "$compose_file" run --rm -T --build {shlex.quote(step.service)} </dev/null',
            step.paths if step.paths is not None else paths,
        )
        for step in (pre_up or ())
    ]
    commands_block = _wrapped_commands_block(
        secrets,
        [*pre_up_commands, ('docker compose -f "$compose_file" up -d --build --remove-orphans', paths)],
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
        secrets, [('docker compose -f "$compose_file" up -d --remove-orphans', paths)]
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
