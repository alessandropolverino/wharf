import subprocess

import pytest

from wharf.identity import (
    CI_IDENTITY,
    DEFAULT_IDENTITY,
    InvalidIdentityError,
    generate_keypair,
    key_comment,
    key_paths,
    resolve_identity,
    staged_key_paths,
    validate_identity_name,
)


def test_resolve_identity_prefers_explicit_over_everything():
    assert resolve_identity("release-bot", is_ci=True) == "release-bot"
    assert resolve_identity("release-bot", is_ci=False) == "release-bot"


def test_resolve_identity_defaults_to_ci_when_detected():
    assert resolve_identity(None, is_ci=True) == CI_IDENTITY


def test_resolve_identity_defaults_to_default_when_not_ci():
    assert resolve_identity(None, is_ci=False) == DEFAULT_IDENTITY


@pytest.mark.parametrize("bad_name", ["", "CI", "my_bot", "-leading-hyphen", "has space", "wharf:ci"])
def test_validate_identity_name_rejects_invalid_names(bad_name):
    with pytest.raises(InvalidIdentityError):
        validate_identity_name(bad_name)


@pytest.mark.parametrize("good_name", ["default", "ci", "release-bot", "ci-staging", "a"])
def test_validate_identity_name_accepts_valid_names(good_name):
    assert validate_identity_name(good_name) == good_name


def test_resolve_identity_validates_explicit_name():
    with pytest.raises(InvalidIdentityError):
        resolve_identity("Not Valid", is_ci=False)


def test_key_paths_default_identity_uses_legacy_layout(tmp_path):
    private, public = key_paths(DEFAULT_IDENTITY, tmp_path)
    assert private == tmp_path / "deploy_key"
    assert public == tmp_path / "deploy_key.pub"


def test_key_paths_named_identity_uses_keys_subdir(tmp_path):
    private, public = key_paths("ci", tmp_path)
    assert private == tmp_path / "keys" / "ci_key"
    assert public == tmp_path / "keys" / "ci_key.pub"


def test_key_comment_default_identity_is_unchanged_legacy_string():
    assert key_comment(DEFAULT_IDENTITY) == "wharf-deploy"


def test_key_comment_named_identity_is_namespaced():
    assert key_comment("ci") == "wharf:ci"


def test_staged_key_paths_appends_new_suffix(tmp_path):
    private = tmp_path / "keys" / "ci_key"
    staged_private, staged_public = staged_key_paths(private)
    assert staged_private == tmp_path / "keys" / "ci_key.new"
    assert staged_public == tmp_path / "keys" / "ci_key.new.pub"


def test_generate_keypair_creates_parent_dir_and_ed25519_key(tmp_path):
    private_key = tmp_path / "keys" / "ci_key"
    generate_keypair(private_key, "wharf:ci")
    assert private_key.exists()
    public_key = private_key.with_name(private_key.name + ".pub")
    assert public_key.exists()
    assert public_key.read_text().strip().endswith("wharf:ci")
    result = subprocess.run(["ssh-keygen", "-lf", str(public_key)], capture_output=True, text=True, check=True)
    assert "ED25519" in result.stdout
