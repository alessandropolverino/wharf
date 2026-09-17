import json

import pytest

from wharf.config import ConfigError, DEFAULT_BRANCH, DEFAULT_COMPOSE_FILE, PreUpStep, load_config

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


def test_secrets_domain_rejects_http(write_config):
    text = VALID_MINIMAL + (
        "secrets:\n"
        "  provider: infisical\n"
        "  project_id: proj-123\n"
        "  domain: http://eu.infisical.com\n"
        "  environment: prod\n"
    )
    with pytest.raises(ConfigError, match="HTTPS"):
        load_config(write_config(text))


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
    assert config.targets[0].pre_up == (
        PreUpStep(service="migrate-janus"),
        PreUpStep(service="bootstrap-dashboard-admin"),
    )


def test_pre_up_step_mapping_form_accepts_own_paths(write_config):
    text = VALID_MINIMAL + (
        "secrets:\n"
        "  provider: infisical\n"
        "  project_id: proj-123\n"
        "  domain: https://eu.infisical.com\n"
        "  environment: prod\n"
    )
    text = text.replace(
        "    order: 10\n",
        '    order: 10\n    paths: ["/svc/"]\n'
        '    pre_up: ["migrate-janus", {service: bootstrap, paths: ["/svc/bootstrap/"]}]\n',
    )
    config = load_config(write_config(text))
    assert config.targets[0].pre_up == (
        PreUpStep(service="migrate-janus"),
        PreUpStep(service="bootstrap", paths=("/svc/bootstrap/",)),
    )
    assert config.targets[0].uses_secrets is True


def test_pre_up_step_mapping_form_requires_service_key(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: [{paths: ["/svc/"]}]\n'
    )
    with pytest.raises(ConfigError, match="missing service"):
        load_config(write_config(text))


def test_pre_up_step_paths_without_secrets_block_rejected(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n",
        '    order: 10\n    pre_up: [{service: migrate, paths: ["/svc/"]}]\n',
    )
    with pytest.raises(ConfigError, match="pre_up\\[0\\] declares paths but no top-level secrets block"):
        load_config(write_config(text))


def test_pre_up_step_without_paths_uses_secrets_purely_from_step(write_config):
    # A target with no target-level `paths` but a pre_up step that declares
    # its own is still "uses_secrets" -- that step alone injects secrets.
    text = VALID_MINIMAL + (
        "secrets:\n"
        "  provider: infisical\n"
        "  project_id: proj-123\n"
        "  domain: https://eu.infisical.com\n"
        "  environment: prod\n"
    )
    text = text.replace(
        "    order: 10\n",
        '    order: 10\n    pre_up: [{service: migrate, paths: ["/svc/"]}]\n',
    )
    config = load_config(write_config(text))
    assert config.targets[0].paths is None
    assert config.targets[0].uses_secrets is True


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


def test_pre_up_rejects_trailing_newline(write_config):
    text = VALID_MINIMAL.replace(
        "    order: 10\n", '    order: 10\n    pre_up: ["migrate\\n"]\n'
    )
    with pytest.raises(ConfigError, match="compose service name"):
        load_config(write_config(text))


def test_duplicate_target_names_error_names_the_duplicate(write_config):
    text = VALID_MINIMAL + VALID_MINIMAL.split("targets:\n")[1].replace("order: 10", "order: 20")
    with pytest.raises(ConfigError, match=r"duplicated: app\)"):
        load_config(write_config(text))


def test_duplicate_target_orders_error_names_the_duplicate(write_config):
    text = VALID_MINIMAL + VALID_MINIMAL.split("targets:\n")[1].replace("name: app", "name: other")
    with pytest.raises(ConfigError, match=r"order values must be unique \(duplicated: 10\)"):
        load_config(write_config(text))


