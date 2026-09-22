# `operations.py`

Orchestrates the `deploy`, `down`, `reload` and `rollback` actions, and
the read-only `status`, `logs` and `history`, across a config's
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

## `deploy(config, *, repo, revision, only=(), force_ci=None, identity=None, dry_run=False)`

`revision` is checked up front — `_check_revision` requires a plain hex
object name, raising `InvalidRevisionError` otherwise, before it can
reach the remote `git checkout -f "$REVISION"` that would otherwise read
a dash-prefixed value (`--upload-pack=...`) as a git *option*, not a
revision. This is the same rule `render_history` already applies to
revisions it reads back for `rollback` (see
[`remote_script.md`](remote_script.md#the-read-only-scripts)), just
applied to the one path that skipped it.

Per target, in order:

1. `render_up` the deploy script.
2. Resolve `SessionAuth` (local vs. CI — see [`ssh.md`](ssh.md)), once
   for the whole run rather than per target (see "Shared helpers"
   below).
3. `push_revision` — `git push` the revision to the target's bare repo
   (see [`git_ops.md`](git_ops.md)).
4. Run the deploy script over SSH.
5. If the target declares `healthcheck`, poll it (see
   [`healthcheck.md`](healthcheck.md)).

Any exception during a target's steps is wrapped in `OperationError`,
which carries the target's name so the CLI can report *which* target
failed.

## `down(config, *, repo, only=(), volumes=False, force_ci=None, identity=None, dry_run=False)`

Same per-target loop, running the `render_down` script (stop, optionally
`--volumes`). No git push, no secrets, no healthcheck.

## `reload(config, *, repo, only=(), force_ci=None, identity=None, dry_run=False)`

Same loop again, running `render_reload` (re-apply compose, no rebuild,
no `pre_up`) — then a healthcheck if configured. No git push: reload acts
on whatever revision is already checked out on the target.

## Dry run

With `dry_run=True` (the CLI's `--dry-run`), each action still runs its
local guards (`_check_branch`, `--only` selection) and renders every
target's script, but prints instead of executing:

```
==> [dry run] Deploying app (203.0.113.10:22)
Would run locally: git push ssh://deploy@203.0.113.10:22/srv/git/myapp.git <sha>:refs/heads/main
Would run on deploy@203.0.113.10:22: REVISION=<sha> bash -l -s <<'WHARF_SCRIPT'
set -euo pipefail
...
WHARF_SCRIPT
Would then poll https://app.example.com/health until it responds
```

`SessionAuth.resolve` is never called, so a dry run needs no
credentials (no `DEPLOY_SSH_KEY`, no local identity key) and makes no
network connection. The push URL/refspec and the remote command line
come from the same helpers the real run uses (`git_ops.push_url`/
`push_refspec`, `ssh.remote_command`), so the preview can't drift from
what actually runs.

`rollback --dry-run` is the one exception: it can't know what it would
deploy without reading each target's history, so it does connect,
read-only, and needs the same credentials a real run would.

## Read-only actions: `status`, `logs`, `history`

Same per-target loop and `SessionAuth` resolution, but **no**
`ensure_branch` guard: that guard exists to stop a target from being
*changed* by accident, and none of these three ever change one — see
[`remote_script.md`](remote_script.md#the-read-only-scripts):

- **`status`** streams `render_status`'s report: the bare repo's `HEAD`
  (what the last checkout left), the last history entry, whether the
  deploy lock is held, and `docker compose ps`.
- **`logs`** streams `render_logs` — `docker compose logs` with
  `--tail`/`--since`/`--follow` and any service names passed through.
  Following never returns on its own, so it's only allowed for a single
  target: the CLI checks that before calling, and the function raises
  `ValueError` otherwise.
- **`history`** is the one action that *captures* output: it runs
  `render_history` through `ssh.capture_remote_script`, parses the lines
  with `parse_history`, and prints them newest first with the current
  one marked.

## `rollback(config, *, repo, only=(), steps=1, force_ci=None, identity=None, dry_run=False)`

Per target: read the history (as `history` does), pick the revision with
`rollback_target`, then run the **same deploy script** for it with
`kind="rollback"` — `pre_up`, lock, image pruning, healthcheck, all as a
deploy — which records itself in the history as a `rollback`. Nothing is
pushed: the revision is already in the target's bare repo (it was
deployed from there), which is what lets a rollback run from a CI
runner or a fresh clone that doesn't have the commit locally.

`rollback` doesn't read the whole history to do this: `render_history`
looks up each line's commit subject with its own remote `git log` call,
so reading everything just to resolve one rollback would cost one
remote process per deploy the target has ever had. `_history_for_rollback`
starts from a bounded tail (enough for the common case of a target that
isn't repeatedly redeploying the same revision) and only grows it -- by
re-reading a larger tail, not appending -- if that didn't turn up
`steps + 1` distinct revisions, stopping once it has, or once a tail
that large turns out to be the whole history anyway.

`rollback_target(records, steps)` walks the history newest-first,
counting each revision once (at its most recent deploy), and returns the
`(current, previous)` pair `steps` apart — so a rollback never lands on
the current revision, and after `A, B, A` the previous revision is `B`
(history is chronological, not a stack). It raises `RuntimeError` with
the reason when the history is empty (pointing at `deploy --revision`)
or too short, which the loop wraps in `OperationError` like any other
target failure.

`parse_history` only accepts lines shaped like history entries
(`<timestamp> <40–64 hex> <deploy|rollback>[<tab><subject>]`): the script
runs in a login shell, so anything a profile script prints is skipped
rather than mistaken for a deploy. That is a parsing guard, not a trust
boundary — the integrity of the record itself comes from where it lives
and who can write it, which
[`remote_script.md`](remote_script.md#history_pathremote_repo-target_name--and-why-it-isnt-in-remote_dir)
covers. If the target's history fails that check, `history` and
`rollback` fail with the reason rather than acting on it.

## Shared helpers

Every action (`deploy`, `down`, `reload`, `status`, `logs`, `history`,
`rollback`) is a thin per-action closure handed to `_for_each_target`,
which does the actual iteration and wraps whatever the closure raises in
`OperationError` — one shared loop instead of seven copies of it.

`_cached_auth(force_ci, identity)` returns a `get_auth()` closure that
resolves `SessionAuth` on its first call and returns that same value on
every later one — session auth doesn't vary per target, and in CI mode
each resolve writes the deploy key out to a fresh temp file, so
resolving it once per target in a multi-target run was pure waste.
Resolving lazily (on first use inside the per-target closure, not up
front) rather than once before the loop starts means a resolve failure
still comes back as `OperationError` for the first target, exactly as if
it had been resolved there directly — and, for `deploy`/`down`/`reload`,
means a dry run — which never calls `get_auth()` at all — still needs no
credentials.

## Guards

- **`_check_branch`** — a no-op unless the config sets `ensure_branch`;
  when set, every action refuses to run unless the *local* checkout
  (where wharf itself is invoked from) is on that branch. Guards against
  e.g. running a prod config from a feature branch by accident. Raises
  `BranchMismatchError`.
- **`infer_repo_name`** — the `{repo}` template value: the `origin`
  remote's URL basename (the last `/`- or `:`-separated component, so
  scp-style `git@host:name.git` works too), falling back to the cwd's
  name when there's no `origin` or no `git` at all. This is what
  lets the *same* config file work identically from a laptop or a CI
  runner — both resolve to the same project name because both operate on
  a checkout of the same repo.
- **`infer_revision`** — `git rev-parse HEAD` of the local checkout; the
  default for `deploy --revision`.
