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


def _secrets_wrap(secrets: SecretsDefaults, paths: tuple[str, ...]) -> str:
    """The `infisical login` + `infisical run --` prefix for a compose call.

    ``INFISICAL_MACHINE_IDENTITY_ID`` / ``_CLIENT_SECRET`` are read from
    whatever environment the remote shell already has -- provisioned once
    per host, outside of wharf's concern. wharf never generates, stores,
    or transmits these credentials itself.
    """
    path_flags = " ".join(f"--path={shlex.quote(p)}" for p in paths)
    return f"""\
: "${{INFISICAL_MACHINE_IDENTITY_ID:?INFISICAL_MACHINE_IDENTITY_ID is required on this host}}"
: "${{INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET:?INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET is required on this host}}"
infisical_token=$(infisical login --method=universal-auth \\
  --domain={shlex.quote(secrets.domain)} \\
  --client-id="$INFISICAL_MACHINE_IDENTITY_ID" \\
  --client-secret="$INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET" \\
  --plain)
infisical run --env={shlex.quote(secrets.environment)} {path_flags} \\
  --projectId={shlex.quote(secrets.project_id)} \\
  --domain={shlex.quote(secrets.domain)} \\
  --token "$infisical_token" \\
  -- """


def render_up(
    *,
    remote_repo: str,
    remote_dir: str,
    compose_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
) -> str:
    """Deploy action: checkout $REVISION, build, start, prune old images."""
    wrap = _secrets_wrap(secrets, paths) if secrets and paths else ""
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
  {wrap}docker compose -f "$compose_file" up -d --build --remove-orphans
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
    that haven't changed.
    """
    wrap = _secrets_wrap(secrets, paths) if secrets and paths else ""
    return f"""\
set -euo pipefail
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
lock_file="$remote_dir/{_LOCK_FILE_NAME}"
mkdir -p "$remote_dir"

(
  flock -x 200 || {{ echo "ERROR: deploy lock is held, aborting" >&2; exit 1; }}
  cd "$remote_dir"
  {wrap}docker compose -f "$compose_file" up -d --remove-orphans
  echo "Reloaded"
) 200>>"$lock_file"
"""