@pytest.mark.parametrize(
    ("original", "replacement", "label"),
    [
        ("remote_repo: /srv/git/{repo}.git", "remote_repo: srv/git/{repo}.git", "remote_repo"),
        ("remote_repo: /srv/git/{repo}.git", "remote_repo: '{repo}.git'", "remote_repo"),
        ("remote_dir: /opt/deploys/{repo}/app", "remote_dir: deploys/{repo}/app", r"targets\[0\].remote_dir"),
    ],
)
def test_remote_paths_must_be_absolute(write_config, original, replacement, label):
    # A relative remote_repo can't form a valid ssh:// push URL
    # ("ssh://deploy@host:22srv/git/..." -- git reads "22srv" as part of the host).
    text = VALID_MINIMAL.replace(original, replacement)
    with pytest.raises(ConfigError, match=rf"{label} must be an absolute path"):
        load_config(write_config(text))


def test_tilde_remote_path_rejected_with_hint(write_config):
    # Remote paths are shell-quoted wherever they're used, so a leading ~ is
    # never expanded -- `mkdir -p '~/app'` would create a directory named "~".
    text = VALID_MINIMAL.replace("remote_dir: /opt/deploys/{repo}/app", "remote_dir: ~/deploys/{repo}/app")
    with pytest.raises(ConfigError, match="'~' is not expanded"):
        load_config(write_config(text))


@pytest.mark.parametrize("user", ["deploy", "deploy_user", "ci.bot", "jdoe@corp.example.com", "_svc", "u-1"])
def test_valid_ssh_users_accepted(write_config, user):
    config = load_config(write_config(VALID_MINIMAL.replace("user: deploy", f"user: '{user}'")))
    assert config.targets[0].user == user


@pytest.mark.parametrize("user", ["-oProxyCommand=touch /tmp/x", "-l", "de ploy", ".hidden", "a:b", "x\ny"])
def test_unsafe_ssh_users_rejected(write_config, user):
    # A leading "-" would make `ssh` parse the user@host argument as an
    # option -- -oProxyCommand=... runs a local command.
    text = VALID_MINIMAL.replace("user: deploy", f"user: {json.dumps(user)}")
    with pytest.raises(ConfigError, match=r"targets\[0\].user must be a valid SSH user name"):
        load_config(write_config(text))


@pytest.mark.parametrize(
    "host",
    ["203.0.113.10", "app.example.com", "prod-box", "2001:db8::1", "::1", "::ffff:192.0.2.1"],
)
def test_valid_hosts_accepted(write_config, host):
    config = load_config(write_config(VALID_MINIMAL.replace("host: 203.0.113.10", f"host: '{host}'")))
    assert config.targets[0].host == host


@pytest.mark.parametrize(
    "host",
    ["-oProxyCommand=x", "[2001:db8::1]", "2001:db8::zz", "host name", "app.example.com:22", "user@host", ".example.com"],
)
def test_malformed_hosts_rejected(write_config, host):
    text = VALID_MINIMAL.replace("host: 203.0.113.10", f"host: '{host}'")
    with pytest.raises(ConfigError, match=r"targets\[0\].host must be a hostname"):
        load_config(write_config(text))


@pytest.mark.parametrize(
    ("host", "port", "address"),
    [("203.0.113.10", 22, "203.0.113.10:22"), ("app.example.com", 2222, "app.example.com:2222"), ("2001:db8::1", 22, "[2001:db8::1]:22")],
)
def test_target_address_brackets_ipv6(write_config, host, port, address):
    text = VALID_MINIMAL.replace("host: 203.0.113.10", f"host: '{host}'").replace("port: 22", f"port: {port}")
    assert load_config(write_config(text)).targets[0].address == address


def test_ipv6_zone_id_rejected(write_config):
    # IPv6Address accepts "fe80::1%eth0", but "%" would need escaping in the push URL.
    text = VALID_MINIMAL.replace("host: 203.0.113.10", "host: 'fe80::1%eth0'")
    with pytest.raises(ConfigError, match=r"targets\[0\].host must be a hostname"):
        load_config(write_config(text))
