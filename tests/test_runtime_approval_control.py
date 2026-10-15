import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from saiworks.config import load_runtime_config
from saiworks.runtime import Runtime
from saiworks.runtime_control import TaskControl
from saiworks.runtime_observation import TaskAccess
from saiworks.sessions.tasks import TaskStore, fingerprint_action
from saiworks.types import ExecutionSummary, UserRequest


def _runtime(tmp_path, **kwargs):
    kwargs.setdefault("task_store", TaskStore(tmp_path / "tasks.sqlite3"))
    runtime = Runtime(object(), **kwargs)
    task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="work")
    return runtime, task


def _request(runtime, task, **kwargs):
    kwargs.setdefault("required_scope", "write")
    return runtime.request_approval(task.submission_id, "patch", {"path": "a.py"}, **kwargs)


def test_request_creates_pending_approval_bound_to_action(tmp_path):
    runtime, task = _runtime(tmp_path, approval_ttl_seconds=60)
    before = runtime.task_store.snapshot(task.task_id).revision
    approval = _request(runtime, task)
    assert approval.state == "pending"
    assert approval.task_id == task.task_id
    assert approval.submission_id == task.submission_id
    assert approval.workspace_root == str(tmp_path.resolve())
    assert approval.action_fingerprint == fingerprint_action(
        task.task_id, task.submission_id, "patch", {"path": "a.py"}, str(tmp_path), "write")
    created = datetime.fromisoformat(approval.created_at)
    expires = datetime.fromisoformat(approval.expires_at)
    assert expires - created == timedelta(seconds=60)
    assert runtime.task_store.snapshot(task.task_id).revision == before + 1


def test_decide_approve_and_deny_record_single_standing_ruling(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"approve"}, actor="cli")
    approval = _request(runtime, task)
    decided = runtime.decide(control, approval.approval_id, "approve",
                             expected_action_fingerprint=approval.action_fingerprint)
    assert decided.state == "approved"
    assert decided.decision_actor == "cli"
    assert decided.decided_at is not None
    repeated = runtime.decide(control, approval.approval_id, "approve",
                              expected_action_fingerprint=approval.action_fingerprint)
    assert repeated == decided
    conflict = runtime.decide(control, approval.approval_id, "deny",
                              expected_action_fingerprint=approval.action_fingerprint)
    assert conflict == decided

    other = _request(runtime, task, required_scope="execute")
    denied = runtime.decide(control, other.approval_id, "deny",
                            expected_action_fingerprint=other.action_fingerprint)
    assert denied.state == "denied"
    with pytest.raises(ValueError, match="decision"):
        runtime.decide(control, other.approval_id, "maybe",
                       expected_action_fingerprint=other.action_fingerprint)


def test_decide_with_stale_fingerprint_writes_nothing(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"approve"})
    approval = _request(runtime, task)
    with pytest.raises(ValueError, match="fingerprint"):
        runtime.decide(control, approval.approval_id, "approve",
                       expected_action_fingerprint="stale")
    assert runtime.task_store.get_approval(approval.approval_id).state == "pending"


def test_expired_approval_refuses_decision_and_rerequest_issues_new_identity(tmp_path):
    runtime, task = _runtime(tmp_path, approval_ttl_seconds=10)
    control = runtime.grant_task_control(task.task_id, {"approve"})
    moment = datetime(2026, 10, 15, tzinfo=timezone.utc)
    approval = _request(runtime, task, now=moment)
    late = moment + timedelta(seconds=11)
    decided = runtime.decide(control, approval.approval_id, "approve",
                             expected_action_fingerprint=approval.action_fingerprint, now=late)
    assert decided.state == "expired"
    assert decided.decision_actor is None
    renewed = _request(runtime, task, now=late)
    assert renewed.approval_id != approval.approval_id
    assert renewed.state == "pending"
    assert runtime.pending_approvals(control, now=late) == (renewed,)
    assert runtime.pending_approvals(control, now=late + timedelta(seconds=11)) == ()
    assert runtime.task_store.get_approval(renewed.approval_id).state == "expired"


