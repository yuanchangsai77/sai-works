import json
import os
import sqlite3
import subprocess
import sys
import pytest
from saiworks.sessions import process_owner
from saiworks.sessions.tasks import TaskStore
from saiworks.types import UserRequest


@pytest.mark.parametrize('admitted', [False, True])
def test_real_process_crash_only_reclaims_submission_without_effectful_action(tmp_path, admitted):
    if process_owner.current_owner() is None:
        pytest.skip('Requires verifiable local Linux process identity')
    path = tmp_path / 'tasks.sqlite3'
    script = '''
import os, sys
from saiworks.sessions.tasks import TaskStore
from saiworks.types import UserRequest
store = TaskStore(sys.argv[1])
store.accept(UserRequest('work', sys.argv[2]), submission_id='old', task_id='task')
assert store.claim('old')
if sys.argv[3] == 'yes':
    store.start_action('task', 'old', 'external', {}, sys.argv[2], 'execute')
os._exit(17)
'''
    child = subprocess.run([sys.executable, '-c', script, str(path), str(tmp_path), 'yes' if admitted else 'no'], capture_output=True, timeout=10)
    assert child.returncode == 17, child.stderr
    store = TaskStore(path)
    if admitted:
        with pytest.raises(RuntimeError, match='explicit recovery'):
            store.accept(UserRequest('continue', str(tmp_path)), submission_id='new', task_id='task')
        assert store.get_submission('old').state == 'running'
    else:
        store.accept(UserRequest('continue', str(tmp_path)), submission_id='new', task_id='task')
        assert store.get_submission('old').state == 'finished'
        assert store.claim('new')
        with sqlite3.connect(path) as connection:
            evidence = json.loads(connection.execute('SELECT evidence FROM submission_recoveries').fetchone()[0])
        assert evidence['owner_stopped'] is True
        assert evidence['effectful_actions_admitted'] is False


def test_live_owner_is_never_reclaimed(tmp_path):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    store.accept(UserRequest('work', str(tmp_path)), submission_id='old', task_id='task')
    assert store.claim('old')
    with pytest.raises(RuntimeError, match='explicit recovery'):
        TaskStore(store.path).accept(UserRequest('continue', str(tmp_path)), submission_id='new', task_id='task')


def test_identity_checks_refuse_unknown_and_foreign_information(monkeypatch):
    monkeypatch.setattr(process_owner, '_host', lambda: ('machine', 'boot'))
    monkeypatch.setattr(process_owner, '_process', lambda _: None)
    monkeypatch.setattr(process_owner, '_namespace', lambda: 'pid-namespace')
    valid = {'machine': 'machine', 'boot': 'boot', 'pid': 123, 'start': '100', 'pid_namespace': 'pid-namespace', 'uid': os.getuid()}
    for owner in [None, {}, {'machine': 'machine'}, {**valid, 'machine': 'foreign'}, valid]:
        assert not process_owner.owner_stopped(owner)
    monkeypatch.setattr(process_owner, '_process', lambda _: ('S', '200'))
    assert process_owner.owner_stopped(valid)
    assert not process_owner.owner_stopped({**valid, 'pid_namespace': 'foreign-namespace'})
    assert not process_owner.owner_stopped({**valid, 'uid': os.getuid() + 1})
    assert process_owner.owner_stopped({**valid, 'boot': 'old-boot'})
