# `ssh.py`

SSH command construction and process execution. Every remote operation
goes through two building blocks defined here:

- **`SessionAuth`** decides *how* to authenticate and whether prompts are
  allowed, based on the environment.
- **`build_ssh_argv` / `build_git_ssh_command`** turn a target's
  `host_key` into a pinned, single-use `known_hosts` file, so wharf never
  falls back to "accept any host key" — the config file is the sole
  source of truth for what a target's host key should be.

## `SessionAuth`

- **`batch`** — `True` disables SSH's interactive prompting
  (`BatchMode=yes`). Must be `True` in CI (nothing can answer a
  passphrase/password prompt there), `False` locally so the operator's
  terminal handles prompts normally.
- **`identity_file`** — an explicit private key (from `DEPLOY_SSH_KEY` in
  CI). `None` means "use the default SSH agent/identity."

`SessionAuth.resolve(force_ci=None, identity=None)` picks the mode:
`force_ci` (the CLI's `--ci`/`--interactive` flags) overrides
autodetection via `is_ci()`, which checks the standard `CI`
env var GitHub Actions and most other CI systems set automatically.
`identity` (see [`identity.md`](identity.md)) only matters for **local**
runs: the `"default"` identity (or no identity) keeps using the ambient
SSH agent, exactly as before; any other identity resolves to that
identity's own key file (`.wharf/keys/<identity>_key`), so a bot or
automation script invoked outside CI can say `--identity release-bot`
and get a dedicated key instead of the ambient agent. In CI, `identity`
has no effect — CI always reads the same `DEPLOY_SSH_KEY` secret
regardless of which identity that secret happens to belong to.

In CI mode, the private key from `DEPLOY_SSH_KEY` is written to a
`mkstemp`-created temp file (not a hand-built path — `mkstemp` uses
`O_EXCL`, so it can't follow a pre-existing symlink at a predictable
path, and the name isn't guessable), `chmod 0600`, and cleaned up via
`atexit` since it needs to outlive individual SSH invocations (re-used
once per target).

## Host key pinning

`_pin_known_hosts(target)` writes a **fresh, single-purpose**
`known_hosts` file per invocation containing only `target.host_key` —
never the operator's real `~/.ssh/known_hosts`. A stale or unrelated
entry elsewhere on the machine can never substitute for the key
committed in the config. Combined with `StrictHostKeyChecking=yes`, an
unrecognized or mismatched host key hard-fails the connection instead of
prompting or silently trusting it.

Each pinned file is removed at process exit (via `atexit`, like the CI
key file) rather than right after use: `GIT_SSH_COMMAND` only carries
the path, and git reads the file later, from its own `ssh` child.

`build_ssh_argv` builds a full `ssh ...` argv including the destination,
preceded by `--` so the `user@host` argument can never be parsed as an
option — a `user` of `-oProxyCommand=...` would otherwise make ssh run
that command locally. ([`config.md`](config.md) already rejects such
user names; this is the second layer.) `build_git_ssh_command` builds
the same options **without** a destination (and without `--`), for
`GIT_SSH_COMMAND` — git appends its own `[-p port] user@host <command>`
onto that string itself, so baking a destination into it too would make
ssh see two destinations and treat the second as a remote command to
execute.

## `run_streaming(argv, *, description, env=None, input_text=None, capture=False)`

The one place that shells out to a subprocess. Deliberately does **not**
capture stdout/stderr — they're left attached to the parent process so
`git push` progress, `ssh` prompts, and `docker compose build` output
all show up live, exactly as if you'd typed the command yourself. Raises
`RemoteCommandError` on non-zero exit. With `capture=True` it returns
stdout as a string instead of streaming it — stderr still streams — for
the few scripts whose output wharf reads rather than shows.

## `run_remote_script(target, auth, script, env_vars, *, description)`

Runs a rendered script (from [`remote_script.md`](remote_script.md)) via
`ssh ... KEY=value... bash -l -s`, piped over stdin. `env_vars` (e.g.
`REVISION`) are set as a shell-level prefix on the remote command line,
the same technique the original per-repo deploy scripts used. That
remote command line comes from `remote_command(env_vars)`, which
[`operations`](operations.md)' dry run prints too, so the preview can't
drift from what actually runs.

**`-l` (login shell) is required, not cosmetic**: it's what makes bash
source `/etc/profile.d/*.sh` before running the script — where
Infisical's machine-identity credentials are expected to live (see
[`configuration.md`](../../docs/configuration.md#secrets)). A plain
non-login `bash -s` would silently skip that and leave those variables
unset.

## `capture_remote_script(target, auth, script, env_vars, *, description)`

`run_remote_script` with `capture=True`: same login shell, same command
line, but the script's stdout comes back as a string. Used for the
deploy history ([`remote_script.render_history`](remote_script.md)),
whose lines [`operations`](operations.md) parses rather than shows. Only
stdout is captured, so ssh prompts and remote error output still appear
live — and since a login shell may print whatever a profile script
echoes, the parser on the other side ignores lines that don't look like
history entries.