def test_cancel_acceptance_is_idempotent_and_invalidates_late_decisions(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"cancel", "approve"})
    approval = _request(runtime, task)
    accepted_at = runtime.cancel(control)
    assert runtime.cancel(control) == accepted_at
    assert runtime.task_store.cancellation_accepted(task.task_id) == accepted_at
    late = runtime.decide(control, approval.approval_id, "approve",
                          expected_action_fingerprint=approval.action_fingerprint)
    assert late.state == "cancelled"
    assert late.decision_actor is None
    with pytest.raises(RuntimeError, match="cancellation"):
        _request(runtime, task)


def test_approved_ruling_survives_later_cancellation_acceptance(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"cancel", "approve"})
    approval = _request(runtime, task)
    decided = runtime.decide(control, approval.approval_id, "approve",
                             expected_action_fingerprint=approval.action_fingerprint)
    runtime.cancel(control)
    assert runtime.task_store.get_approval(approval.approval_id) == decided


def test_decide_and_cancel_race_lands_exactly_one_ordering(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"cancel", "approve"})
    approval = _request(runtime, task)
    with ThreadPoolExecutor(max_workers=2) as pool:
        decision = pool.submit(runtime.decide, control, approval.approval_id, "approve",
                               expected_action_fingerprint=approval.action_fingerprint)
        cancellation = pool.submit(runtime.cancel, control)
        decided = decision.result(timeout=5)
        cancellation.result(timeout=5)
    assert decided.state in {"approved", "cancelled"}
    if decided.state == "approved":
        assert runtime.task_store.cancellation_accepted(task.task_id) is not None

    runtime, task = _runtime(tmp_path / "second")
    control = runtime.grant_task_control(task.task_id, {"cancel", "approve"})
    approval = _request(runtime, task)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.cancel, control)
        second = pool.submit(runtime.decide, control, approval.approval_id, "deny",
                             expected_action_fingerprint=approval.action_fingerprint)
        first.result(timeout=5)
        decided = second.result(timeout=5)
    assert decided.state in {"denied", "cancelled"}


