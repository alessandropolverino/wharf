# `operations.py`

Orchestrates the `deploy`, `down`, and `reload` actions across a config's
targets. This is the layer between the CLI ([`cli.md`](cli.md)) and the
per-target mechanics (script rendering in
[`remote_script.md`](remote_script.md), SSH in [`ssh.md`](ssh.md)).

## Sequential rollout

Targets are always processed **sequentially, in `order`** — there is no
parallel rollout strategy (mirrors the original per-repo scripts, which
only ever used a "sequential" strategy). The **first failing target stops
the run**: later targets are left untouched rather than piling more
changes on top of a broken deploy. This is the tool's only rollout
strategy — see [`how-it-works.md`](../../docs/how-it-works.md) for why that's
an intentional simplicity trade-off.

## `deploy(config, *, repo, revision, only=(), force_ci=None)`

Per target, in order:

1. Resolve `SessionAuth` (local vs. CI — see [`ssh.md`](ssh.md)).
2. `push_revision` — `git push` the revision to the target's bare repo
   (see [`git_ops.md`](git_ops.md)).
3. `render_up` the deploy script and run it over SSH.
4. If the target declares `healthcheck`, poll it (see
   [`healthcheck.md`](healthcheck.md)).

Any exception during a target's steps is wrapped in `OperationError`,
which carries the target's name so the CLI can report *which* target
failed.

## `down(config, *, repo, only=(), volumes=False, force_ci=None)`

Same per-target loop, running the `render_down` script (stop, optionally
`--volumes`). No git push, no secrets, no healthcheck.

## `reload(config, *, repo, only=(), force_ci=None)`

Same loop again, running `render_reload` (re-apply compose, no rebuild,
no `pre_up`) — then a healthcheck if configured. No git push: reload acts
on whatever revision is already checked out on the target.

## Guards

- **`_check_branch`** — a no-op unless the config sets `ensure_branch`;
  when set, every action refuses to run unless the *local* checkout
  (where wharf itself is invoked from) is on that branch. Guards against
  e.g. running a prod config from a feature branch by accident. Raises
  `BranchMismatchError`.
- **`infer_repo_name`** — the `{repo}` template value: the `origin`
  remote's URL basename, falling back to the cwd's name. This is what
  lets the *same* config file work identically from a laptop or a CI
  runner — both resolve to the same project name because both operate on
  a checkout of the same repo.
- **`infer_revision`** — `git rev-parse HEAD` of the local checkout; the
  default for `deploy --revision`.
