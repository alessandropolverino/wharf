from dataclasses import replace

from wharf import git_ops
from wharf.config import Target
from wharf.git_ops import push_revision, push_url
from wharf.ssh import SessionAuth

TARGET = Target(
    name="app",
    remote_dir="/opt/deploys/app",
    host="203.0.113.10",
    port=2222,
    user="deploy",
    host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg",
    order=10,
)


def test_push_url():
    assert push_url(TARGET, "/srv/git/app.git") == "ssh://deploy@203.0.113.10:2222/srv/git/app.git"


def test_push_url_brackets_ipv6_host():
    # Unbracketed, git hands ssh "deploy@2001:db8::1:2222" with no -p.
    target = replace(TARGET, host="2001:db8::1")
    assert push_url(target, "/srv/git/app.git") == "ssh://deploy@[2001:db8::1]:2222/srv/git/app.git"


def test_push_revision_pushes_revision_to_branch(monkeypatch):
    seen = {}

    def fake_run_streaming(argv, *, description, env=None, input_text=None):
        seen.update(argv=argv, description=description, env=env)

    monkeypatch.setattr(git_ops, "run_streaming", fake_run_streaming)
    push_revision(TARGET, "/srv/git/app.git", "main", "abc123", SessionAuth(batch=True))

    assert seen["argv"] == ["git", "push", "ssh://deploy@203.0.113.10:2222/srv/git/app.git", "abc123:refs/heads/main"]
    assert seen["description"] == "git push to app"
    assert seen["env"]["GIT_SSH_COMMAND"].startswith("ssh -p 2222 ")
    assert "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]
