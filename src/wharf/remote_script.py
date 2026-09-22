"""Built-in remote-side bash for wharf's up/down/reload/rollback actions
and the read-only status/logs/history scripts.

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

Every successful ``up`` (a deploy or a rollback) appends one line --
``<UTC timestamp> <full sha> <kind>`` -- to the target's history file
(see :func:`history_path`), which is what `wharf history`, `wharf status`
and `wharf rollback` read back. Besides the checkout itself and the lock
file, it's the only state wharf keeps on a target.

That file decides which revision a later `wharf rollback` deploys, so it
lives beside the *bare repo*, never inside ``remote_dir``, and is read
back only when its ownership and mode say the deploy user alone could
have written it -- see :func:`history_path` and :data:`_TRUST_CHECK`.
"""

from __future__ import annotations

import shlex

from .config import PreUpStep, SecretsDefaults

_LOCK_FILE_NAME = ".wharf-deploy.lock"
_STATE_DIR_SUFFIX = ".wharf"

# The deploy history is an input to `wharf rollback`, so it is only
# believed when nothing but its owner could have written it, and that
# owner is the user wharf logs in as. `stat -c` is GNU/busybox syntax
# (like the `flock` and `mapfile` these scripts already rely on), with
# the BSD spelling as a fallback; if neither answers, the file is not
# trusted rather than trusted blindly.
_TRUST_CHECK = """wharf_untrusted() {
  local path=$1 mode owner
  mode=$(stat -c %a "$path" 2>/dev/null || stat -f %Lp "$path" 2>/dev/null) \
    || { echo "$path: cannot check its permissions"; return; }
  owner=$(stat -c %u "$path" 2>/dev/null || stat -f %u "$path" 2>/dev/null) \
    || { echo "$path: cannot check its owner"; return; }
  [ "$owner" = "$(id -u)" ] || { echo "$path is owned by uid $owner, not by the deploy user (uid $(id -u))"; return; }
  (( (8#$mode & 0022) == 0 )) || { echo "$path is mode $mode -- writable by group or other"; return; }
}
# Checks each path in turn, stopping at the first one that fails -- used
# for a history file together with its directory (a writable directory
# lets its owner replace the file between one deploy/read and the next).
# `stat` (unlike a plain redirect) does not follow a symlink, so a path
# planted as a symlink is judged by the symlink's own owner/mode, not
# the target's -- an attacker's symlink is caught the same as their
# regular file would be. A path that doesn't exist yet (and isn't even
# a dangling symlink) is skipped rather than treated as untrusted: it
# has nothing on it yet for anyone to have forged.
wharf_untrusted_any() {
  local path result
  for path in "$@"; do
    [ -e "$path" ] || [ -L "$path" ] || continue
    result=$(wharf_untrusted "$path")
    if [ -n "$result" ]; then
      echo "$result"
      return
    fi
  done
}"""


def history_path(remote_repo: str, target_name: str) -> str:
    """Where a target's deploy history lives on the host.

    Beside the bare repo, deliberately **not** inside ``remote_dir``:
    that is the compose project directory, which compose files routinely
    bind-mount into containers (``volumes: [".:/app"]``), and
    ``git checkout -f`` does not remove untracked files, so a forged
    record would survive later deploys. A workload that could write it
    would choose what the next `wharf rollback` deploys -- any revision
    in the bare repo, including one whose bug was since patched.

    The bare repo is already the trusted source of the code itself, so
    keeping the record beside it adds no trust that isn't there already.
    """
    base = remote_repo[:-4] if remote_repo.endswith(".git") else remote_repo
    return f"{base}{_STATE_DIR_SUFFIX}/{target_name}.history"


