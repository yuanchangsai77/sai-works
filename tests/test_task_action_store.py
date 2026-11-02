import sqlite3
import pytest
from saiworks.sessions.tasks import TaskStore
from saiworks.types import ExecutionSummary, UserRequest


@pytest.mark.parametrize('error_code', ['new_provider_failure', None, 'invalid_patch'])
def test_effectful_failure_never_defaults_to_known_without_execution_proof(tmp_path, error_code):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.accept(UserRequest('effect', str(tmp_path)), submission_id='one')
    assert store.claim('one')
    action = store.start_action(task.task_id, 'one', 'external', {}, str(tmp_path), 'write')
    store.finish_action(action, success=False, error_code=error_code)
    assert store.unknown_actions(task.task_id)[0]['state'] == 'uncertain'
    with pytest.raises(RuntimeError, match='unknown outcome'):
        store.start_action(task.task_id, 'one', 'another', {}, str(tmp_path), 'write')


def test_reconciliation_requires_basis_and_persists_redacted_decision_atomically(tmp_path):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.accept(UserRequest('effect', str(tmp_path)), submission_id='one')
    assert store.claim('one')
    action = store.start_action(task.task_id, 'one', 'external', {}, str(tmp_path), 'write')
    store.finish('one', ExecutionSummary('lost', [], outcome='runtime_error'))
    with pytest.raises(ValueError, match='actor and nonempty evidence'):
        store.resolve_action(action, effect_state='performed', actor='', evidence={})
    assert len(store.unknown_actions(task.task_id)) == 1
    store.resolve_action(action, effect_state='performed', actor='owner',
                         evidence={'receipt': 'verified locally', 'api_key': 'supersecret'})
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute('SELECT kind, actor, evidence FROM action_evidence WHERE action_id=?', (action,)).fetchall()
    assert len(rows) == 1
    assert rows[0][:2] == ('reconciliation', 'owner')
    assert 'supersecret' not in rows[0][2]
    assert '[REDACTED]' in rows[0][2]
    assert store.unknown_actions(task.task_id) == ()


@pytest.mark.parametrize('operation', ['phase', 'reconciliation'])
def test_evidence_write_failure_rolls_back_the_action_state(tmp_path, monkeypatch, operation):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.accept(UserRequest('work', str(tmp_path)), submission_id='one')
    store.claim('one')
    action = store.start_action(task.task_id, 'one', 'effect', {}, str(tmp_path), 'write')
    if operation == 'reconciliation':
        store.finish('one', ExecutionSummary('stopped', [], outcome='runtime_error'))
    connect = store._connect
    def deny_evidence():
        connection = connect()
        connection.set_authorizer(lambda action, table, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'action_evidence'
            else sqlite3.SQLITE_OK)
        return connection
    monkeypatch.setattr(store, '_connect', deny_evidence)
    with pytest.raises(sqlite3.DatabaseError):
        if operation == 'phase':
            store.finish_action(action, success=False, error_code='invalid_patch', known_not_started=True)
        else:
            store.resolve_action(action, effect_state='not_performed', actor='owner', evidence={'receipt': 'verified'})
    assert store.unknown_actions(task.task_id)[0]['state'] == 'started'


@pytest.mark.parametrize('success,proof', [(True, 'false'), (1, False), (False, 1)])
def test_execution_phase_requires_real_booleans(tmp_path, success, proof):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.accept(UserRequest('work', str(tmp_path)), submission_id='one')
    store.claim('one')
    action = store.start_action(task.task_id, 'one', 'effect', {}, str(tmp_path), 'write')
    with pytest.raises(TypeError, match='booleans'):
        store.finish_action(action, success=success, error_code='failure', known_not_started=proof)
    assert store.unknown_actions(task.task_id)[0]['state'] == 'started'
