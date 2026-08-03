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
