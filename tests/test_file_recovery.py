import json
import sqlite3

import pytest

from saiworks.sessions.tasks import TaskStore
from saiworks.sessions.file_recovery import describe_creation
from saiworks.types import ExecutionSummary, UserRequest

DIFF = '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello\n'


def pending(tmp_path):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = store.accept(UserRequest('create', str(tmp_path)), submission_id='one', task_id='task')
    assert store.claim('one')
    action = store.start_action(task.task_id, 'one', 'patch', {'diff': DIFF}, str(tmp_path), 'write')
    return store, action


@pytest.mark.parametrize('content, observed', [(None, 'absent'), ('hello\n', 'present'), ('conflict\n', 'present')])
def test_observation_never_infers_execution_history(tmp_path, content, observed):
    store, action = pending(tmp_path)
    if content is not None:
        (tmp_path / 'new.txt').write_text(content)
    assert store.observe_pending_file_actions('task') == ()
    store.finish('one', ExecutionSummary('lost', [], outcome='runtime_error'))
    reopened = TaskStore(store.path)
    facts = reopened.observe_pending_file_actions('task')
    assert facts[0]['observed']['state'] == observed
    assert facts[0]['execution_outcome'] == 'unknown'
    assert reopened.unknown_actions('task')[0]['action_id'] == action
    with sqlite3.connect(store.path) as connection:
        evidence = json.loads(connection.execute('SELECT reconciliation_evidence FROM action_attempts WHERE action_id=?', (action,)).fetchone()[0])
    assert evidence['execution_outcome'] == 'unknown'
    reopened.accept(UserRequest('continue', str(tmp_path)), submission_id='two', task_id='task')
    assert reopened.claim('two')
    with pytest.raises(RuntimeError, match='unknown outcome'):
        reopened.start_action('task', 'two', 'patch', {'diff': DIFF}, str(tmp_path), 'write')


def test_links_never_release_unknown_effect_gate(tmp_path):
    store, _ = pending(tmp_path)
    store.finish('one', ExecutionSummary('lost', [], outcome='runtime_error'))
    outside = tmp_path / 'outside'
    outside.write_text('hello\n')
    target = tmp_path / 'new.txt'
    target.symlink_to(outside)
    assert store.observe_pending_file_actions('task')[0]['observed']['state'] == 'unreadable'
    target.unlink()
    target.hardlink_to(outside)
    assert store.observe_pending_file_actions('task')[0]['observed']['state'] == 'unsupported'
    assert len(store.unknown_actions('task')) == 1


@pytest.mark.parametrize('diff', [DIFF.replace('b/new.txt', 'b/../outside'), DIFF.replace('b/new.txt', '/absolute'), DIFF + DIFF, DIFF.replace('-0,0', '-1,1')])
def test_unsupported_patch_has_no_automatic_recovery(tmp_path, diff):
    assert describe_creation('patch', {'diff': diff}, str(tmp_path)) is None


def test_same_task_claim_is_exclusive_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    for submission in ('one', 'two'):
        store.accept(UserRequest('work', str(tmp_path)), submission_id=submission, task_id='task')
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda name: TaskStore(store.path).claim(name), ('one', 'two')))
    assert sorted(results) == [False, True]
    winner = ('one', 'two')[results.index(True)]
    loser = ('one', 'two')[results.index(False)]
    store.finish(winner, ExecutionSummary('done', []))
    assert store.claim(loser)


def test_v5_upgrade_preserves_unknown_action_without_inventing_descriptor(tmp_path):
    store, action = pending(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute('ALTER TABLE action_attempts DROP COLUMN recovery_descriptor')
        connection.execute('ALTER TABLE action_attempts DROP COLUMN reconciliation_evidence')
        for table in ('action_evidence', 'submission_owners', 'submission_recoveries'):
            connection.execute(f'DROP TABLE {table}')
        connection.execute('PRAGMA user_version=5')
    upgraded = TaskStore(store.path)
    upgraded.finish('one', ExecutionSummary('lost', [], outcome='runtime_error'))
    (tmp_path / 'new.txt').write_text('hello\n')
    assert upgraded.observe_pending_file_actions('task') == ()
    assert upgraded.unknown_actions('task')[0]['action_id'] == action


def test_observations_append_history_without_overwriting_prior_facts(tmp_path):
    store, action = pending(tmp_path)
    store.finish('one', ExecutionSummary('lost', [], outcome='runtime_error'))
    store.observe_pending_file_actions('task')
    (tmp_path / 'new.txt').write_text('hello\n')
    store.observe_pending_file_actions('task')
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT evidence FROM action_evidence WHERE action_id=? AND kind='observation' ORDER BY rowid", (action,)).fetchall()
    assert [json.loads(row[0])['observed']['state'] for row in rows] == ['absent', 'present']
    assert len(store.unknown_actions('task')) == 1


def test_v5_migration_failure_rolls_back_every_new_schema_change(tmp_path, monkeypatch):
    store, _ = pending(tmp_path)
    with sqlite3.connect(store.path) as connection:
        for column in ('recovery_descriptor', 'reconciliation_evidence'):
            connection.execute(f'ALTER TABLE action_attempts DROP COLUMN {column}')
        for table in ('action_evidence', 'submission_owners', 'submission_recoveries'):
            connection.execute(f'DROP TABLE {table}')
        connection.execute('PRAGMA user_version=5')
        before = connection.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall()
    connect = TaskStore._connect
    def deny_last_table(self):
        connection = connect(self)
        connection.set_authorizer(lambda action, table, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and table == 'submission_recoveries'
            else sqlite3.SQLITE_OK)
        return connection
    monkeypatch.setattr(TaskStore, '_connect', deny_last_table)
    with pytest.raises(sqlite3.DatabaseError):
        TaskStore(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 5
        assert connection.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall() == before


def test_each_observation_batch_is_bounded_and_does_not_resolve_actions(tmp_path):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    store.accept(UserRequest('work', str(tmp_path)), submission_id='one', task_id='task')
    store.claim('one')
    for index in range(9):
        diff = DIFF.replace('new.txt', f'new-{index}.txt')
        action = store.start_action('task', 'one', 'patch', {'diff': diff}, str(tmp_path), 'write')
        store.finish_action(action, success=True, error_code=None)
    store.finish('one', ExecutionSummary('stopped', [], outcome='runtime_error'))
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE action_attempts SET state='uncertain'")
    observed = store.observe_pending_file_actions('task')
    assert len(observed) == 8
    assert all(item['execution_outcome'] == 'unknown' for item in observed)
    assert len(store.unknown_actions('task')) == 9
