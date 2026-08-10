# `setup.py`

Implements `wharf setup` — the one-time bootstrap command that
provisions what a config file *assumes already exists*: a dedicated
deploy SSH keypair, a bare git repo per target, and that keypair's
public half installed in each target's `authorized_keys`.

It uses the **operator's own existing SSH access** (default
agent/identity, prompts allowed) to provision targets — there's no
chicken-and-egg problem, since you need working SSH access to a host
already to set anything up on it. This is the only wharf command that
doesn't use [`SessionAuth`](ssh.md)'s CI/batch logic for its own
connection.

## `ensure_deploy_keypair(key_dir=.wharf)`

Shells out to `ssh-keygen -t ed25519` rather than adding a crypto
library dependency — SSH is already a hard requirement. Idempotent: if
`.wharf/deploy_key` already exists, it's left as-is and reused.

## `setup(config, *, repo, only=())`

For each selected target, `_provision_target`:

1. Opens an interactive SSH session (`bash -s`) using the operator's own
   identity.
2. `git init --bare` the target's `remote_repo` if it doesn't exist yet.
3. `mkdir -p` the target's `remote_dir`.
4. Appends the deploy keypair's public half to
   `~/.ssh/authorized_keys` (creating it with `700`/`600` permissions if
   needed), idempotently — `grep -qxF` first to avoid duplicate entries
   on repeated runs.

**Shell-injection note:** `remote_repo`, `remote_dir`, and `target.name`
are all `shlex.quote()`d before being interpolated into the script.
Interpolating them straight into a *double-quoted* `echo` string
wouldn't be enough — double quotes don't stop `$(...)`/backtick command
substitution — so the already-quoted variables are instead passed as
separate, unquoted-by-us `echo` arguments.

After provisioning every target, `setup` prints the one manual step it
deliberately does **not** automate: adding the private key as the CI's
`DEPLOY_SSH_KEY` secret. Shelling out to `gh` (or another provider's CLI)
on the operator's behalf would be more magic than a bootstrap command
should have — it's printed as a copy-pasteable instruction instead.
