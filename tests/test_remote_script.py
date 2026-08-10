from wharf.config import PreUpStep, SecretsDefaults
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
    assert 'INFISICAL_TOKEN="$infisical_token"' in script
    assert 'docker compose -f "$compose_file" up -d --build --remove-orphans' in script
    assert "INFISICAL_MACHINE_IDENTITY_ID" in script
    # credentials must never be passed as CLI flags -- argv is readable by
    # any local user via `ps` for the life of the process; env-var prefix
    # only sets the child's environment (see _secrets_login docstring).
    assert "--token" not in script
    assert "--client-id" not in script
    assert "--client-secret" not in script
    assert 'INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$INFISICAL_MACHINE_IDENTITY_ID"' in script
    assert 'INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$INFISICAL_MACHINE_IDENTITY_CLIENT_SECRET"' in script


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


def test_render_up_with_pre_up_runs_before_up_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
        pre_up=(PreUpStep(service="migrate-janus"), PreUpStep(service="bootstrap-dashboard-admin")),
    )
    migrate_index = script.index("run --rm -T --build migrate-janus </dev/null")
    bootstrap_index = script.index("run --rm -T --build bootstrap-dashboard-admin </dev/null")
    up_index = script.index("up -d --build --remove-orphans")
    assert migrate_index < bootstrap_index < up_index


def test_render_up_pre_up_commands_are_shlex_quoted():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
        pre_up=(PreUpStep(service="migrate-janus"),),
    )
    assert 'docker compose -f "$compose_file" run --rm -T --build migrate-janus </dev/null' in script


def test_render_up_pre_up_shell_metacharacters_are_neutralized_by_shlex_quote():
    # migrate-janus alone can't prove shlex.quote is doing anything -- quoting
    # it is a no-op, so the test would pass identically if shlex.quote were
    # deleted from the source. Use a value only quoting (not Task 1's config
    # regex, deliberately bypassed here by calling render_up directly)
    # neutralizes, to prove the render-layer defense works on its own.
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
        pre_up=(PreUpStep(service="a$(id)"),),
    )
    assert "run --rm -T --build 'a$(id)' </dev/null" in script
    assert "run --rm -T --build a$(id)" not in script


def test_render_up_with_pre_up_and_secrets_calls_login_once():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/core/",),
        pre_up=(
            PreUpStep(service="migrate-janus"),
            PreUpStep(service="bootstrap-dashboard-admin"),
            PreUpStep(service="migrate-janusdashboard"),
        ),
    )
    assert script.count("infisical login") == 1
    assert script.count("infisical run --env=prod --path=/core/") == 4  # 3 pre_up + 1 up
    for service in ("migrate-janus", "bootstrap-dashboard-admin", "migrate-janusdashboard"):
        assert f"run --rm -T --build {service} </dev/null" in script


def test_render_up_pre_up_step_with_own_paths_scopes_only_that_command():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/core/",),
        pre_up=(PreUpStep(service="migrate-janus", paths=("/core/migrate/",)),),
    )
    assert script.count("infisical login") == 1  # still just one login for the whole script
    assert "infisical run --env=prod --path=/core/migrate/ --projectId" in script
    assert "infisical run --env=prod --path=/core/ --projectId" in script
    migrate_index = script.index("--path=/core/migrate/")
    up_index = script.index("--path=/core/ --projectId")
    assert migrate_index < up_index


def test_render_up_pre_up_step_without_own_paths_inherits_target_paths():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=("/core/",),
        pre_up=(PreUpStep(service="migrate-janus"),),
    )
    assert script.count("infisical run --env=prod --path=/core/ --projectId") == 2  # pre_up + up


def test_render_up_pre_up_step_paths_without_target_paths_still_wraps_only_that_step():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=SECRETS,
        paths=None,
        pre_up=(PreUpStep(service="migrate-janus", paths=("/core/migrate/",)),),
    )
    assert script.count("infisical login") == 1
    assert "infisical run --env=prod --path=/core/migrate/" in script
    up_line = next(line for line in script.splitlines() if "up -d --build" in line)
    assert "infisical run" not in up_line


def test_render_up_without_pre_up_matches_no_pre_up_behavior():
    script = render_up(
        remote_repo="/srv/git/app.git",
        remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml",
        secrets=None,
        paths=None,
    )
    assert "run --rm" not in script
