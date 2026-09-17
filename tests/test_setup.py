import subprocess

from wharf import setup as wharf_setup
from wharf.config import load_config
from wharf.setup import _render_setup_script, ensure_deploy_keypair

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


def test_ensure_deploy_keypair_default_identity_uses_legacy_path(tmp_path):
    key_dir = tmp_path / ".wharf"
    private, public = ensure_deploy_keypair("default", key_dir=key_dir)
    assert private == key_dir / "deploy_key"
    assert public == key_dir / "deploy_key.pub"
    assert "wharf-deploy" in public.read_text()


def test_ensure_deploy_keypair_named_identity_uses_keys_subdir(tmp_path):
    key_dir = tmp_path / ".wharf"
    private, public = ensure_deploy_keypair("ci", key_dir=key_dir)
    assert private == key_dir / "keys" / "ci_key"
    assert "wharf:ci" in public.read_text()


def test_ensure_deploy_keypair_is_idempotent(tmp_path, capsys):
    key_dir = tmp_path / ".wharf"
    private1, _ = ensure_deploy_keypair("ci", key_dir=key_dir)
    content1 = private1.read_text()

    private2, _ = ensure_deploy_keypair("ci", key_dir=key_dir)

    assert private2.read_text() == content1
    assert "leaving it as-is" in capsys.readouterr().out


def test_setup_defaults_to_default_identity_when_not_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(wharf_setup, "_provision_target", lambda *a, **k: None)

    wharf_setup.setup(config, repo="app")

    assert (tmp_path / ".wharf" / "deploy_key").exists()
    assert not (tmp_path / ".wharf" / "keys" / "ci_key").exists()


def test_setup_resolves_ci_identity_when_running_in_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(wharf_setup, "_provision_target", lambda *a, **k: None)

    wharf_setup.setup(config, repo="app")

    assert (tmp_path / ".wharf" / "keys" / "ci_key").exists()
    assert not (tmp_path / ".wharf" / "deploy_key").exists()


def test_setup_explicit_identity_overrides_ci_detection(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(wharf_setup, "_provision_target", lambda *a, **k: None)

    wharf_setup.setup(config, repo="app", identity="release-bot")

    assert (tmp_path / ".wharf" / "keys" / "release-bot_key").exists()


def test_setup_passes_resolved_identitys_public_key_to_provision_target(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    seen = {}

    def fake_provision(config, target, repo, public_key):
        seen["public_key"] = public_key

    monkeypatch.setattr(wharf_setup, "_provision_target", fake_provision)
    wharf_setup.setup(config, repo="app")

    assert seen["public_key"].endswith("wharf-deploy")


# --- _render_setup_script executed for real, no SSH ---

DEPLOY_KEY = "ssh-ed25519 AAAANEWDEPLOYKEY wharf-deploy"


def _run_setup_script(tmp_path, monkeypatch) -> subprocess.CompletedProcess:
    monkeypatch.setenv("HOME", str(tmp_path))
    script = _render_setup_script(
        remote_repo=str(tmp_path / "git" / "app.git"),
        remote_dir=str(tmp_path / "deploys" / "app"),
        public_key=DEPLOY_KEY,
        target_name="app",
    )
    return subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, check=True)


def test_setup_script_creates_bare_repo_remote_dir_and_authorized_keys(tmp_path, monkeypatch):
    _run_setup_script(tmp_path, monkeypatch)

    assert (tmp_path / "git" / "app.git" / "HEAD").is_file()
    assert (tmp_path / "deploys" / "app").is_dir()
    assert (tmp_path / ".ssh" / "authorized_keys").read_text() == DEPLOY_KEY + "\n"


def test_setup_script_repairs_missing_trailing_newline_before_appending(tmp_path, monkeypatch):
    # Without the repair, `echo >>` produced
    # "ssh-ed25519 AAAAOPERATORssh-ed25519 AAAANEWDEPLOYKEY wharf-deploy":
    # the operator's key blob corrupted and the deploy key never authorized.
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "authorized_keys").write_text("ssh-ed25519 AAAAOPERATOR")

    _run_setup_script(tmp_path, monkeypatch)

    assert (ssh_dir / "authorized_keys").read_text() == f"ssh-ed25519 AAAAOPERATOR\n{DEPLOY_KEY}\n"


def test_setup_script_leaves_newline_terminated_file_alone(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "authorized_keys").write_text("ssh-ed25519 AAAAOPERATOR someone@laptop\n")

    _run_setup_script(tmp_path, monkeypatch)

    assert (ssh_dir / "authorized_keys").read_text() == f"ssh-ed25519 AAAAOPERATOR someone@laptop\n{DEPLOY_KEY}\n"


def test_setup_script_is_idempotent(tmp_path, monkeypatch):
    _run_setup_script(tmp_path, monkeypatch)
    second = _run_setup_script(tmp_path, monkeypatch)

    assert "Deploy key already authorized on app" in second.stdout
    assert "Bare repo already exists at" in second.stdout
    assert (tmp_path / ".ssh" / "authorized_keys").read_text() == DEPLOY_KEY + "\n"
