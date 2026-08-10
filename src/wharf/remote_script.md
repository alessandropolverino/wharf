# `remote_script.py`

Generates the bash scripts wharf runs on the remote target for the
`deploy`, `down`, and `reload` actions. These replace the per-repo
`deploy_prod.sh` script wharf's predecessor used — the locking, checkout,
secrets-wrapping, and image-cleanup logic now lives once, inside wharf
itself, driven entirely by the config file.

Each `render_*` function returns a **complete, self-contained bash
script**, meant to be piped to `ssh ... bash -s` (see
[`ssh.md`](ssh.md)'s `run_remote_script`). Config values (paths, compose
file, secrets location) are embedded as shell-quoted literals at render
time; only `REVISION` travels as an environment variable, so the script
text stays identical across deploys of the same target — useful when
eyeballing what actually ran in a log.

## `render_up(...)` — the `deploy` script

In order:

1. `flock` the deploy lock file — a concurrent deploy on the same
   `remote_dir` aborts loudly instead of racing.
2. Record the currently-running image IDs (for cleanup at the end).
3. `git checkout -f $REVISION` into `remote_dir` from the bare repo
   pushed by [`git_ops.md`](git_ops.md).
4. Run each `pre_up` entry: `docker compose run --rm -T --build <service>
   </dev/null`.
5. `docker compose up -d --build --remove-orphans`.
6. Remove any of step 2's images no longer referenced by a running
   container.

**Why `--build` is mandatory on `pre_up`'s `run`:** `docker compose run`
reuses a cached image by default. Without `--build`, a migration could
run against the *previous* release's image while `up --build` builds and
starts the new one — silently migrating against stale code.

**Why `-T` and `</dev/null` are both mandatory:** the whole script is
piped into `ssh ... bash -s` over stdin. `-T` only disables pseudo-TTY
allocation — it does *not* disable `docker compose run`'s `--interactive`
default, so without `</dev/null` the container can still attach to and
drain that same stdin pipe, silently swallowing the rest of the script
(including the final `up`) while the process still exits 0.

**Failure semantics:** `set -euo pipefail` means a failing `pre_up`
command aborts the script *after* the checkout but *before* `up` — the
target's working tree has the new revision's code, but the old
containers are still running. See
[`configuration.md`](../../docs/configuration.md) for what this means when
debugging a failed migration.

## `render_down(...)` / `render_reload(...)`

- **`down`**: `docker compose down` (optionally `--volumes`), under the
  same lock. No secrets, no checkout.
- **`reload`**: `docker compose up -d` **without** `--build`, against
  whatever revision is already checked out. No `pre_up` — reload doesn't
  check out a new revision, so there's nothing new to migrate. Useful
  after rotating a secret, or to just restart services.

## Secrets injection

A target opts into secrets by declaring `paths` (see
[`config.md`](config.md)). When it does, every wrapped command is
prefixed to authenticate and inject secrets from Infisical:

```
infisical_token=$(INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=... INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET=... \
  infisical login --method=universal-auth --domain=<domain> --plain)

INFISICAL_TOKEN="$infisical_token" infisical run --env=<env> --path=<path> ... -- <command>
```

- **`_secrets_login`** runs `infisical login` exactly **once** per
  script, regardless of how many commands reuse the resulting
  `$infisical_token` — a naive "login + run" wrap per command would call
  `infisical login` once per command instead.
- **`_secrets_run_prefix`** builds the per-command `infisical run ... --`
  prefix, reusing that token.
- **Credentials are passed as env-var prefixes, never CLI flags.**
  `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=... infisical login` sets the
  child process's environment directly; `infisical login
  --client-id=...` would put the literal secret value in the process's
  argv, which any local user on the target host can read via `ps` for
  the life of the process. Same reasoning for `INFISICAL_TOKEN=...`
  vs. `--token`.
- **Per-`pre_up`-step scoping**: each `pre_up` entry can declare its own
  `paths` (via the config's mapping form), wrapped independently of the
  target's own `paths`. `_wrapped_commands_block` takes a `(command,
  paths)` list and only shares the *login* across commands — each
  command's `--path` flags come from its own scope. This narrows what
  secrets land in a given command's environment (e.g. a migration only
  gets `db` credentials, not the app's full runtime secret set) without
  requiring a second `infisical login`. Note this doesn't shrink the
  underlying token's actual authorization — that's controlled by the
  Infisical machine identity's own ACLs, not by wharf's `--path` filter.

## Locking

Every script wraps its body in `( flock -x 200 || exit 1; ...) 200>>"$lock_file"`,
where `$lock_file` is `$remote_dir/.wharf-deploy.lock`. A concurrent
`deploy`/`down`/`reload` on the same target fails fast with "deploy lock
is held" instead of corrupting state.
