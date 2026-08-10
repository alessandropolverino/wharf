# `identity.py`

Named deploy-key identities: resolution, on-disk layout, and the
`authorized_keys` comment marker used as rotation bookkeeping. Shared by
[`setup.py`](setup.md), [`rotate.py`](rotate.md), `cli.py`'s
`identities` command, and [`ssh.py`](ssh.md)'s `SessionAuth` — resolving
an identity name and finding its key files happens in exactly one place
so those consumers can't drift apart on what a marker or a file path
looks like.

## `resolve_identity(explicit, *, is_ci)`

`--identity` wins if given; otherwise `"ci"` in CI, `"default"`
locally. `"default"` exists solely so anyone who never passes
`--identity` gets exactly wharf's original single-key behavior,
unchanged.

Identity names are restricted to `^[a-z0-9][a-z0-9-]*$`
(`validate_identity_name`) — a charset with no regex metacharacters, so
it's always safe to embed directly in a filename or in the `grep`
pattern [`rotate.py`](rotate.md) builds, with nothing to escape.

## `key_paths(identity, key_dir=.wharf)` / `key_comment(identity)`

- `identity == "default"` → `.wharf/deploy_key` / `.wharf/deploy_key.pub`,
  comment `wharf-deploy` — the exact path and comment wharf has always
  used, so existing installs need no migration.
- any other identity → `.wharf/keys/<identity>_key` /
  `.wharf/keys/<identity>_key.pub`, comment `wharf:<identity>`.

The comment is what `ssh-keygen -C` writes into the public key file, so
it travels verbatim into the target's `authorized_keys` line once
provisioned. That string *is* the bookkeeping `rotate` reads back from
the target to know which lines belong to which identity — there's no
separate state file.

## `generate_keypair(private_key, comment)`

Shells out to `ssh-keygen -t ed25519` rather than adding a crypto
library dependency, and creates the key's parent directory as needed.
Used by both `setup.ensure_deploy_keypair` (the live key) and
`rotate._ensure_staged_keypair` (the staged replacement).

## `staged_key_paths(private_key)`

Where `rotate` stages a replacement keypair (`<name>.new` /
`<name>.new.pub`) before promoting it to the live path — see
[`rotate.md`](rotate.md).

## `list_identities(key_dir=.wharf)`

Backs `wharf identities`. Scans `.wharf/deploy_key` plus every
`.wharf/keys/*_key` file and returns an `IdentityInfo` per identity
found (name, key paths, comment, and whether a staged `.new` file is
sitting there from an interrupted `rotate`). **Local-only, deliberately**
— reads only the current checkout's disk, never SSHes anywhere, so it
cannot detect a manually-edited `authorized_keys` or a partial rotation
that hasn't been retried yet.
