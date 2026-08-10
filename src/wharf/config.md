# `config.py`

Loads and validates a wharf YAML config file into typed, immutable
dataclasses. This is the only place a config file is parsed — every other
module works with `Config`/`Target`/`SecretsDefaults`/`PreUpStep` objects,
never raw YAML.

One config file describes **one environment** (e.g. `deploy.yml` vs.
`deploy.staging.yml`). There's no `production:`/`staging:` wrapper key —
which environment a file represents is a fact about the filename, not
the schema.

## Types

- **`Config`** — the whole file: `remote_repo`, `branch`, `compose_file`,
  `secrets`, `targets` (always sorted by `order`), `ensure_branch`.
  - `compose_file_for(target)` — a target's own `compose_file` overrides
    the file-level default.
  - `select_targets(only=())` — targets in deploy order, optionally
    filtered by name; raises `ConfigError` if `--only` names an unknown
    target, so a typo fails loudly instead of silently deploying nothing.
- **`Target`** — one deploy destination: `name`, `remote_dir`, `host`,
  `port`, `user`, `host_key`, `order`, plus optional `healthcheck`,
  `compose_file`, `paths`, `pre_up`.
  - `uses_secrets` — true if the target's own `up` (via `paths`) or any
    `pre_up` step injects secrets.
- **`SecretsDefaults`** — shared Infisical location, defined once per
  file: `provider`, `project_id`, `domain`, `environment`. Only the
  *location* of secrets lives here — credentials themselves are never
  stored in the config; they're expected on the target host as
  environment variables (see [`remote_script.md`](remote_script.md)).
- **`PreUpStep`** — one `pre_up` list entry: `service` (a compose service
  name) plus optional `paths`. `paths=None` means "inherit the target's
  own `paths`" — that's what lets the plain-string shorthand
  (`pre_up: [migrate]`) and the mapping form
  (`pre_up: [{service: migrate, paths: [...]}]`) coexist without
  breaking existing configs.

## Loading

`load_config(path)` reads the file with `yaml.safe_load` (never
`yaml.load` — no arbitrary Python object deserialization), then runs it
through a chain of small validators, each raising `ConfigError` with a
field-scoped message (`targets[1].pre_up[0].paths`-style labels) so a bad
config fails with a precise, actionable error instead of a raw
`KeyError`/`TypeError`.

Every field is validated against an *exact* key set
(`_exact_keys` — required ∪ optional, nothing else) — an unrecognized key
anywhere in the file is a hard error, not a silently-ignored typo.

## Security-relevant validation

These exist specifically to keep a config file from becoming a remote
command-injection or credential-exfiltration vector — see also
[`remote_script.md`](remote_script.md)'s "defense in depth" note:

- **`host_key`** must parse as `ssh-ed25519 <base64>` — wharf only ever
  trusts the exact key pinned in the config (see [`ssh.md`](ssh.md)),
  never TOFU or the operator's own `known_hosts`.
- **`secrets.domain`** must be an `https` URL (not `http`) — this is
  where the Infisical machine-identity credentials get sent; plaintext
  HTTP would leak them on the wire.
- **Compose service names** (`pre_up` service, both forms) must match
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` — rejects shell metacharacters,
  leading `-` (which could be parsed as a flag), and structurally
  anything that isn't a plausible compose service name. This is
  defense-in-depth: `remote_script.py` also `shlex.quote()`s every
  service name at render time, independently of this regex.

## `render_repo_template(template, repo)`

Substitutes the `{repo}` placeholder used in `remote_repo` and
`remote_dir`. Deliberately a plain `str.replace`, not `str.format` —
these are filesystem/git paths, which may legitimately contain other
brace-like characters that shouldn't be treated as format fields.