# -n: fail fast instead of queueing behind a running deploy/down/reload.
# A blocking flock waits indefinitely behind a hung run, and a deploy
# queued behind another one would check out its revision afterwards --
# silently rolling back if the revision that just went out is newer.
_ACQUIRE_LOCK = (
    'flock -n -x 200 || { echo "ERROR: another wharf deploy/down/reload holds '
    '$lock_file on this target, aborting" >&2; exit 1; }'
)


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
    history_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
    pre_up: tuple[PreUpStep, ...] | None = None,
    kind: str = "deploy",
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

    The checkout is always of a bare SHA, i.e. a detached HEAD, so git's
    multi-paragraph "detached HEAD" advice is switched off rather than
    repeated in every deploy log.

    Once the services are up, the deploy is appended to ``history_file``
    (see :func:`history_path`) as ``kind`` (``deploy``, or ``rollback``
    when :func:`wharf.operations.rollback` re-deploys an earlier
    revision), with the *resolved* sha -- ``$REVISION`` may be a tag or
    branch name. A failed ``pre_up`` or ``up`` records nothing. The
    record is created under ``umask 077`` and left mode 600, so only the
    deploy user can write it; failing to record it warns rather than
    failing a deploy whose services are already up.

    Before appending, the directory and (if one is already there) the
    file are put through the same trust check the read side uses: `>>`
    follows a symlink, so without this a pre-planted one in a directory
    that isn't exclusively the deploy user's could redirect the append
    to any file that user can write.
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
history_file={shlex.quote(history_file)}
history_dir=$(dirname "$history_file")
{_TRUST_CHECK}
mkdir -p "$remote_dir"

