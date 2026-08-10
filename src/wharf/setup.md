# `setup.py`

Implements `wharf setup` — the one-time bootstrap command that
provisions what a config file *assumes already exists*: a dedicated
deploy SSH keypair, a bare git repo per target, and that keypair's
public half installed in each target's `authorized_keys`.

It uses the **operator's own existing SSH access** (default
agent/identity, prompts allowed) to provision targets — there's no
chicken-and-egg problem, since you need working SSH access to a host
already to set anything up on it. Like [`rotate.py`](rotate.md), it
bypasses [`SessionAuth`](ssh.md)'s CI/batch resolution for its own
connection — `--identity` picks which key gets *installed* or
*rotated*, never which key authenticates the request making that
change.

## `ensure_deploy_keypair(identity, key_dir=.wharf)`

Thin wrapper around [`identity.generate_keypair`](identity.md):
idempotent (if the identity's key already exists, it's left as-is and
reused), and prints which path it used. `identity == "default"` keeps
writing to `.wharf/deploy_key` exactly as `wharf setup` has always done.

## `setup(config, *, repo, only=(), identity=None, force_ci=None)`

Resolves the identity (`identity.resolve_identity` — explicit
`--identity`, else `"ci"` in CI, else `"default"`), ensures that
identity's keypair exists, then for each selected target,
`_provision_target`:

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
