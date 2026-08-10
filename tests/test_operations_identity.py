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
"""


class _StopEarly(Exception):
    pass


def _capture_identity(monkeypatch, seen):
    def fake_resolve(*, force_ci=None, identity=None):
        seen["identity"] = identity
        raise _StopEarly

    monkeypatch.setattr(SessionAuth, "resolve", staticmethod(fake_resolve))


def test_deploy_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.deploy(config, repo="app", revision="abc123", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_down_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.down(config, repo="app", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_reload_passes_identity_to_session_auth(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.reload(config, repo="app", identity="release-bot")

    assert seen["identity"] == "release-bot"


def test_deploy_defaults_identity_to_none(write_config, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}
    _capture_identity(monkeypatch, seen)

    with pytest.raises(operations.OperationError):
        operations.deploy(config, repo="app", revision="abc123")

    assert seen["identity"] is None
