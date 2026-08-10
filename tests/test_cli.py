from pathlib import Path

from wharf.cli import build_parser


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
