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
