import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from wharf.config import PreUpStep, SecretsDefaults
from wharf.remote_script import render_down, render_history, render_logs, render_reload, render_status, render_up

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


# --- history recording, and the read-only status/logs/history scripts ---

def _up(**overrides):
    params = dict(
        remote_repo="/srv/git/app.git", remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml", secrets=None, paths=None,
    )
    params.update(overrides)
    return render_up(**params)


def test_render_up_records_the_resolved_revision_once_services_are_up():
    script = _up()
    assert 'history_file="$remote_dir/.wharf-history"' in script
    resolve_index = script.index('deployed_revision=$(git --git-dir="$remote_repo" rev-parse HEAD)')
    up_index = script.index("up -d --build --remove-orphans")
    record_index = script.index('"$deployed_revision" deploy >> "$history_file"')
    assert resolve_index < up_index < record_index


def test_render_up_kind_rollback_is_recorded_as_such():
    assert '"$deployed_revision" rollback >> "$history_file"' in _up(kind="rollback")


def test_render_status_only_reads():
    script = render_status(
        remote_repo="/srv/git/app.git", remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml", secrets=None, paths=None,
    )
    assert "mkdir" not in script and ">>" not in script
    assert "flock -n 200" in script and '200<"$lock_file"' in script  # a read-only descriptor
    assert 'docker compose -f "$compose_file" ps' in script
    assert "infisical" not in script


def test_render_status_wraps_ps_with_secrets_when_target_has_paths():
    script = render_status(
        remote_repo="/srv/git/app.git", remote_dir="/opt/deploys/app",
        compose_file="docker-compose.yml", secrets=SECRETS, paths=("/app/",),
    )
    assert script.count("infisical login") == 1
    assert (
        "infisical run --env=prod --path=/app/ --projectId=proj-123 --domain=https://eu.infisical.com "
        '-- docker compose -f "$compose_file" ps'
    ) in script


def test_render_logs_passes_flags_and_quoted_services():
    script = render_logs(
        remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", secrets=None, paths=None,
        services=("api", "a$(id)"), follow=True, tail="all", since="30m",
    )
    assert (
        'docker compose -f "$compose_file" logs --tail=all --since=30m --follow api \'a$(id)\' </dev/null'
    ) in script


def test_render_logs_defaults_to_the_last_100_lines_of_every_service():
    script = render_logs(remote_dir="/opt/deploys/app", compose_file="docker-compose.yml", secrets=None, paths=None)
    assert 'docker compose -f "$compose_file" logs --tail=100 </dev/null' in script


def test_render_history_limit_keeps_the_newest_entries():
    assert 'tail -n 5 "$history_file"' in render_history(
        remote_repo="/srv/git/app.git", remote_dir="/opt/deploys/app", limit=5
    )
    assert 'cat "$history_file"' in render_history(remote_repo="/srv/git/app.git", remote_dir="/opt/deploys/app")


@pytest.fixture
def bare_repo(tmp_path):
    """A bare repo holding two commits (v1, v2), like a target's remote_repo after two pushes."""
    src = tmp_path / "src"
    src.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (src / "docker-compose.yml").write_text("services: {}\n")
    shas = []
    for version in ("v1", "v2"):
        (src / "app.txt").write_text(version + "\n")
        git("add", ".")
        git("commit", "-q", "-m", version)
        shas.append(git("rev-parse", "HEAD"))
    bare = tmp_path / "app.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git("push", "-q", str(bare), "HEAD:refs/heads/main")
    return bare, shas


def _run_up(bare: Path, remote_dir: Path, revision: str, **overrides) -> None:
    params = dict(
        remote_repo=str(bare), remote_dir=str(remote_dir), compose_file="docker-compose.yml",
        secrets=None, paths=None,
    )
    params.update(overrides)
    script = render_up(**params)
    subprocess.run(
        ["bash", "-s"], input=script, text=True, check=True, capture_output=True,
        env={**os.environ, "REVISION": revision},
    )


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True)


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_up_script_appends_each_successful_deploy_to_the_history(tmp_path, fake_docker, bare_repo):
    bare, (v1, v2) = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    # A compose file name the repo doesn't contain: the old-image scan that
    # runs once it exists uses `mapfile`, which macOS's bash 3.2 lacks.
    common = {"compose_file": "compose.other.yml"}

    _run_up(bare, remote_dir, v1, **common)
    _run_up(bare, remote_dir, "main", **common)  # a ref name, recorded as v2's sha
    _run_up(bare, remote_dir, v1, kind="rollback", **common)

    lines = (remote_dir / ".wharf-history").read_text().splitlines()
    assert [line.split()[1:] for line in lines] == [[v1, "deploy"], [v2, "deploy"], [v1, "rollback"]]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line.split()[0]) for line in lines)
    assert (remote_dir / "app.txt").read_text() == "v1\n"


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_up_script_records_nothing_when_up_fails(tmp_path, bare_repo, monkeypatch):
    bare, (v1, _) = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    failing_docker = bin_dir / "docker"
    failing_docker.write_text("#!/bin/sh\nexit 1\n")
    failing_docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(subprocess.CalledProcessError):
        _run_up(bare, remote_dir, v1, compose_file="compose.other.yml")

    assert not (remote_dir / ".wharf-history").exists()


