import subprocess

import pytest

from wharf import rotate as rotate_module
from wharf.config import ConfigError, load_config
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


def test_rotate_script_aborts_on_real_grep_failure_without_destroying_keys(tmp_path, monkeypatch):
    """A real grep error (exit >= 2, e.g. "Is a directory") must abort the
    script under `set -e` rather than being swallowed like the benign
    "no lines matched" exit-1 case -- otherwise an empty temp file would
    get `mv`-ed over authorized_keys, destroying every key on the host."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    # A directory in place of authorized_keys makes `grep` exit 2 (GNU
    # grep's "Is a directory" error), simulating a real grep failure that
    # isn't the benign "no lines selected" exit-1 case.
    (ssh_dir / "authorized_keys").mkdir()
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    result = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True)

    assert result.returncode != 0
    # authorized_keys must still be the untouched directory, not
    # replaced by a (possibly empty) file.
    assert (ssh_dir / "authorized_keys").is_dir()


def test_rotate_script_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    new_key = "ssh-ed25519 AAAANEW wharf:ci"
    script = _render_rotate_script(new_key, " wharf:ci$", "app")

    subprocess.run(["bash", "-s"], input=script, text=True, check=True)
    subprocess.run(["bash", "-s"], input=script, text=True, check=True)

    lines = (tmp_path / ".ssh" / "authorized_keys").read_text().splitlines()
    assert lines.count(new_key) == 1


def test_rotate_script_handles_authorized_keys_without_trailing_newline(tmp_path, monkeypatch):
    # grep terminates its last output line, so the appended key can't be
    # glued onto a final line that lacked a newline (the bug `setup` had).
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "authorized_keys").write_text("ssh-ed25519 AAAAOLD wharf:ci\nssh-ed25519 AAAAHUMAN")
    new_key = "ssh-ed25519 AAAANEW wharf:ci"

    subprocess.run(["bash", "-s"], input=_render_rotate_script(new_key, " wharf:ci$", "app"), text=True, check=True)

    assert (ssh_dir / "authorized_keys").read_text() == f"ssh-ed25519 AAAAHUMAN\n{new_key}\n"


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


TWO_TARGETS = CONFIG_TEXT + """\
  - name: worker
    remote_dir: /opt/deploys/{repo}/worker
    host: 203.0.113.11
    port: 22
    user: deploy
    host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
    order: 20
"""


def test_rotate_rejects_unknown_only_target_before_staging_a_key(tmp_path, monkeypatch, write_config):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(CONFIG_TEXT))
    monkeypatch.setattr(rotate_module, "_rotate_target", lambda *a, **k: None)

    with pytest.raises(ConfigError, match="typo"):
        rotate_module.rotate(config, repo="app", identity="ci", only=("typo",))

    assert not (tmp_path / ".wharf").exists()


def test_rotate_with_only_warns_about_targets_left_on_the_old_key(tmp_path, monkeypatch, write_config, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(TWO_TARGETS))
    calls = []
    monkeypatch.setattr(
        rotate_module, "_rotate_target",
        lambda target, new_public_key, marker_pattern: calls.append(target.name),
    )

    rotate_module.rotate(config, repo="app", identity="ci", only=("app",))

    out = capsys.readouterr().out
    assert calls == ["app"]
    assert "WARNING: not rotated (excluded by --only): worker" in out
    assert "without --only" in out


def test_rotate_of_every_target_prints_no_warning(tmp_path, monkeypatch, write_config, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CI", raising=False)
    config = load_config(write_config(TWO_TARGETS))
    monkeypatch.setattr(rotate_module, "_rotate_target", lambda *a, **k: None)

    rotate_module.rotate(config, repo="app", identity="ci", only=("worker", "app"))

    assert "WARNING" not in capsys.readouterr().out
