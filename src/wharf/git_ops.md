# `git_ops.py`

One function: `push_revision(target, remote_repo, branch, revision,
auth)`.

`git push`es `revision` to `remote_repo` on `target`, over
`ssh://user@host:port/path`, using `GIT_SSH_COMMAND` built by
[`ssh.build_git_ssh_command`](ssh.md) (pinned host key, CI/local auth
mode).

This push is what **stands in for a container registry**: after it
succeeds, the target has the exact source tree at `revision` sitting in
its bare repo, ready for [`remote_script.render_up`](remote_script.md)
to check out and build locally. No image is ever pushed or pulled — see
[`how-it-works.md`](../../docs/how-it-works.md) for why this replaces a
registry entirely.
