# `rotate.py`

Implements `wharf rotate`: replaces an identity's deploy key across
every selected target, removing the old `authorized_keys` entry instead
of `setup`'s purely-additive behavior. See [`identity.md`](identity.md)
for how an identity resolves to key paths and its `authorized_keys`
marker.

## Staging

`_ensure_staged_keypair` generates the replacement to a **staged** path
(`identity.staged_key_paths`) rather than overwriting the live key file
immediately. If a staged pair already exists — from a previous `rotate`
run that got interrupted partway through the targets — it's reused
instead of generating another, the same "if it exists, leave it"
idempotency `setup.ensure_deploy_keypair` already uses.

## Per-target script

`_render_rotate_script` builds one script per target: filter every
`authorized_keys` line matching the identity's marker pattern (its
`identity.key_comment`, anchored as `" <comment>$"` so e.g. `wharf:ci`
can never match a sibling `wharf:ci-staging` line) out to a temp file
created by `mktemp` *inside* `~/.ssh` itself, unconditionally append the
new key, then atomically `mv` the temp file over `authorized_keys`.
Staging the temp file in the same directory as the target (rather than
the default `/tmp`) is what makes that `mv` an atomic same-filesystem
rename instead of a cross-device copy+unlink, and lets the file inherit
`~/.ssh`'s permissions/SELinux context instead of `/tmp`'s. Filtering
and re-appending in one pass — rather than adding the new key and
separately removing the old one — means there's never an intermediate
write where the live file has neither key, and running the same script
twice converges to the same state (the new key's own line also carries
the marker, so it gets filtered out and unconditionally re-added on
every run).

`_rotate_target` runs that script over `SessionAuth(batch=False)` — the
operator's own identity, same as `setup._provision_target` — never the
key being rotated, for the same anti-chicken-and-egg reason `setup`
already relies on.

## Promotion

`rotate()` only promotes the staged keypair to the live key path — via
`Path.rename`, which atomically replaces the live private/public key
files with the staged ones — **after every selected target succeeds**.
Targets are processed sequentially in config order, stopping at the
first failure — if one fails, the live key files are left untouched and
the staged files persist, so re-running `rotate` is the recovery path,
not a separate resume command.

Prints the same "update your CI secret" instruction
[`setup.py`](setup.md) does, pointing at the now-live new private key.
