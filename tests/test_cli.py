from pathlib import Path

import pytest

from wharf import operations, setup as setup_mod
from wharf.cli import build_parser, main
from wharf.identity import generate_keypair, key_comment, key_paths

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


def test_identities_reports_missing_public_key_instead_of_crashing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    private, public = key_paths("release-bot", tmp_path / ".wharf")
    generate_keypair(private, key_comment("release-bot"))
    public.unlink()

    exit_code = main(["identities"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "(no public key)" in out
    assert "Traceback" not in out


def test_setup_with_invalid_identity_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)

    exit_code = main(["setup", "deploy.yml", "--identity", "Bad_Name"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "Bad_Name" in err
    assert "Traceback" not in err


def test_setup_accepts_identity_and_ci_flags():
    args = build_parser().parse_args(["setup", "deploy.yml", "--identity", "ci", "--ci"])
    assert args.identity == "ci"
    assert args.ci is True


def test_setup_identity_defaults_to_none():
    args = build_parser().parse_args(["setup", "deploy.yml"])
    assert args.identity is None


def test_rotate_parses_like_setup():
    args = build_parser().parse_args(["rotate", "deploy.yml", "--identity", "release-bot", "--only", "app"])
    assert args.command == "rotate"
    assert args.config == Path("deploy.yml")
    assert args.identity == "release-bot"
    assert args.only == ["app"]


def test_identities_takes_no_config_argument():
    args = build_parser().parse_args(["identities"])
    assert args.command == "identities"


def test_deploy_down_reload_all_accept_identity():
    for command in ("deploy", "down", "reload"):
        args = build_parser().parse_args([command, "deploy.yml", "--identity", "ci"])
        assert args.identity == "ci"


@pytest.fixture
def offline(monkeypatch):
    """No update check, and local (non-CI) mode unless a test says otherwise."""
    monkeypatch.setenv("WHARF_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("CI", raising=False)


@pytest.mark.parametrize("command", ["deploy", "down", "reload", "setup", "rotate"])
def test_unknown_only_target_is_a_clean_config_error(tmp_path, monkeypatch, capsys, offline, write_config, command):
    monkeypatch.chdir(tmp_path)
    config = write_config(CONFIG_TEXT)

    with pytest.raises(SystemExit) as excinfo:
        main([command, str(config), "--only", "typo"])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--only references unknown target(s): typo" in err
    assert "Traceback" not in err
    assert not (tmp_path / ".wharf").exists()  # setup/rotate generated no key


def test_non_utf8_config_is_a_clean_config_error(tmp_path, capsys, offline):
    config = tmp_path / "deploy.yml"
    config.write_bytes(b"\xff\xfe\x00not yaml")

    with pytest.raises(SystemExit) as excinfo:
        main(["ls", str(config)])

    assert excinfo.value.code == 2
    assert "codec can't decode" in capsys.readouterr().err


def test_deploy_outside_a_git_checkout_exits_cleanly(tmp_path, monkeypatch, capsys, offline, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    config = write_config(CONFIG_TEXT)

    exit_code = main(["deploy", str(config)])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "could not determine the revision to deploy" in err
    assert "--revision" in err


def test_setup_local_failure_exits_cleanly(tmp_path, monkeypatch, capsys, offline, write_config):
    config = write_config(CONFIG_TEXT)

    def missing_ssh_keygen(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ssh-keygen")

    monkeypatch.setattr(setup_mod, "setup", missing_ssh_keygen)

    exit_code = main(["setup", str(config), "--repo", "app"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "ssh-keygen" in err
    assert "Traceback" not in err


def test_ctrl_c_exits_130_without_a_traceback(monkeypatch, capsys, offline, write_config):
    config = write_config(CONFIG_TEXT)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(operations, "down", interrupted)

    assert main(["down", str(config), "--repo", "app"]) == 130
    assert "wharf: interrupted" in capsys.readouterr().err
