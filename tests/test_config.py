import pytest

from wharf.config import ConfigError, DEFAULT_BRANCH, DEFAULT_COMPOSE_FILE, load_config

VALID_MINIMAL = """\
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


def test_minimal_config_parses_with_defaults(write_config):
    config = load_config(write_config(VALID_MINIMAL))
    assert config.remote_repo == "/srv/git/{repo}.git"
    assert config.branch == DEFAULT_BRANCH
    assert config.compose_file == DEFAULT_COMPOSE_FILE
    assert config.secrets is None
    assert config.ensure_branch is None
    assert len(config.targets) == 1
    assert config.targets[0].name == "app"
    assert config.targets[0].uses_secrets is False


def test_ensure_branch_parses_when_present(write_config):
    text = VALID_MINIMAL + "ensure_branch: main\n"
    config = load_config(write_config(text))
    assert config.ensure_branch == "main"


def test_unknown_root_key_rejected(write_config):
    text = VALID_MINIMAL + "not_a_real_field: true\n"
    with pytest.raises(ConfigError, match="unexpected"):
        load_config(write_config(text))


def test_version_must_be_1(write_config):
    text = VALID_MINIMAL.replace("version: 1", "version: 2")
    with pytest.raises(ConfigError, match="version must be 1"):
        load_config(write_config(text))


def test_bad_host_key_rejected(write_config):
    text = VALID_MINIMAL.replace(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg",
        "not-a-real-key",
    )
    with pytest.raises(ConfigError, match="ssh-ed25519"):
        load_config(write_config(text))


def test_duplicate_target_names_rejected(write_config):
    text = VALID_MINIMAL + VALID_MINIMAL.split("targets:\n")[1].replace("order: 10", "order: 20")
    with pytest.raises(ConfigError, match="unique"):
        load_config(write_config(text))


def test_target_paths_without_secrets_block_rejected(write_config):
    text = VALID_MINIMAL + '    paths: ["/some/path/"]\n'
    with pytest.raises(ConfigError, match="no top-level secrets block"):
        load_config(write_config(text))


def test_secrets_and_paths_enable_uses_secrets(write_config):
    text = VALID_MINIMAL + (
        "secrets:\n"
        "  provider: infisical\n"
        "  project_id: proj-123\n"
        "  domain: https://eu.infisical.com\n"
        "  environment: prod\n"
    )
    text = text.replace(
        "    order: 10\n",
        '    order: 10\n    paths: ["/svc/"]\n',
    )
    config = load_config(write_config(text))
    assert config.secrets is not None
    assert config.secrets.project_id == "proj-123"
    assert config.targets[0].uses_secrets is True


def test_unsupported_secrets_provider_rejected(write_config):
    text = VALID_MINIMAL + (
        "secrets:\n"
        "  provider: vault\n"
        "  project_id: proj-123\n"
        "  domain: https://eu.infisical.com\n"
        "  environment: prod\n"
    )
    with pytest.raises(ConfigError, match="provider"):
        load_config(write_config(text))


def test_healthcheck_must_be_http_url(write_config):
    text = VALID_MINIMAL.replace("    order: 10\n", "    order: 10\n    healthcheck: not-a-url\n")
    with pytest.raises(ConfigError, match="HTTP"):
        load_config(write_config(text))


def test_per_target_compose_file_override(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", "    order: 10\n    compose_file: docker-compose.custom.yml\n"
    )
    config = load_config(write_config(text))
    assert config.compose_file_for(config.targets[0]) == "docker-compose.custom.yml"


def test_pre_up_defaults_to_none(write_config):
    config = load_config(write_config(VALID_MINIMAL))
    assert config.targets[0].pre_up is None


def test_pre_up_accepts_valid_service_names(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n",
        '    order: 10\n    pre_up: ["migrate-janus", "bootstrap-dashboard-admin"]\n',
    )
    config = load_config(write_config(text))
    assert config.targets[0].pre_up == ("migrate-janus", "bootstrap-dashboard-admin")


def test_pre_up_rejects_leading_dash(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: ["--rm"]\n'
    )
    with pytest.raises(ConfigError, match="compose service name"):
        load_config(write_config(text))


def test_pre_up_rejects_empty_list(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", "    order: 10\n    pre_up: []\n"
    )
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(write_config(text))


def test_pre_up_rejects_shell_metacharacters(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: ["svc; rm -rf /"]\n'
    )
    with pytest.raises(ConfigError, match="compose service name"):
        load_config(write_config(text))
