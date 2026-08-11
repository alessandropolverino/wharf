from pathlib import Path

from wharf.cli import build_parser, main
from wharf.identity import generate_keypair, key_comment, key_paths


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
