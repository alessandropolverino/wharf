import json
from io import BytesIO

from wharf.update_check import _parse_version, check_for_update


def test_parse_version():
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("1.2.3") == (1, 2, 3)
    assert _parse_version("v2.0") == (2, 0)


def test_check_for_update_returns_notice_when_newer(monkeypatch):
    monkeypatch.setattr("wharf.update_check.__version__", "0.1.0")

    def fake_urlopen(url, timeout):
        return BytesIO(json.dumps({"tag_name": "v0.2.0"}).encode())

    monkeypatch.setattr("wharf.update_check.urllib.request.urlopen", fake_urlopen)
    notice = check_for_update()
    assert notice is not None
    assert "v0.2.0" in notice


def test_check_for_update_returns_none_when_up_to_date(monkeypatch):
    monkeypatch.setattr("wharf.update_check.__version__", "0.1.0")

    def fake_urlopen(url, timeout):
        return BytesIO(json.dumps({"tag_name": "v0.1.0"}).encode())

    monkeypatch.setattr("wharf.update_check.urllib.request.urlopen", fake_urlopen)
    assert check_for_update() is None


def test_check_for_update_swallows_network_errors(monkeypatch):
    def fake_urlopen(url, timeout):
        raise OSError("no network")

    monkeypatch.setattr("wharf.update_check.urllib.request.urlopen", fake_urlopen)
    assert check_for_update() is None