def test_control_qualifications_are_enforced(tmp_path):
    runtime, task = _runtime(tmp_path)
    approval = _request(runtime, task)
    approve_only = runtime.grant_task_control(task.task_id, {"approve"})
    cancel_only = runtime.grant_task_control(task.task_id, {"cancel"})
    with pytest.raises(PermissionError):
        runtime.cancel(approve_only)
    with pytest.raises(PermissionError):
        runtime.decide(cancel_only, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    with pytest.raises(PermissionError):
        runtime.decide(TaskAccess(), approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    with pytest.raises(PermissionError):
        runtime.cancel(TaskAccess())
    with pytest.raises(PermissionError):
        runtime.decide(TaskControl(), approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    runtime.revoke_task_control(approve_only)
    with pytest.raises(PermissionError):
        runtime.decide(approve_only, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    runtime.cancel(cancel_only)


def test_control_cannot_cross_tasks_and_malformed_handles_are_rejected(tmp_path):
    runtime, task = _runtime(tmp_path)
    other = runtime.accept(UserRequest("other", str(tmp_path)), submission_id="other")
    approval = runtime.request_approval(other.submission_id, "patch", {"path": "b.py"},
                                        required_scope="write")
    control = runtime.grant_task_control(task.task_id, {"approve"})
    with pytest.raises(PermissionError, match="different task"):
        runtime.decide(control, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    with pytest.raises(PermissionError):
        runtime.decide(None, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    for invalid in (None, "task", {}, []):
        with pytest.raises(PermissionError):
            runtime.revoke_task_control(invalid)
    with pytest.raises(ValueError, match="permission"):
        runtime.grant_task_control(task.task_id, {"approve", "execute"})
    with pytest.raises(ValueError, match="permission"):
        runtime.grant_task_control(task.task_id, set())
    with pytest.raises(KeyError):
        runtime.grant_task_control("missing", {"approve"})
    with pytest.raises(KeyError):
        runtime.decide(control, "missing", "approve", expected_action_fingerprint="x")
    with pytest.raises(KeyError):
        runtime.request_approval("missing", "patch", {}, required_scope="write")


def test_closed_runtime_revokes_control_and_blocks_issuance(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"approve", "cancel"})
    approval = _request(runtime, task)
    runtime.close()
    with pytest.raises(PermissionError):
        runtime.decide(control, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
    with pytest.raises(PermissionError):
        runtime.cancel(control)
    with pytest.raises(RuntimeError, match="closed"):
        runtime.grant_task_control(task.task_id, {"approve"})
    with pytest.raises(RuntimeError, match="closed"):
        _request(runtime, task)


def test_revision_bumps_with_each_authoritative_change(tmp_path):
    runtime, task = _runtime(tmp_path)
    control = runtime.grant_task_control(task.task_id, {"approve", "cancel"})
    snapshot = runtime.task_store.snapshot(task.task_id)
    approval = _request(runtime, task)
    assert runtime.task_store.snapshot(task.task_id).revision == snapshot.revision + 1
    runtime.decide(control, approval.approval_id, "deny",
                   expected_action_fingerprint=approval.action_fingerprint)
    assert runtime.task_store.snapshot(task.task_id).revision == snapshot.revision + 2
    runtime.cancel(control)
    assert runtime.task_store.snapshot(task.task_id).revision == snapshot.revision + 3
    runtime.cancel(control)
    assert runtime.task_store.snapshot(task.task_id).revision == snapshot.revision + 3


def test_upgrade_v2_database_and_reject_unknown_versions(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = TaskStore(path)
    task = store.accept(UserRequest("work", str(tmp_path)), submission_id="work")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE approvals")
        connection.execute("DROP TABLE task_cancellations")
        connection.execute("PRAGMA user_version=2")
    upgraded = TaskStore(path)
    assert upgraded.snapshot(task.task_id).submissions[0].submission_id == "work"
    approval = upgraded.request_approval(
        task_id=task.task_id, submission_id=task.submission_id, action_name="patch",
        arguments={}, workspace_root=str(tmp_path), required_scope="write", ttl_seconds=30)
    assert approval.state == "pending"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        connection.execute("PRAGMA user_version=4")
    with pytest.raises(ValueError, match="database version"):
        TaskStore(path)


def test_approval_record_rejects_unknown_state(tmp_path):
    runtime, task = _runtime(tmp_path)
    approval = _request(runtime, task)
    with sqlite3.connect(runtime.task_store.path) as connection:
        connection.execute("UPDATE approvals SET state='mystery' WHERE approval_id=?", (approval.approval_id,))
    with pytest.raises(ValueError, match="approval record state"):
        runtime.task_store.get_approval(approval.approval_id)


def test_approval_ttl_config_defaults_and_bounds(tmp_path):
    config = load_runtime_config(cwd=tmp_path)
    assert config.orchestration.approval_ttl_seconds == 300
    config_dir = tmp_path / ".saiworks"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[orchestration]\napproval_ttl_seconds = 120\n")
    assert load_runtime_config(cwd=tmp_path).orchestration.approval_ttl_seconds == 120
    (config_dir / "config.toml").write_text("[orchestration]\napproval_ttl_seconds = 0\n")
    with pytest.raises(ValueError, match="approval_ttl_seconds"):
        load_runtime_config(cwd=tmp_path)


def test_execution_summary_still_unaffected_by_approval_records(tmp_path):
    runtime, task = _runtime(tmp_path)
    _request(runtime, task)
    runtime.task_store.claim(task.submission_id)
    runtime.task_store.finish(task.submission_id, ExecutionSummary("done", []))
    snapshot = runtime.task_store.snapshot(task.task_id)
    assert snapshot.state == "finished"
    assert snapshot.submissions[0].result.final_message == "done"