def test_status_script_reports_a_never_deployed_target(tmp_path, bare_repo):
    bare, _ = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    script = render_status(
        remote_repo=str(bare), remote_dir=str(remote_dir), compose_file="docker-compose.yml", secrets=None, paths=None,
    )

    result = _run(script)

    assert result.returncode == 0
    assert result.stdout == f"not deployed: {remote_dir} does not exist\n"


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_status_script_reports_revision_last_deploy_lock_and_services(tmp_path, fake_docker, bare_repo):
    bare, (_, v2) = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    _run_up(bare, remote_dir, v2)
    fake_docker.write_text("")
    script = render_status(
        remote_repo=str(bare), remote_dir=str(remote_dir), compose_file="docker-compose.yml", secrets=None, paths=None,
    )

    lines = _run(script).stdout.splitlines()

    assert lines[0] == f"revision: {v2[:7]} v2"
    assert lines[1].startswith("last deploy: 20") and lines[1].endswith(f"Z (deploy of {v2[:7]})")
    assert lines[2] == "lock: free"
    assert fake_docker.read_text() == "compose -f docker-compose.yml ps\n"


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs util-linux flock (Linux)")
def test_status_script_sees_a_lock_held_by_a_running_deploy(tmp_path, bare_repo):
    import fcntl  # POSIX-only, like flock itself

    bare, _ = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    remote_dir.mkdir(parents=True)
    lock = remote_dir / ".wharf-deploy.lock"
    lock.touch()
    script = render_status(
        remote_repo=str(bare), remote_dir=str(remote_dir), compose_file="docker-compose.yml", secrets=None, paths=None,
    )

    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        while_held = _run(script).stdout
    after = _run(script).stdout

    assert "lock: held (a deploy, down or reload is running)" in while_held
    assert "lock: free\n" in after
    assert "last deploy: no history recorded" in after
    assert lock.stat().st_size == 0  # probing never wrote to it


def test_logs_script_runs_compose_logs_in_remote_dir(tmp_path, fake_docker):
    remote_dir = tmp_path / "deploys" / "app"
    remote_dir.mkdir(parents=True)
    script = render_logs(
        remote_dir=str(remote_dir), compose_file="docker-compose.yml", secrets=None, paths=None,
        services=("api",), tail="20",
    )

    assert _run(script).returncode == 0
    assert fake_docker.read_text() == "compose -f docker-compose.yml logs --tail=20 api\n"


def test_logs_script_fails_clearly_on_a_never_deployed_target(tmp_path, fake_docker):
    script = render_logs(remote_dir=str(tmp_path / "nope"), compose_file="docker-compose.yml", secrets=None, paths=None)

    result = _run(script)

    assert result.returncode == 1
    assert "not deployed" in result.stderr
    assert not fake_docker.exists()


def test_history_script_echoes_entries_with_their_commit_subjects(tmp_path, bare_repo):
    bare, (v1, v2) = bare_repo
    remote_dir = tmp_path / "deploys" / "app"
    remote_dir.mkdir(parents=True)
    gone = "0" * 40  # a revision the bare repo no longer has
    (remote_dir / ".wharf-history").write_text(
        f"2026-09-17T10:00:00Z {v1} deploy\n2026-09-17T11:00:00Z {v2} deploy\n2026-09-17T12:00:00Z {gone} rollback\n"
    )

    result = _run(render_history(remote_repo=str(bare), remote_dir=str(remote_dir)))

    assert result.returncode == 0
    assert result.stdout == (
        f"2026-09-17T10:00:00Z {v1} deploy\tv1\n"
        f"2026-09-17T11:00:00Z {v2} deploy\tv2\n"
        f"2026-09-17T12:00:00Z {gone} rollback\t\n"
    )
    limited = _run(render_history(remote_repo=str(bare), remote_dir=str(remote_dir), limit=1))
    assert limited.stdout == f"2026-09-17T12:00:00Z {gone} rollback\t\n"


def test_history_script_prints_nothing_without_a_history_file(tmp_path, bare_repo):
    bare, _ = bare_repo
    result = _run(render_history(remote_repo=str(bare), remote_dir=str(tmp_path / "deploys" / "app")))
    assert (result.returncode, result.stdout) == (0, "")
