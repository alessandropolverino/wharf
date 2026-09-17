import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wharf import ssh
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


def test_build_ssh_argv_ends_option_parsing_before_destination():
    # Belt-and-braces with config validation: after "--", even a user like
    # "-oProxyCommand=..." is a destination, never an ssh option.
    argv = build_ssh_argv(TARGET, AUTH)
    assert argv[-2:] == ["--", "deploy@203.0.113.10"]


def test_build_git_ssh_command_has_no_option_terminator():
    # git appends "-p <port> user@host ..." to GIT_SSH_COMMAND; a "--" here
    # would turn that "-p" into the destination.
    assert "--" not in shlex.split(build_git_ssh_command(TARGET, AUTH))


def _known_hosts_file(argv: list[str]) -> Path:
    option = next(part for part in argv if part.startswith("UserKnownHostsFile="))
    return Path(option.split("=", 1)[1])


def test_pinned_known_hosts_file_is_removed_at_exit(monkeypatch):
    registered = []
    monkeypatch.setattr(ssh.atexit, "register", lambda fn, *args, **kwargs: registered.append((fn, args, kwargs)))

    known_hosts = _known_hosts_file(build_ssh_argv(TARGET, AUTH))

    assert known_hosts.read_text() == f"[203.0.113.10]:2222 {TARGET.host_key}\n"
    for fn, args, kwargs in registered:
        fn(*args, **kwargs)
    assert not known_hosts.exists()


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("203.0.113.10", 22, "203.0.113.10"),
        ("2001:db8::1", 22, "2001:db8::1"),
        ("2001:db8::1", 2222, "[2001:db8::1]:2222"),
    ],
)
def test_known_hosts_line_host_pattern(host, port, expected):
    line = ssh._known_hosts_line(replace(TARGET, host=host, port=port))
    assert line == f"{expected} {TARGET.host_key}\n"


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


def test_run_streaming_capture_returns_stdout_but_keeps_stderr_live(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="line\n")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)

    assert ssh.run_streaming(["x"], description="x", input_text="s", capture=True) == "line\n"
    assert seen["stdout"] is subprocess.PIPE and "stderr" not in seen
    assert ssh.run_streaming(["x"], description="x") is None


def test_capture_remote_script_runs_the_same_remote_command_line(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["input"] = argv, kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout="history\n")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)

    output = ssh.capture_remote_script(TARGET, AUTH, "cat file\n", {"REVISION": "abc"}, description="read")

    assert output == "history\n" and seen["input"] == "cat file\n"
    assert seen["argv"][-5:] == ["deploy@203.0.113.10", "REVISION=abc", "bash", "-l", "-s"]


def test_capture_remote_script_raises_on_failure(monkeypatch):
    monkeypatch.setattr(ssh.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3, stdout=""))
    with pytest.raises(ssh.RemoteCommandError, match=r"read failed \(exit 3\)"):
        ssh.capture_remote_script(TARGET, AUTH, "x", {}, description="read")


class _Stream:
    """A stand-in for sys.stdout/stderr that records when it is flushed."""

    def __init__(self, log, name):
        self.log, self.name = log, name

    def write(self, text):
        pass

    def flush(self):
        self.log.append(f"flush {self.name}")


def test_run_streaming_flushes_python_output_before_the_child_writes(monkeypatch):
    # When stdout is a pipe (CI logs), buffered headers would otherwise land
    # after the subprocess output they introduce.
    log = []
    monkeypatch.setattr(ssh.sys, "stdout", _Stream(log, "stdout"))
    monkeypatch.setattr(ssh.sys, "stderr", _Stream(log, "stderr"))

    def fake_run(argv, **kwargs):
        log.append("child")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)

    ssh.run_streaming(["x"], description="x")

    assert log == ["flush stdout", "flush stderr", "child"]
