import pytest

from wharf.config import ConfigError, load_config, render_repo_template

THREE_TARGETS = """\
version: 1
remote_repo: /srv/git/{repo}.git
targets:
  - name: second
    remote_dir: /opt/deploys/{repo}/second
    host: 203.0.113.10
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 20
  - name: first
    remote_dir: /opt/deploys/{repo}/first
    host: 203.0.113.11
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 10
  - name: third
    remote_dir: /opt/deploys/{repo}/third
    host: 203.0.113.12
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 30
"""


def test_targets_are_sorted_by_order_regardless_of_file_order(write_config):
    config = load_config(write_config(THREE_TARGETS))
    assert [t.name for t in config.targets] == ["first", "second", "third"]


def test_select_targets_defaults_to_all_in_order(write_config):
    config = load_config(write_config(THREE_TARGETS))
    assert [t.name for t in config.select_targets()] == ["first", "second", "third"]


def test_select_targets_filters_and_preserves_order(write_config):
    config = load_config(write_config(THREE_TARGETS))
    selected = config.select_targets(("third", "first"))
    assert [t.name for t in selected] == ["first", "third"]


def test_select_targets_rejects_unknown_name(write_config):
    config = load_config(write_config(THREE_TARGETS))
    with pytest.raises(ConfigError, match="unknown target"):
        config.select_targets(("nonexistent",))


def test_render_repo_template_substitutes_repo_only():
    assert render_repo_template("/srv/git/{repo}.git", "wharf") == "/srv/git/wharf.git"
    assert render_repo_template("/opt/deploys/{repo}/app", "my-app") == "/opt/deploys/my-app/app"
