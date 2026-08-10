import pytest

from wharf.config import Target
from wharf.ssh import SessionAuth, build_git_ssh_command, build_ssh_argv

TARGET = Target(
    name="app",
    remote_dir="/opt/deploys/app",
    host="203.0.113.10",
    port=2222,
    user="deploy",
    host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg",
    order=10,
)
AUTH = SessionAuth(batch=False, identity_file=None)


def test_build_ssh_argv_includes_destination():
    argv = build_ssh_argv(TARGET, AUTH)
    assert argv[-1] == "deploy@203.0.113.10"


def test_build_git_ssh_command_excludes_destination():
    # GIT_SSH_COMMAND must be options-only -- git appends its own
    # [-p port] user@host <command> onto it based on the push URL. A
    # baked-in destination here makes ssh see two, and treats the
    # second as a remote command to execute (see the janus-dashboard
    # staging deploy that surfaced this: "bash: line 1:
    # deploy@203.0.113.10: command not found").
    command = build_git_ssh_command(TARGET, AUTH)
    assert "deploy@203.0.113.10" not in command
    assert command.startswith("ssh -p 2222")


def test_resolve_local_default_identity_uses_ambient_agent(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    auth = SessionAuth.resolve(force_ci=False)
    assert auth.batch is False
    assert auth.identity_file is None


def test_resolve_local_named_identity_uses_its_own_key_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_file = tmp_path / ".wharf" / "keys" / "release-bot_key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("fake private key material")

    auth = SessionAuth.resolve(force_ci=False, identity="release-bot")

    assert auth.batch is False
    assert auth.identity_file == key_file


def test_resolve_local_named_identity_without_key_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="release-bot"):
        SessionAuth.resolve(force_ci=False, identity="release-bot")


def test_resolve_ci_ignores_identity_name_reads_same_env_var(monkeypatch):
    monkeypatch.setenv("DEPLOY_SSH_KEY", "fake-private-key-content\n")

    auth_default = SessionAuth.resolve(force_ci=True)
    auth_named = SessionAuth.resolve(force_ci=True, identity="release-bot")

    assert auth_default.batch is True and auth_named.batch is True
    assert auth_default.identity_file is not None
    assert auth_named.identity_file is not None
    assert auth_default.identity_file.read_text() == auth_named.identity_file.read_text()
