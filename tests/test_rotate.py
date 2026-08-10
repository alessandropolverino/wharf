import subprocess

import pytest

from wharf import rotate as rotate_module
from wharf.config import load_config
from wharf.identity import generate_keypair, key_comment, key_paths, staged_key_paths
from wharf.rotate import _render_rotate_script
from wharf.ssh import RemoteCommandError

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


# --- _render_rotate_script: pure string rendering, checked for shape ---

def test_render_rotate_script_orders_filter_before_append_before_move():
    script = _render_rotate_script(
        new_public_key="ssh-ed25519 AAAANEW wharf:ci",
        marker_pattern=" wharf:ci$",
        target_name="app",
    )
    filter_pos = script.index("grep -v --")
    append_pos = script.index("wharf:ci$", filter_pos)  # the marker inside the grep line
    move_pos = script.index("mv \"$tmp_file\"")
    assert filter_pos < append_pos < move_pos
    assert "'ssh-ed25519 AAAANEW wharf:ci'" in script


# --- _render_rotate_script executed for real, no SSH: this is where the
# actual grep/marker logic gets verified, since a substring match on the
# wrong identity's marker (e.g. "wharf:ci" matching inside "wharf:ci-staging")
# would silently strand or duplicate access. ---

def test_rotate_script_replaces_old_key_without_touching_other_identities(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    old_key = "ssh-ed25519 AAAAOLD wharf:ci"
    sibling_identity_key = "ssh-ed25519 AAAASIB wharf:ci-staging"
    unrelated_human_key = "ssh-ed25519 AAAAHUMAN someone@laptop"
    (ssh_dir / "authorized_keys").write_text(
        "\n".join([old_key, sibling_identity_key, unrelated_human_key]) + "\n"
    )
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    subprocess.run(["bash", "-s"], input=script, text=True, check=True)

    lines = (ssh_dir / "authorized_keys").read_text().splitlines()
    assert old_key not in lines
    assert new_key in lines
    assert sibling_identity_key in lines
    assert unrelated_human_key in lines


def test_rotate_script_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    subprocess.run(["bash", "-s"], input=script, text=True, check=True)
    subprocess.run(["bash", "-s"], input=script, text=True, check=True)

    lines = (tmp_path / ".ssh" / "authorized_keys").read_text().splitlines()
    assert lines.count(new_key) == 1


# --- rotate(): staging/promotion orchestration, with _rotate_target
# faked out so no real SSH happens ---

def test_rotate_promotes_staged_key_after_all_targets_succeed(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    old_content = live_private.read_text()
    calls = []
    monkeypatch.setattr(
        rotate_module, "_rotate_target",
        lambda target, new_public_key, marker_pattern: calls.append(target.name),
    )

    rotate_module.rotate(config, repo="app", identity="ci")

    assert calls == ["app"]
    assert live_private.read_text() != old_content  # promoted to the new key
    staged_private, _ = staged_key_paths(live_private)
    assert not staged_private.exists()


def test_rotate_leaves_live_key_untouched_when_a_target_fails(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    old_content = live_private.read_text()

    def boom(target, new_public_key, marker_pattern):
        raise RemoteCommandError(f"rotate on {target.name}", 1)

    monkeypatch.setattr(rotate_module, "_rotate_target", boom)

    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")

    assert live_private.read_text() == old_content
    staged_private, _ = staged_key_paths(live_private)
    assert staged_private.exists()


def test_rotate_reuses_staged_key_on_retry_instead_of_regenerating(tmp_path, monkeypatch, write_config, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))

    def boom(target, new_public_key, marker_pattern):
        raise RemoteCommandError(f"rotate on {target.name}", 1)

    monkeypatch.setattr(rotate_module, "_rotate_target", boom)
    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")
    staged_private, _ = staged_key_paths(live_private)
    first_staged_content = staged_private.read_text()

    with pytest.raises(RemoteCommandError):
        rotate_module.rotate(config, repo="app", identity="ci")

    assert staged_private.read_text() == first_staged_content
    assert "reusing it" in capsys.readouterr().out


def test_rotate_defaults_to_ci_identity_in_ci(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    config = load_config(write_config(CONFIG_TEXT))
    live_private, _ = key_paths("ci", tmp_path / ".wharf")
    generate_keypair(live_private, key_comment("ci"))
    monkeypatch.setattr(rotate_module, "_rotate_target", lambda *a, **k: None)

    rotate_module.rotate(config, repo="app")

    assert not (tmp_path / ".wharf" / "deploy_key").exists()
