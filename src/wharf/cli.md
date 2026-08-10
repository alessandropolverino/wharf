# `cli.py`

wharf's `argparse`-based command-line interface — the entry point wired
up as the `wharf` console script (see [`__main__.md`](__main__.md) for
the `python -m wharf` equivalent).

```
wharf deploy     <config.yml> [--only NAME...] [--repo NAME] [--revision SHA] [--identity NAME]
wharf down       <config.yml> [--only NAME...] [--volumes] [--identity NAME]
wharf reload     <config.yml> [--only NAME...] [--identity NAME]
wharf ls         <config.yml>
wharf setup      <config.yml> [--only NAME...] [--identity NAME]
wharf rotate     <config.yml> [--only NAME...] [--identity NAME]
wharf identities
```

Every subcommand except `ls` and `identities` also accepts
`--ci`/`--interactive` to override wharf's automatic CI-vs-local
detection (see [`ssh.md`](ssh.md)'s `is_ci`).

`rotate` and `identities` follow the same subparser pattern as the
other commands — `rotate` takes the same `<config.yml> [--only]
[--identity]` shape as `setup`; `identities` takes no config file
argument at all, since identity key files live under `.wharf/` at the
project root, not per config file. `--identity` is added to
`deploy`/`down`/`reload`/`setup`/`rotate` via the shared
`_add_identity_flag` helper, the same pattern `_add_ci_flags` already
uses.

## Flow

1. `build_parser()` — the full argparse tree, shared flags factored into
   `_add_common` (`config`, `--only`, `--repo`), `_add_ci_flags`
   (`--ci`/`--interactive`, mutually exclusive), and `_add_identity_flag`
   (`--identity`).
2. `main(argv)` dispatches on `args.command`:
   - `ls` loads the config and prints each target's order, host, and
     `[secrets]`/healthcheck annotations — never touches the network,
     so it skips both the update check and any SSH connection.
   - `identities` calls `identity.list_identities()` directly — no
     config file involved at all, so it also skips the update check.
   - `setup` and `rotate` each load the config, resolve `--repo`, call
     `setup_mod.setup`/`rotate_mod.rotate` directly, and catch
     `BranchMismatchError`/`RemoteCommandError` inline — not through
     `_run_operation`, since neither raises the per-target
     `OperationError` that `operations.py` wraps failures in.
   - `deploy`/`down`/`reload` load the config, resolve `--repo` and the
     CI/local auth mode, then call the matching
     [`operations`](operations.md) function through `_run_operation`.
3. Before dispatching (except for `ls`), a best-effort, silent-on-failure
   [`update_check`](update_check.md) runs — skipped in CI and when
   `WHARF_NO_UPDATE_CHECK` is set, so it never adds an unexpected network
   call where one isn't wanted.

## Error handling — `_run_operation` / `_load`

Config errors (`ConfigError`, `OSError`, `yaml.YAMLError`) are caught at
`_load()` and turned into a one-line `wharf: <path>: <message>` on
stderr with exit code 2 — never a raw traceback.

`_run_operation` (used for `deploy`/`down`/`reload`) catches the two
ways an `operations` call can fail:

- **`BranchMismatchError`** (config's `ensure_branch` doesn't match the
  local checkout) → exit 2.
- **`OperationError`** (a target failed) → its message is printed, and
  if the underlying cause was a `RemoteCommandError`, the **remote
  command's own exit code** is propagated (so a failed `docker compose
  build` on the target surfaces the same exit code locally) — otherwise
  exit 1.

`setup`/`rotate` reach the same two outcomes (exit 2 on
`BranchMismatchError`, the remote command's own exit code on failure)
without going through `_run_operation`, since `setup.setup`/
`rotate.rotate` raise `RemoteCommandError` straight from
[`ssh.run_streaming`](ssh.md) rather than a wrapping `OperationError`.
