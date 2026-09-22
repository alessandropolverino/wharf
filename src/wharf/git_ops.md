# `git_ops.py`

`push_revision(target, remote_repo, branch, revision, auth)` `git
push`es `revision` to `remote_repo` on `target`, using `GIT_SSH_COMMAND`
built by [`ssh.build_git_ssh_command`](ssh.md) (pinned host key,
CI/local auth mode).

Its two building blocks are public because
[`operations`](operations.md)' dry run prints them:

- **`push_url(target, remote_repo)`** — `ssh://user@host:port/path`,
  built from [`Target.address`](config.md), which brackets an IPv6 host
  (`ssh://deploy@[2001:db8::1]:22/...`). Unbracketed, git can't tell
  where the address ends and the port begins, and hands ssh a mangled
  destination with no `-p`.
- **`push_refspec(revision, branch)`** — `+<revision>:refs/heads/<branch>`.
  Forced on purpose: that branch only ever mirrors "the revision last
  pushed for deployment", nobody fast-forwards along it, and without the
  `+` a `deploy --revision <older sha>` is rejected as a non-fast-forward
  update — precisely when it's wanted.

This push is what **stands in for a container registry**: after it
succeeds, the target has the exact source tree at `revision` sitting in
its bare repo, ready for [`remote_script.render_up`](remote_script.md)
to check out and build locally. No image is ever pushed or pulled — see
[`how-it-works.md`](../../docs/how-it-works.md) for why this replaces a
registry entirely.
