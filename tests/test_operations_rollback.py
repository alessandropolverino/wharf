import pytest

from wharf import operations
from wharf.config import load_config
from wharf.operations import DeployRecord, parse_history, rollback_target
from wharf.ssh import SessionAuth

V1, V2, V3 = "a" * 40, "b" * 40, "c" * 40

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
    healthcheck: https://app.example.com/health
"""


def _history(*entries):
    """render_history-shaped text: one `<timestamp> <sha> <kind>\\t<subject>` line per entry."""
    return "".join(
        f"2026-09-17T10:0{i}:00Z {revision} {kind}\t{subject}\n"
        for i, (revision, kind, subject) in enumerate(entries)
    )


# --- parsing and rollback selection: pure functions ---

def test_parse_history_reads_entries_and_ignores_login_shell_noise():
    text = "Welcome to prod!\n" + _history((V1, "deploy", "v1")) + f"2026-09-17T11:00:00Z {V2} rollback\n"
    assert parse_history(text) == [
        DeployRecord("2026-09-17T10:00:00Z", V1, "deploy", "v1"),
        DeployRecord("2026-09-17T11:00:00Z", V2, "rollback", ""),
    ]


def test_parse_history_skips_malformed_lines():
    text = f"2026-09-17T10:00:00Z abc123 deploy\n2026-09-17T10:00:00Z {V1} restart\n"
    assert parse_history(text) == []


def test_describe_leaves_out_a_missing_subject():
    assert DeployRecord("t", V1, "deploy").describe() == V1[:7]
    assert DeployRecord("t", V1, "deploy", "v1").describe() == f"{V1[:7]} v1"


def _records(*revisions):
    return [DeployRecord(f"t{i}", revision, "deploy") for i, revision in enumerate(revisions)]


def test_rollback_target_is_the_previous_distinct_revision():
    current, previous = rollback_target(_records(V1, V2, V2))  # v2 deployed twice in a row
    assert (current.revision, previous.revision) == (V2, V1)


def test_rollback_target_steps_walk_further_back():
    assert rollback_target(_records(V1, V2, V3), 2)[1].revision == V1


def test_rollback_after_a_rollback_goes_forward_again():
    # History is chronological, not a stack: after A, B, A the revision before the current A is B.
    current, previous = rollback_target(_records(V1, V2, V1))
    assert (current.revision, previous.revision) == (V1, V2)


def test_rollback_steps_never_land_on_the_current_revision():
    # v1, v2, v3, then a rollback to v2: two steps back from v2 is v1, not v2 again.
    records = _records(V1, V2, V3) + [DeployRecord("t3", V2, "rollback")]
    assert rollback_target(records, 2)[1].revision == V1
    with pytest.raises(RuntimeError, match="only goes back 2 distinct"):
        rollback_target(records, 3)


def test_rollback_target_explains_why_it_cannot():
    with pytest.raises(RuntimeError, match="no deploy history"):
        rollback_target([])
    with pytest.raises(RuntimeError, match="only goes back 1 distinct"):
        rollback_target(_records(V1, V2), 2)
    with pytest.raises(RuntimeError, match="only goes back 0 distinct"):
        rollback_target(_records(V1, V1))


def test_history_for_rollback_grows_the_limit_until_enough_distinct_revisions(monkeypatch):
    # One old deploy (V1), then the same revision (V2) redeployed 25 times in a
    # row -- the initial bounded window (20) sees only V2, not enough to
    # resolve a 1-step rollback, so the limit must grow until V1 is in view too.
    full = _records(V1, *([V2] * 25))
    limits_tried = []

    def fake_read_history(target, auth, remote_repo, limit):
        limits_tried.append(limit)
        return full[-limit:]

    monkeypatch.setattr(operations, "_read_history", fake_read_history)

    records = operations._history_for_rollback(target=None, auth=None, remote_repo="x", steps=1)

    assert limits_tried == [20, 80]  # 20 wasn't enough, 80 covers the whole 26-entry history
    assert {record.revision for record in records} == {V1, V2}


def test_history_for_rollback_reads_the_whole_history_at_most_once_if_that_is_not_enough(monkeypatch):
    # Every entry is the same revision: no number of distinct steps back
    # exists, so growing the limit past the actual history size must stop.
    full = _records(*([V1] * 5))
    limits_tried = []

    def fake_read_history(target, auth, remote_repo, limit):
        limits_tried.append(limit)
        return full[-limit:]

    monkeypatch.setattr(operations, "_read_history", fake_read_history)

    records = operations._history_for_rollback(target=None, auth=None, remote_repo="x", steps=1)

    assert limits_tried == [20]  # the fake already returned everything there is
    assert len(records) == 5


# --- orchestration, with SSH faked out ---

@pytest.fixture
def remote(monkeypatch):
    """Fake out every remote call and record what each action would have run."""
    calls = {"run": [], "capture": [], "health": [], "history_text": ""}
    monkeypatch.setattr(
        SessionAuth, "resolve", staticmethod(lambda *, force_ci=None, identity=None: SessionAuth(batch=True)),
    )

    def fake_capture(target, auth, script, env_vars, *, description):
        calls["capture"].append((target.name, script, description))
        return calls["history_text"]

    def fake_run(target, auth, script, env_vars, *, description):
        calls["run"].append((target.name, script, dict(env_vars), description))

    monkeypatch.setattr(operations, "capture_remote_script", fake_capture)
    monkeypatch.setattr(operations, "run_remote_script", fake_run)
    monkeypatch.setattr(operations, "wait_healthy", lambda url: calls["health"].append(url))
    return calls


def test_history_prints_newest_first_and_marks_the_current(write_config, remote, capsys):
    config = load_config(write_config(CONFIG_TEXT))
    remote["history_text"] = _history((V1, "deploy", "v1"), (V2, "deploy", "v2"), (V1, "rollback", "v1"))

    operations.history(config, repo="myapp", limit=3)

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "==> History of app (203.0.113.10:22)"
    assert out[1] == f"  2026-09-17T10:02:00Z  {V1[:7]}  rollback  v1  <- current"
    assert out[2] == f"  2026-09-17T10:01:00Z  {V2[:7]}  deploy    v2"
    assert out[3] == f"  2026-09-17T10:00:00Z  {V1[:7]}  deploy    v1"
    _, script, description = remote["capture"][0]
    assert 'tail -n 3 "$history_file"' in script
    assert description == "read deploy history on app"
    assert remote["run"] == []  # read-only


def test_history_says_so_when_there_is_none(write_config, remote, capsys):
    config = load_config(write_config(CONFIG_TEXT))

    operations.history(config, repo="myapp")

    assert "no deploy history recorded" in capsys.readouterr().out


def test_rollback_redeploys_the_previous_revision_through_the_deploy_script(write_config, remote, capsys):
    config = load_config(write_config(CONFIG_TEXT))
    remote["history_text"] = _history((V1, "deploy", "v1"), (V2, "deploy", "v2"))

    operations.rollback(config, repo="myapp")

    _, script, env_vars, description = remote["run"][0]
    assert env_vars == {"REVISION": V1}
    assert description == "rollback on app"
    assert 'checkout -f "$REVISION"' in script
    assert '"$deployed_revision" rollback >> "$history_file"' in script
    assert remote["health"] == ["https://app.example.com/health"]
    out = capsys.readouterr().out
    assert "==> Rolling back app (203.0.113.10:22)" in out
    assert f"{V2[:7]} v2 -> {V1[:7]} v1 (deployed 2026-09-17T10:00:00Z)" in out


def test_rollback_steps(write_config, remote):
    config = load_config(write_config(CONFIG_TEXT))
    remote["history_text"] = _history((V1, "deploy", "v1"), (V2, "deploy", "v2"), (V3, "deploy", "v3"))

    operations.rollback(config, repo="myapp", steps=2)

    assert remote["run"][0][2] == {"REVISION": V1}


def test_rollback_dry_run_reads_history_but_deploys_nothing(write_config, remote, capsys):
    config = load_config(write_config(CONFIG_TEXT))
    remote["history_text"] = _history((V1, "deploy", "v1"), (V2, "deploy", "v2"))

    operations.rollback(config, repo="myapp", dry_run=True)

    assert len(remote["capture"]) == 1
    assert remote["run"] == [] and remote["health"] == []
    out = capsys.readouterr().out
    assert out.startswith("==> [dry run] Rolling back app (203.0.113.10:22)\n")
    assert f"Would run on deploy@203.0.113.10:22: REVISION={V1} bash -l -s <<'WHARF_SCRIPT'" in out
    assert out.endswith("WHARF_SCRIPT\nWould then poll https://app.example.com/health until it responds\n")


def test_rollback_without_history_fails_with_advice(write_config, remote):
    config = load_config(write_config(CONFIG_TEXT))

    with pytest.raises(operations.OperationError, match="target 'app': no deploy history .* --revision"):
        operations.rollback(config, repo="myapp")

    assert remote["run"] == []


def test_rollback_still_enforces_ensure_branch(write_config, remote, monkeypatch):
    config = load_config(write_config(CONFIG_TEXT + "ensure_branch: main\n"))
    monkeypatch.setattr(operations, "infer_current_branch", lambda cwd=None: "feature-x")

    with pytest.raises(operations.BranchMismatchError):
        operations.rollback(config, repo="myapp")

    assert remote["capture"] == []
