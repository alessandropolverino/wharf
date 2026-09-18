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
  - `address` — `host:port`, with an IPv6 host bracketed
    (`[2001:db8::1]:22`); used for progress output and the push URL.
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

Beyond types, a few fields have shape rules:

- **`remote_repo`** and each target's **`remote_dir`** must be absolute
  paths. A relative `remote_repo` can't form a valid `ssh://` push URL
  (`ssh://deploy@host:22srv/git/...` — git reads `22srv` as part of the
  host), and a leading `~` is never expanded because the paths are
  shell-quoted everywhere they're used — `mkdir -p '~/app'` creates a
  directory literally named `~`.
- **`host`** must be a hostname, an IPv4 address, or an *unbracketed*
  IPv6 address; brackets are added where a URL needs them (see
  `Target.address`).
- Duplicate target `name`s or `order`s are rejected, and the error names
  the duplicated values.

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
- **`user`** must match `^[A-Za-z0-9_][A-Za-z0-9._@-]*$`. It's the first
  half of the `user@host` argument handed to `ssh`, so a leading `-`
  would be parsed as an option — `-oProxyCommand=...` runs a local
  command. Defense in depth again: [`ssh.build_ssh_argv`](ssh.md) also
  puts `--` before the destination. (`host` can't start with `-` either,
  per the rule above.)

## `render_repo_template(template, repo)`

Substitutes the `{repo}` placeholder used in `remote_repo` and
`remote_dir`. Deliberately a plain `str.replace`, not `str.format` —
these are filesystem/git paths, which may legitimately contain other
brace-like characters that shouldn't be treated as format fields.
