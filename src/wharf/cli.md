# `cli.py`

wharf's `argparse`-based command-line interface — the entry point wired
up as the `wharf` console script (see [`__main__.md`](__main__.md) for
the `python -m wharf` equivalent).

```
wharf deploy <config.yml> [--only NAME...] [--repo NAME] [--revision SHA]
wharf down   <config.yml> [--only NAME...] [--volumes]
wharf reload <config.yml> [--only NAME...]
wharf ls     <config.yml>
wharf setup  <config.yml> [--only NAME...]
```

Every subcommand except `ls` also accepts `--ci`/`--interactive` to
override wharf's automatic CI-vs-local detection (see
[`ssh.md`](ssh.md)'s `is_ci`).

## Flow

1. `build_parser()` — the full argparse tree, shared flags factored into
   `_add_common` (`config`, `--only`, `--repo`) and `_add_ci_flags`
   (`--ci`/`--interactive`, mutually exclusive).
2. `main(argv)` dispatches on `args.command`:
   - `ls` loads the config and prints each target's order, host, and
     `[secrets]`/healthcheck annotations — never touches the network,
     so it skips both the update check and any SSH connection.
   - Every other command loads the config, resolves `--repo` (via
     `operations.infer_repo_name()` if unset), resolves the CI/local
     auth mode from `--ci`/`--interactive`, then calls the matching
     [`operations`](operations.md) function through `_run_operation`.
3. Before dispatching (except for `ls`), a best-effort, silent-on-failure
   [`update_check`](update_check.md) runs — skipped in CI and when
   `WHARF_NO_UPDATE_CHECK` is set, so it never adds an unexpected network
   call where one isn't wanted.

## Error handling — `_run_operation` / `_load`

Config errors (`ConfigError`, `OSError`, `yaml.YAMLError`) are caught at
`_load()` and turned into a one-line `wharf: <path>: <message>` on
stderr with exit code 2 — never a raw traceback.

`_run_operation` catches the two ways an `operations` call can fail:

- **`BranchMismatchError`** (config's `ensure_branch` doesn't match the
  local checkout) → exit 2.
- **`OperationError`** (a target failed) → its message is printed, and
  if the underlying cause was a `RemoteCommandError`, the **remote
  command's own exit code** is propagated (so a failed `docker compose
  build` on the target surfaces the same exit code locally) — otherwise
  exit 1.
