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

`config.py`: add `pre_up: tuple[str, ...] | None = None` to `Target`, parsed with the existing `_string_list` helper (already used for `paths`). No new validation logic — a compose service name has the same "non-empty string" shape as a path.

## Secrets

No new secrets fields. If the target declares `paths` (i.e. `uses_secrets`), every `pre_up` command *and* the final `up` are wrapped with the same Infisical injection, matching the old scripts — they fetch one token and reuse it across all migration calls plus the final `up`.

`remote_script.py` refactor: split `_secrets_wrap` into:
- `_secrets_login(secrets)` — the `infisical login ...` call, emitted once per script.
- `_secrets_run_prefix(secrets, paths)` — the `infisical run --env=... --path=... --token "$infisical_token" -- ` prefix, reused before each wrapped command.

This avoids calling `infisical login` once per command (as a naive reuse of the old single-string `_secrets_wrap` would if used twice in one script).

## Execution

In `render_up`, each `pre_up` entry becomes:

```bash
{run_prefix}docker compose -f "$compose_file" run --rm <service>
```

emitted in list order, after checkout and before the final `up -d --build --remove-orphans` line. `render_down` and `render_reload` are unchanged — the old scripts never ran migrations on down or reload either.

## Failure handling

No new code needed. `render_up`'s script already starts with `set -euo pipefail`, so a non-zero exit from any `pre_up` command aborts the script before `up` runs, exactly like any other step failing today.

## Testing

Add `tests/test_remote_script.py` (this module currently has no direct test coverage):
- A target with no `pre_up` renders identically to today (no regression).
- A target with `pre_up` renders each `run --rm <service>` line, in order, after checkout and before `up`.
- With `secrets`/`paths` configured, `infisical login` appears exactly once in the rendered script regardless of how many `pre_up` entries exist, and each `pre_up`/`up` line is prefixed with `infisical run ... --token "$infisical_token" --`.

## Scope

This is a general wharf feature (any target may declare `pre_up`), not infra-specific — janus-infra's `core` unit is just the first consumer. Writing janus-infra's actual `.janus/deploy.yml`/`deploy.staging.yml` `core` target (including its exact service list, which repeats `bootstrap-dashboard-admin` twice per the existing scripts) is a follow-up task, not part of this design.
