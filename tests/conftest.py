from pathlib import Path

import pytest

MINIMAL_TARGET = """\
    - name: app
      remote_dir: /opt/deploys/{repo}/app
      host: 203.0.113.10
      port: 22
      user: deploy
      host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIONdCvpb2NyLGGzZ6xmFdOyqzmEQziCRgRAPiJ5OmBeg
      order: 10
"""


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(text: str, name: str = "deploy.yml") -> Path:
        path = tmp_path / name
        path.write_text(text)
        return path

    return _write
