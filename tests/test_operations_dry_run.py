import pytest

from wharf import operations
from wharf.config import load_config
from wharf.ssh import SessionAuth

CONFIG_TEXT = """\
version: 1
remote_repo: /srv/git/{repo}.git
targets:
  - name: app
    remote_dir: /opt/deploys/{repo}/app
    host: 2001:db8::10
    port: 2222
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
    healthcheck: https://app.example.com/health
"""


@pytest.fixture
def config(write_config, monkeypatch):
    """A config whose operations fail loudly if they touch auth or SSH."""

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not resolve credentials or connect")

    monkeypatch.setattr(SessionAuth, "resolve", staticmethod(forbidden))
    monkeypatch.setattr(operations, "push_revision", forbidden)
    monkeypatch.setattr(operations, "run_remote_script", forbidden)
    monkeypatch.setattr(operations, "wait_healthy", forbidden)
    return load_config(write_config(CONFIG_TEXT))


def test_deploy_dry_run_shows_push_script_and_healthcheck(config, capsys):
    operations.deploy(config, repo="myapp", revision="abc123", dry_run=True)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "==> [dry run] Deploying app ([2001:db8::10]:2222)"
    assert lines[1] == "Would run locally: git push ssh://deploy@[2001:db8::10]:2222/srv/git/myapp.git +abc123:refs/heads/main"
    assert lines[2] == "Would run on deploy@2001:db8::10 (port 2222): REVISION=abc123 bash -l -s <<'WHARF_SCRIPT'"
    assert lines[3] == "set -euo pipefail"
    assert 'checkout -f "$REVISION"' in out
    assert lines[-2:] == ["WHARF_SCRIPT", "Would then poll https://app.example.com/health until it responds"]


def test_deploy_rejects_a_revision_that_is_really_a_git_option(config):
    # A forged --revision like --upload-pack=... would otherwise reach the
    # remote `git checkout -f "$REVISION"` as an *option*, not a revision --
    # the same class of bug the deploy history's own hex check guards against.
    with pytest.raises(operations.InvalidRevisionError):
        operations.deploy(config, repo="myapp", revision="--upload-pack=/tmp/evil", dry_run=True)


def test_down_dry_run_shows_script(config, capsys):
    operations.down(config, repo="myapp", volumes=True, dry_run=True)

    out = capsys.readouterr().out
    assert out.startswith("==> [dry run] Stopping app ([2001:db8::10]:2222)\n")
    assert "Would run on deploy@2001:db8::10 (port 2222): bash -l -s <<'WHARF_SCRIPT'\n" in out
    assert 'docker compose -f "$compose_file" down --volumes\n' in out
    assert out.endswith("WHARF_SCRIPT\n")


def test_reload_dry_run_shows_script_and_healthcheck(config, capsys):
    operations.reload(config, repo="myapp", dry_run=True)

    out = capsys.readouterr().out
    assert out.startswith("==> [dry run] Reloading app ([2001:db8::10]:2222)\n")
    assert 'docker compose -f "$compose_file" up -d --remove-orphans\n' in out
    assert out.endswith("WHARF_SCRIPT\nWould then poll https://app.example.com/health until it responds\n")


def test_dry_run_still_enforces_ensure_branch(write_config, monkeypatch):
    # The dry run should fail wherever the real run would, before printing a plan.
    config = load_config(write_config(CONFIG_TEXT + "ensure_branch: main\n"))
    monkeypatch.setattr(operations, "infer_current_branch", lambda cwd=None: "feature-x")

    with pytest.raises(operations.BranchMismatchError):
        operations.deploy(config, repo="myapp", revision="abc123", dry_run=True)
