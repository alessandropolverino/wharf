# Code reference

One page per file in this folder, living next to the code it documents.
For the config *schema* (what goes in `deploy.yml`), see
[`configuration.md`](../../docs/configuration.md). For the big picture
(what wharf does and how it relates to `docker compose`), see
[`how-it-works.md`](../../docs/how-it-works.md).

| File | What it does |
|---|---|
| [`cli.md`](cli.md) | `argparse` CLI, subcommand dispatch, error → exit code mapping. |
| [`config.md`](config.md) | Parses and validates a config file into `Config`/`Target`/`SecretsDefaults`/`PreUpStep`. |
| [`operations.md`](operations.md) | Orchestrates `deploy`/`down`/`reload` across a config's targets, sequentially. |
| [`remote_script.md`](remote_script.md) | Renders the bash scripts that actually run on each target. |
| [`ssh.md`](ssh.md) | SSH argv construction, host-key pinning, CI vs. local auth, subprocess execution. |
| [`git_ops.md`](git_ops.md) | `git push`es the deploy revision to a target's bare repo — wharf's registry substitute. |
| [`healthcheck.md`](healthcheck.md) | Polls a target's `healthcheck` URL after `deploy`/`reload`. |
| [`setup.md`](setup.md) | `wharf setup`: generates a deploy keypair, bootstraps bare repos and `authorized_keys`. |
| [`update_check.md`](update_check.md) | Best-effort "a newer wharf release exists" notice. |
| [`__init__.md`](__init__.md) | Package version. |
| [`__main__.md`](__main__.md) | `python -m wharf` entry point. |

## Call graph

```
cli.py
 ├─ config.py            (load_config)
 ├─ operations.py         deploy / down / reload
 │   ├─ config.py         (select_targets, compose_file_for, render_repo_template)
 │   ├─ git_ops.py         push_revision  ──┐
 │   ├─ remote_script.py   render_up/down/reload
 │   ├─ ssh.py              SessionAuth, run_remote_script  ◄┘ (both go over SSH)
 │   └─ healthcheck.py      wait_healthy
 ├─ setup.py               setup
 │   └─ ssh.py              SessionAuth, build_ssh_argv, run_streaming
 └─ update_check.py        check_for_update
```

Nothing in this package talks to Docker, Infisical, or a target host
directly except through `ssh.py`'s `run_streaming`/`run_remote_script` —
every remote effect is a bash script rendered by `remote_script.py` (or,
for `setup`, an inline script in `setup.py`) piped over one SSH
connection.
