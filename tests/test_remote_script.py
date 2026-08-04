from wharf.config import SecretsDefaults
from wharf.remote_script import render_down, render_reload, render_up

SECRETS = SecretsDefaults(
    provider="infisical",
    project_id="proj-123",
    domain="https://eu.infisical.com",
    environment="prod",
)


def test_render_up_without_secrets_has_no_infisical():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" up -d --build --remove-orphans' in script


def test_render_up_with_secrets_wraps_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/app/",),
    )
    assert script.count("infisical login") == 1
    assert "infisical run --env=prod --path=/app/" in script
    assert '--token "$infisical_token"' in script
    assert 'docker compose -f "$compose_file" up -d --build --remove-orphans' in script
    assert "INFISICAL_MACHINE_IDENTITY_ID" in script


def test_render_up_checkout_happens_before_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    checkout_index = script.index('checkout -f "$REVISION"')
    up_index = script.index("up -d --build --remove-orphans")
    assert checkout_index < up_index


def test_render_reload_without_secrets_has_no_infisical():
    script = render_reload(
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" up -d --remove-orphans' in script
    assert "--build" not in script


def test_render_reload_with_secrets_wraps_up_command():
    script = render_reload(
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/app/",),
    )
    assert script.count("infisical login") == 1
    assert "infisical run --env=prod --path=/app/" in script
    assert 'docker compose -f "$compose_file" up -d --remove-orphans' in script


def test_render_down_has_no_secrets_wrapping():
    script = render_down(remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", volumes=False)
    assert "infisical" not in script
    assert 'docker compose -f "$compose_file" down' in script


def test_render_down_with_volumes_adds_flag():
    script = render_down(remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", volumes=True)
    assert 'docker compose -f "$compose_file" down --volumes' in script
