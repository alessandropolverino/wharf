import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wharf.config import load_config
from wharf.operations import BranchMismatchError, _check_branch, infer_current_branch

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


def _init_repo(path: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "-b", branch], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_infer_current_branch(tmp_path):
    _init_repo(tmp_path, "feature-x")
    assert infer_current_branch(tmp_path) == "feature-x"


def test_check_branch_noop_when_ensure_branch_unset(write_config, tmp_path):
    config = load_config(write_config(CONFIG_TEXT))
    _init_repo(tmp_path, "whatever")
    _check_branch(config, tmp_path)  # must not raise


def test_check_branch_passes_when_matching(write_config, tmp_path):
    config = replace(load_config(write_config(CONFIG_TEXT)), ensure_branch="main")
    _init_repo(tmp_path, "main")
    _check_branch(config, tmp_path)  # must not raise


def test_check_branch_raises_when_mismatched(write_config, tmp_path):
    config = replace(load_config(write_config(CONFIG_TEXT)), ensure_branch="main")
    _init_repo(tmp_path, "feature-x")
    with pytest.raises(BranchMismatchError, match="feature-x"):
        _check_branch(config, tmp_path)
