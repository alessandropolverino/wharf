import subprocess

import pytest

from wharf import operations
from wharf.operations import infer_repo_name


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/myapp.git",
        "https://github.com/owner/myapp",
        "git@github.com:owner/myapp.git",
        "git@myserver:myapp.git",  # scp-style with no "/" in the path
        "ssh://git@myserver:2222/srv/git/myapp.git/",
        "/srv/git/myapp.git",
    ],
)
def test_infer_repo_name_from_origin_url(tmp_path, url):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", url)
    assert infer_repo_name(tmp_path) == "myapp"


def test_infer_repo_name_falls_back_to_directory_without_origin(tmp_path):
    checkout = tmp_path / "myapp"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    assert infer_repo_name(checkout) == "myapp"


def test_infer_repo_name_falls_back_to_directory_without_git(tmp_path, monkeypatch):
    def git_not_installed(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(operations.subprocess, "run", git_not_installed)
    checkout = tmp_path / "myapp"
    checkout.mkdir()
    assert infer_repo_name(checkout) == "myapp"
