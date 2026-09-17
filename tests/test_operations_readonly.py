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
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
    healthcheck: https://app.example.com/health
"""
TWO_TARGETS = CONFIG_TEXT + """\
  - name: worker
    remote_dir: /opt/deploys/{repo}/worker
    host: 203.0.113.11
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 20
"""


@pytest.fixture
def remote(monkeypatch):
    """Fake out every remote call and record what each action would have run."""
    calls = {"run": [], "health": []}
    monkeypatch.setattr(
        SessionAuth, "resolve", staticmethod(lambda *, force_ci=None, identity=None: SessionAuth(batch=True)),
    )

    def fake_run(target, auth, script, env_vars, *, description):
        calls["run"].append((target.name, script, dict(env_vars), description))

    monkeypatch.setattr(operations, "run_remote_script", fake_run)
    monkeypatch.setattr(operations, "wait_healthy", lambda url: calls["health"].append(url))
    return calls


def test_status_runs_the_status_script_on_each_target(write_config, remote, capsys):
    config = load_config(write_config(TWO_TARGETS))

    operations.status(config, repo="myapp")

    assert [name for name, *_ in remote["run"]] == ["app", "worker"]
    _, script, env_vars, description = remote["run"][0]
    assert 'docker compose -f "$compose_file" ps' in script
    assert "remote_dir=/opt/deploys/myapp/app" in script
    assert (env_vars, description) == ({}, "status on app")
    assert remote["health"] == []
    out = capsys.readouterr().out
    assert "==> Status of app (203.0.113.10:22)" in out
    assert "==> Status of worker (203.0.113.11:22)" in out


def test_logs_passes_services_and_flags_through(write_config, remote):
    config = load_config(write_config(CONFIG_TEXT))

    operations.logs(config, repo="myapp", services=("api",), follow=True, tail="all", since="1h")

    _, script, _, description = remote["run"][0]
    assert "logs --tail=all --since=1h --follow api </dev/null" in script
    assert description == "logs on app"


def test_logs_follow_refuses_more_than_one_target(write_config, remote):
    config = load_config(write_config(TWO_TARGETS))

    with pytest.raises(ValueError, match="one target at a time"):
        operations.logs(config, repo="myapp", follow=True)

    assert remote["run"] == []
