import os
import shutil
import subprocess

import pytest

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


def _all_scripts(remote_dir: str = "/opt/deploys/app") -> dict[str, str]:
    return {
        "up": render_up(
            remote_repo="/srv/git/app.git", remote_dir=remote_dir,
            compose_file="docker-compose.yml", secrets=None, paths=None,
        ),
        "down": render_down(remote_dir=remote_dir, compose_file="docker-compose.yml", volumes=False),
        "reload": render_reload(remote_dir=remote_dir, compose_file="docker-compose.yml", secrets=None, paths=None),
    }


@pytest.mark.parametrize("action", ["up", "down", "reload"])
def test_every_script_takes_the_deploy_lock_without_waiting(action):
    # Blocking flock (no -n) would queue behind a running -- or hung --
    # deploy instead of aborting as documented.
    script = _all_scripts()[action]
    assert "flock -n -x 200 ||" in script
    assert script.rstrip().endswith(') 200>>"$lock_file"')


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` on PATH that only logs its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_script_aborts_immediately_while_another_run_holds_the_lock(tmp_path, fake_docker):
    import fcntl  # POSIX-only, like flock itself

    remote_dir = tmp_path / "app"
    remote_dir.mkdir()
    script = _all_scripts(str(remote_dir))["down"]

    with open(remote_dir / ".wharf-deploy.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        # timeout: a blocking flock would hang here instead of failing
        result = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, timeout=10)

    assert result.returncode == 1
    assert "another wharf deploy/down/reload holds" in result.stderr
    assert not fake_docker.exists()  # never reached `docker compose down`


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_script_proceeds_when_the_lock_is_free(tmp_path, fake_docker):
    remote_dir = tmp_path / "app"
    script = _all_scripts(str(remote_dir))["down"]

    result = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, timeout=10)

    assert result.returncode == 0, result.stderr
    assert fake_docker.read_text() == "compose -f docker-compose.yml down\n"
