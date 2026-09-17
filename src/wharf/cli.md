# `cli.py`

wharf's `argparse`-based command-line interface — the entry point wired
up as the `wharf` console script (see [`__main__.md`](__main__.md) for
the `python -m wharf` equivalent).

```
wharf deploy     <config.yml> [--only NAME...] [--repo NAME] [--revision SHA] [--identity NAME] [--dry-run]
wharf down       <config.yml> [--only NAME...] [--volumes] [--identity NAME] [--dry-run]
wharf reload     <config.yml> [--only NAME...] [--identity NAME] [--dry-run]
wharf status     <config.yml> [--only NAME...] [--identity NAME]
wharf logs       <config.yml> [SERVICE...] [--only NAME...] [--follow] [--tail N] [--since WHEN] [--identity NAME]
wharf history    <config.yml> [--only NAME...] [--limit N] [--identity NAME]
wharf rollback   <config.yml> [--only NAME...] [--steps N] [--identity NAME] [--dry-run]
wharf ls         <config.yml>
wharf setup      <config.yml> [--only NAME...] [--identity NAME]
wharf rotate     <config.yml> [--only NAME...] [--identity NAME]
wharf identities
wharf --version
```

Every subcommand except `ls` and `identities` also accepts
`--ci`/`--interactive` to override wharf's automatic CI-vs-local
detection (see [`ssh.md`](ssh.md)'s `is_ci`).

`--dry-run` (on `deploy`/`down`/`reload`/`rollback`, added by
`_add_dry_run_flag`) prints each target's `git push` and remote script
instead of running them — see [`operations.md`](operations.md#dry-run);
`rollback`'s variant gets its own help text since it still connects to
read the history.

`rotate` and `identities` follow the same subparser pattern as the
other commands — `rotate` takes the same `<config.yml> [--only]
[--identity]` shape as `setup`; `identities` takes no config file
argument at all, since identity key files live under `.wharf/` at the
project root, not per config file. `--identity` is added to
`deploy`/`down`/`reload`/`setup`/`rotate` via the shared
`_add_identity_flag` helper, the same pattern `_add_ci_flags` already
uses.

## Flow

1. `build_parser()` — the full argparse tree (plus a top-level
   `--version`), shared flags factored into `_add_common` (`config`,
   `--only`, `--repo`), `_add_ci_flags` (`--ci`/`--interactive`,
   mutually exclusive), `_add_identity_flag` (`--identity`), and
   `_add_dry_run_flag` (`--dry-run`). Values with a shape are checked by
   argparse `type=` functions — `_positive_int` (`--steps`, `--limit`),
   `_tail_count` (`--tail`: a count or `all`) and `_compose_service`
   (`logs`' `SERVICE` arguments, via `config.compose_service_name`) — so
   a bad value is a usage error, not a remote failure.
2. `main(argv)` dispatches on `args.command` (via `_main`; `main` itself
   only turns Ctrl-C into a one-line `wharf: interrupted` and exit code
   130 instead of a traceback):
   - `ls` loads the config and prints each target's order, host, and
     `[secrets]`/healthcheck annotations — never touches the network,
     so it skips both the update check and any SSH connection.
   - `identities` calls `identity.list_identities()` directly — no
     config file involved at all, so it also skips the update check.
   - Every other command loads the config (see `_load` below), resolves
     `--repo` and the CI/local auth mode, then:
   - `setup` and `rotate` share one block that calls
     `setup_mod.setup`/`rotate_mod.rotate` directly and catches their
     errors inline — not through `_run_operation`, since neither raises
     the per-target `OperationError` that `operations.py` wraps failures
     in.
   - `deploy`/`down`/`reload` call the matching
     [`operations`](operations.md) function through `_run_operation`.
     `deploy` first infers the revision (`git rev-parse HEAD`) unless
     `--revision` is given; outside a git checkout (or in one with no
     commits) that's a one-line error and exit 2, not a traceback.
   - `status`, `logs`, `history` and `rollback` go through
     `_run_operation` the same way. `logs --follow` is checked up front
     to select exactly one target (following never returns on its own),
     and a Ctrl-C while following is the normal way to stop, so it exits
     0 quietly rather than `wharf: interrupted` / 130.
3. Before dispatching (except for `ls` and `identities`), a best-effort, silent-on-failure
   [`update_check`](update_check.md) runs — skipped in CI and when
   `WHARF_NO_UPDATE_CHECK` is set, so it never adds an unexpected network
   call where one isn't wanted.

## Error handling — `_run_operation` / `_load`

Config errors (`ConfigError`, `OSError`, `UnicodeDecodeError`,
`yaml.YAMLError`) are caught at `_load()` and turned into a one-line
`wharf: <path>: <message>` on stderr with exit code 2 — never a raw
traceback. `_load()` also checks `--only` against the config's targets
right away, so a typo'd target name is reported the same way before
anything runs — in particular before `setup`/`rotate` generate a key.

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
Local failures in those two (`ssh-keygen` missing or failing, an
unreadable key file — `CalledProcessError`/`OSError`) print one line and
exit 1.
