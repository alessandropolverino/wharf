# `pre_up` hook design

## Problem

wharf's `render_up` (deploy action) always runs `docker compose -f <compose_file> up -d --build --remove-orphans` and nothing else. Some units — starting with janus-infra's `core` unit — need to run one-off migration/bootstrap commands (`docker compose run --rm <service>`) *before* `up`, e.g. database migrations. wharf has no way to express this today, so those units can't move off the old per-repo `deploy_prod.sh`/`deploy_staging.sh` scripts.

## Schema

New optional per-target field, a list of compose service names:

```yaml
targets:
  - name: core
    compose_file: compose/docker-compose.core.prod.yml
    pre_up: [migrate-janus, bootstrap-dashboard-admin, migrate-janusdashboard]
    ...
```

`config.py`: add `pre_up: tuple[str, ...] | None = None` to `Target`. Unlike `paths`, a `pre_up` entry becomes a literal argv token passed to `docker compose run --rm --build <service>` on the remote host (see Execution below) — a value that shell-quotes cleanly but starts with `-` (e.g. `--rm`, `--entrypoint=sh`) would still be parsed by `docker compose` as a flag, not a service name, regardless of quoting. So `pre_up` gets its own validator, `_compose_service_name`, instead of reusing `_nonempty_string`/`_string_list` as-is: each entry must be non-empty, must not start with `-`, and must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` (the character set Compose itself allows for service names).

## Secrets

No new secrets fields. If the target declares `paths` (i.e. `uses_secrets`), every `pre_up` command *and* the final `up` are wrapped with the same Infisical injection, matching the old scripts — they fetch one token and reuse it across all migration calls plus the final `up`.

`remote_script.py` refactor: split `_secrets_wrap` into:
- `_secrets_login(secrets)` — the `infisical login ...` call, emitted once per script.
- `_secrets_run_prefix(secrets, paths)` — the `infisical run --env=... --path=... --token "$infisical_token" -- ` prefix, reused before each wrapped command.

This avoids calling `infisical login` once per command (as a naive reuse of the old single-string `_secrets_wrap` would if used twice in one script).

## Execution

In `render_up`, each `pre_up` entry becomes:

```bash
{run_prefix}docker compose -f "$compose_file" run --rm --build {shlex.quote(service)}
```

with the service name passed through `shlex.quote` at render time, same as every other config value `remote_script.py` embeds (`remote_repo`, `remote_dir`, `compose_file`, ...) — belt-and-suspenders alongside the `_compose_service_name` validation above, not a substitute for it.

The `--build` flag is required, not cosmetic: `docker compose run` reuses a cached image if one already exists and only builds when forced to, so without `--build` a migration container can run against the previous release's image while the final `up --build` builds and starts the new one — i.e. migrations for schema changes the new code expects would run before those changes exist. Passing `--build` on every `pre_up` invocation forces each to build first; Compose's layer cache means the later `up -d --build` is then a no-op rebuild, not a second real build.

`pre_up` commands are emitted in list order, after checkout and before the final `up -d --build --remove-orphans` line. `render_down` and `render_reload` are unchanged — the old scripts never ran migrations on down or reload either.

## Failure handling

No new code needed. `render_up`'s script already starts with `set -euo pipefail`, so a non-zero exit from any `pre_up` command aborts the script before `up` runs, exactly like any other step failing today.

## Testing

Add `tests/test_remote_script.py` (this module currently has no direct test coverage):
- A target with no `pre_up` renders identically to today (no regression).
- A target with `pre_up` renders each `run --rm --build <service>` line, in order, after checkout and before `up`, with the service name `shlex.quote`d.
- With `secrets`/`paths` configured, `infisical login` appears exactly once in the rendered script regardless of how many `pre_up` entries exist, and each `pre_up`/`up` line is prefixed with `infisical run ... --token "$infisical_token" --`.

Add to `tests/test_config.py`:
- `pre_up` entries are accepted when they match `^[A-Za-z0-9][A-Za-z0-9._-]*$`.
- A `pre_up` entry starting with `-` (e.g. `--rm`, `-x`) is rejected with a `ConfigError`, so a malformed/malicious entry can't be interpreted as a `docker compose run` flag.
- An empty `pre_up` entry, or a value containing shell metacharacters (`;`, `` ` ``, `$(`, whitespace) outside the allowed character set, is rejected.

## Scope

This is a general wharf feature (any target may declare `pre_up`), not infra-specific — janus-infra's `core` unit is just the first consumer. Writing janus-infra's actual `.janus/deploy.yml`/`deploy.staging.yml` `core` target (including its exact service list, which repeats `bootstrap-dashboard-admin` twice per the existing scripts) is a follow-up task, not part of this design.