(
  {_ACQUIRE_LOCK}

  old_images=()
  if [ -f "$remote_dir/$compose_file" ]; then
    mapfile -t old_images < <(cd "$remote_dir" && \\
      docker compose -f "$compose_file" images -q 2>/dev/null | sort -u || true)
  fi

  git -c advice.detachedHead=false --work-tree="$remote_dir" --git-dir="$remote_repo" checkout -f "$REVISION"
  deployed_revision=$(git --git-dir="$remote_repo" rev-parse HEAD)
  echo "Code deployed to $remote_dir (revision ${{deployed_revision:0:7}})"

  cd "$remote_dir"
{commands_block}
  echo "Services started"
  history_problem=""
  if ! (umask 077; mkdir -p "$history_dir") 2>/dev/null; then
    history_problem="could not create $history_dir"
  else
    history_problem=$(wharf_untrusted_any "$history_dir" "$history_file")
  fi
  if [ -n "$history_problem" ]; then
    echo "WARNING: not recording this deploy in $history_file -- $history_problem (rollback will not see it)" >&2
  elif printf '%s %s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$deployed_revision" {shlex.quote(kind)} >> "$history_file"; then
    chmod 600 "$history_file" 2>/dev/null || true
  else
    echo "WARNING: could not record this deploy in $history_file (rollback will not see it)" >&2
  fi

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
  {_ACQUIRE_LOCK}
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
  {_ACQUIRE_LOCK}
  cd "$remote_dir"
{commands_block}
  echo "Reloaded"
) 200>>"$lock_file"
"""


def render_status(
    *,
    remote_repo: str,
    remote_dir: str,
    compose_file: str,
    history_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
) -> str:
    """Status action: report what's on the target, changing nothing.

    Prints the checked-out revision (the bare repo's HEAD, which
    :func:`render_up`'s `checkout -f` moves), the last history entry,
    whether the deploy lock is held right now, and `docker compose ps`.
    The lock is probed with a non-blocking `flock` on a *read-only*
    descriptor, so unlike the other scripts this one never creates the
    lock file -- or anything else. A target that was never deployed is
    reported as such, not treated as an error.

    The last deploy is reported from ``history_file`` only when it and its
    directory pass the same trust check `wharf history` uses; otherwise
    the line says so instead of quoting a record anything could have
    written.

    `docker compose ps` gets the same secrets wrapping as `up` when the
    target declares ``paths``: compose still interpolates the file for
    `ps`, so a ``${VAR:?}`` reference would otherwise fail.
    """
    ps_block = _wrapped_commands_block(secrets, [('docker compose -f "$compose_file" ps', paths)])
    return f"""\
set -euo pipefail
remote_repo={shlex.quote(remote_repo)}
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
lock_file="$remote_dir/{_LOCK_FILE_NAME}"
history_file={shlex.quote(history_file)}
history_dir=$(dirname "$history_file")
{_TRUST_CHECK}

if [ ! -d "$remote_dir" ]; then
  echo "not deployed: $remote_dir does not exist"
  exit 0
fi
if revision=$(git --git-dir="$remote_repo" rev-parse --verify -q HEAD 2>/dev/null); then
  echo "revision: ${{revision:0:7}} $(git --git-dir="$remote_repo" log -1 --format=%s HEAD)"
else
  echo "revision: nothing checked out"
fi
if [ -s "$history_file" ]; then
  untrusted=$(wharf_untrusted_any "$history_dir" "$history_file")
  if [ -n "$untrusted" ]; then
    echo "last deploy: not trusted -- $untrusted"
  else
    timestamp= deployed= kind=
    read -r timestamp deployed kind _ < <(tail -n 1 "$history_file") || true
    echo "last deploy: $timestamp (${{kind:-deploy}} of ${{deployed:0:7}})"
  fi
else
  echo "last deploy: no history recorded"
fi
if [ ! -e "$lock_file" ]; then
  echo "lock: free (never taken)"
elif ! command -v flock >/dev/null; then
  echo "lock: unknown (flock is not installed)"
else
  ( flock -n 200 && echo "lock: free" || echo "lock: held (a deploy, down or reload is running)" ) 200<"$lock_file"
fi
if [ -f "$remote_dir/$compose_file" ]; then
  cd "$remote_dir"
{ps_block}
else
  echo "compose: $compose_file not found in $remote_dir"
fi
"""


def render_logs(
    *,
    remote_dir: str,
    compose_file: str,
    secrets: SecretsDefaults | None,
    paths: tuple[str, ...] | None,
    services: tuple[str, ...] = (),
    follow: bool = False,
    tail: str = "100",
    since: str | None = None,
) -> str:
    """Logs action: `docker compose logs` for a target, optionally followed.

    ``tail`` (a line count, or ``all``) and ``since`` are passed through
    to compose. Wrapped with secrets like `up` when the target declares
    ``paths`` -- see :func:`render_status`. ``</dev/null`` for the same
    reason as `pre_up`'s `run`: the script arrives on stdin, and a
    long-running child must not be able to read it.
    """
    flags = [f"--tail={shlex.quote(tail)}"]
    if since is not None:
        flags.append(f"--since={shlex.quote(since)}")
    if follow:
        flags.append("--follow")
    words = " ".join([*flags, *(shlex.quote(service) for service in services)])
    logs_block = _wrapped_commands_block(
        secrets, [(f'docker compose -f "$compose_file" logs {words} </dev/null', paths)]
    )
    return f"""\
set -euo pipefail
remote_dir={shlex.quote(remote_dir)}
compose_file={shlex.quote(compose_file)}
[ -d "$remote_dir" ] || {{ echo "not deployed: $remote_dir does not exist" >&2; exit 1; }}
cd "$remote_dir"
{logs_block}
"""


def render_history(*, remote_repo: str, history_file: str, limit: int | None = None) -> str:
    """History action: print the target's deploy history, oldest first. Read-only.

    Each history line (``<timestamp> <revision> <kind>``, as appended by
    :func:`render_up`) is echoed back with the revision's commit subject
    looked up in the bare repo, tab-separated, for
    :func:`wharf.operations.parse_history` to read. ``limit`` keeps only
    the most recent entries. A target with no history file prints nothing.

    Two guards, because this output chooses what `wharf rollback`
    deploys. The file and its directory must pass :data:`_TRUST_CHECK`,
    or the script refuses rather than reporting a record that something
    else may have written. And each revision must be a plain hex object
    name before it reaches `git`: a value like ``--output=<path>`` would
    otherwise be read by `git log` as an *option* and write that file as
    the deploy user (``--`` can't help -- after it, git takes the
    argument as a pathspec rather than a revision).
    """
    source = f"tail -n {int(limit)}" if limit is not None else "cat"
    return f"""\
set -euo pipefail
remote_repo={shlex.quote(remote_repo)}
history_file={shlex.quote(history_file)}
history_dir=$(dirname "$history_file")
{_TRUST_CHECK}
[ -f "$history_file" ] || exit 0
untrusted=$(wharf_untrusted_any "$history_dir" "$history_file")
if [ -n "$untrusted" ]; then
  echo "refusing to read the deploy history: $untrusted" >&2
  echo "wharf believes this file only if the deploy user alone can write it" >&2
  exit 1
fi
{source} "$history_file" | while read -r timestamp revision kind _; do
  case "$revision" in ''|*[!0-9a-f]*) continue;; esac
  [ ${{#revision}} -ge 40 ] || continue
  subject=$(git --git-dir="$remote_repo" log -1 --format=%s "$revision" 2>/dev/null || true)
  printf '%s %s %s\\t%s\\n' "$timestamp" "$revision" "${{kind:-deploy}}" "$subject"
done
"""
