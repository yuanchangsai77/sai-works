from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.runtime import Runtime
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.sessions.tasks import TaskStore
from saiworks.tools.base import SimpleTool
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.types import ExecutionSummary, ModelReply, ToolAction, UserRequest


def runtime_for(tmp_path, model, calls):
    logger = InMemoryLogger(str(tmp_path / 'runs'))
    tools = build_builtin_registry(logger)
    from saiworks.types import ToolResult
    for name, risk in [('before', 'write'), ('effect', 'execute'), ('after', 'write')]:
        tools.register(SimpleTool(name, name, {},
            lambda action, _: calls.append(action.name) or ToolResult(action.name, True, 'ok'), risk_level=risk))
    engine = ExecutionEngine(model, tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger)
    return Runtime(engine, logger=logger, task_store=TaskStore(tmp_path / 'tasks.sqlite3'))


class ApprovalModel:
    calls = 0

    def respond(self, session):
        self.calls += 1
        if session.request.prompt == 'other':
            return ModelReply('Other session complete.', done=True)
        return ModelReply('Execute the steps.', done=True,
                          actions=[ToolAction('before'), ToolAction('effect'), ToolAction('after')])


def test_approval_releases_slot_and_resumes_exact_actions_after_other_session(tmp_path):
    calls, model = [], ApprovalModel()
    runtime = runtime_for(tmp_path, model, calls)
    task = runtime.accept(UserRequest('work', str(tmp_path), {'session_id': 'one'}), submission_id='first')
    waiting = runtime.advance('first')
    assert waiting.state == 'waiting_approval'
    assert calls == ['before']
    access = runtime.grant_task_access(task.task_id)
    snapshot = runtime.snapshot(access)
    assert snapshot.pending_interactions[0].kind == 'approval'
    assert not runtime.is_running
    assert runtime.execute(UserRequest('other', str(tmp_path), {'session_id': 'two'})).outcome == 'completed'
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    run_id = runtime.task_store.continuation('first')['payload']['logger']['run_id']
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    summary = runtime.advance('first')
    assert isinstance(summary, ExecutionSummary)
    assert summary.outcome == 'completed'
    assert calls == ['before', 'effect', 'after']
    assert model.calls == 2  # No extra model call to regenerate the approved action.
    assert runtime.task_store.get_approval(approval.approval_id).state == 'consumed'
    assert runtime.get_submission('first').run_ids == [run_id]


def test_user_answer_survives_new_runtime_and_remains_a_paired_tool_result(tmp_path):
    calls = []

    class Model:
        def respond(self, session):
            if not session.tool_results:
                return ModelReply('Choose a target.', actions=[ToolAction('request_user_input', {
                    'question': 'Which target?', 'options': ['local', 'remote'],
                })])
            assert session.tool_results[-1].output == 'local'
            group = session.model_history[-1]
            assert group[1]['tool_call_id'] == group[0]['tool_calls'][0]['id']
            return ModelReply('Selected local.', done=True)

    runtime = runtime_for(tmp_path, Model(), calls)
    task = runtime.accept(UserRequest('choose', str(tmp_path)), submission_id='choice')
    assert runtime.advance('choice').state == 'waiting_user'
    assert runtime.get_submission('choice').result is None
    runtime.close()
    resumed = runtime_for(tmp_path, Model(), calls)
    access = resumed.grant_task_access(task.task_id)
    interaction, = resumed.snapshot(access).pending_interactions
    observer = resumed.grant_task_control(task.task_id, {'approve'})
    with pytest.raises(PermissionError):
        resumed.reply(observer, interaction.interaction_id, 'local')
    control = resumed.grant_task_control(task.task_id, {'reply'})
    resumed.reply(control, interaction.interaction_id, 'local')
    resumed.reply(control, interaction.interaction_id, 'local')
    with pytest.raises(ValueError):
        resumed.reply(control, interaction.interaction_id, 'remote')
    assert resumed.advance('choice').outcome == 'completed'


def test_waiting_task_blocks_only_its_own_session(tmp_path):
    runtime = runtime_for(tmp_path, ApprovalModel(), [])
    runtime.accept(UserRequest('work', str(tmp_path), {'session_id': 'one'}), submission_id='first')
    runtime.advance('first')
    runtime.accept(UserRequest('other', str(tmp_path), {'session_id': 'one'}), submission_id='second')
    assert runtime.advance('second').state == 'queued'
    assert runtime.execute(UserRequest('other', str(tmp_path), {'session_id': 'two'})).outcome == 'completed'


def test_synchronous_approval_callback_runs_outside_execution_slot(tmp_path):
    calls, entered, release = [], Event(), Event()
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)

    def approve(*_):
        entered.set()
        assert release.wait(5)
        return True

    runtime.configure_interaction(approval_callback=approve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(runtime.execute, UserRequest('work', str(tmp_path), {'session_id': 'one'}))
        assert entered.wait(5)
        assert runtime.execute(UserRequest('other', str(tmp_path), {'session_id': 'two'})).outcome == 'completed'
        release.set()
        assert waiting.result(timeout=5).outcome == 'completed'
    assert calls == ['before', 'effect', 'after']


def test_cancel_before_late_approval_keeps_partial_results_without_dispatch(tmp_path):
    calls, model = [], ApprovalModel()
    runtime = runtime_for(tmp_path, model, calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='cancel')
    runtime.advance('cancel')
    control = runtime.grant_task_control(task.task_id, {'approve', 'cancel'})
    approval, = runtime.pending_approvals(control)
    runtime.cancel(control)
    assert runtime.decide(control, approval.approval_id, 'approve',
                          expected_action_fingerprint=approval.action_fingerprint).state == 'cancelled'
    with pytest.raises(KeyboardInterrupt):
        runtime.advance('cancel')
    assert calls == ['before']
    record = runtime.get_submission('cancel')
    assert record.state == 'cancelled'
    assert [result.name for result in record.result.tool_results] == ['before']


def test_expired_approval_renews_without_replaying_or_requesting_model(tmp_path):
    calls, model = [], ApprovalModel()
    runtime = runtime_for(tmp_path, model, calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='expiry')
    runtime.advance('expiry')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    with sqlite3.connect(runtime.task_store.path) as connection:
        connection.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00'")
    assert runtime.advance('expiry').state == 'waiting_approval'
    renewed, = runtime.pending_approvals(control)
    assert renewed.approval_id != approval.approval_id
    assert model.calls == 1
    assert calls == ['before']


def test_wait_does_not_reset_model_budget(tmp_path):
    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            return ModelReply('choose', actions=[ToolAction('request_user_input', {'question': f'Choice {self.calls}?'})])

    model = Model()
    runtime = runtime_for(tmp_path, model, [])
    runtime.engine.run_budget_policy = type(runtime.engine.run_budget_policy)(max_model_attempts=2)
    task = runtime.accept(UserRequest('choose', str(tmp_path)), submission_id='budget')
    control = runtime.grant_task_control(task.task_id, {'reply'})
    access = runtime.grant_task_access(task.task_id)
    for _ in range(2):
        assert runtime.advance('budget').state == 'waiting_user'
        question, = runtime.snapshot(access).pending_interactions
        runtime.reply(control, question.interaction_id, 'yes')
    summary = runtime.advance('budget')
    assert summary.outcome == 'exhausted'
    assert model.calls == 2
    assert summary.checkpoint.runtime_state['model_attempts'] == '2'


def test_failed_wait_save_never_dispatches_or_publishes_a_wait(tmp_path, monkeypatch):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='save')
    def fail(*_, **__):
        raise OSError('disk full')
    monkeypatch.setattr(runtime.task_store, 'suspend', fail)
    with pytest.raises(OSError, match='disk full'):
        runtime.advance('save')
    assert runtime.get_submission('save').state == 'running'
    assert runtime.task_store.continuation('save') is None
    assert runtime.task_store.snapshot(task.task_id).pending_interactions == ()
    assert calls == ['before']
    assert not runtime.is_running
    assert runtime.logger.run_dir is None


def test_cancel_racing_with_wait_save_preserves_partial_result(tmp_path, monkeypatch):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='race')
    suspend = runtime.task_store.suspend
    def cancel_then_suspend(*args, **kwargs):
        runtime.task_store.accept_cancellation(task.task_id)
        return suspend(*args, **kwargs)
    monkeypatch.setattr(runtime.task_store, 'suspend', cancel_then_suspend)
    with pytest.raises(KeyboardInterrupt):
        runtime.advance('race')
    record = runtime.get_submission('race')
    assert record.state == 'cancelled'
    assert [result.name for result in record.result.tool_results] == ['before']
    assert calls == ['before']
    assert runtime.task_store.continuation('race') is None


def test_cancel_wins_transaction_before_approved_action_start(tmp_path, monkeypatch):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='admit')
    runtime.advance('admit')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    start = runtime.task_store.start_action
    def cancel_then_start(*args, **kwargs):
        runtime.task_store.accept_cancellation(task.task_id)
        return start(*args, **kwargs)
    monkeypatch.setattr(runtime.task_store, 'start_action', cancel_then_start)
    with pytest.raises(KeyboardInterrupt):
        runtime.advance('admit')
    assert calls == ['before']
    assert runtime.get_submission('admit').state == 'cancelled'


def test_waiting_continuation_survives_real_process_exit(tmp_path):
    import subprocess
    import sys
    script = '''
import os, sys
from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.runtime import Runtime
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.sessions.tasks import TaskStore
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.types import ModelReply, ToolAction, UserRequest
class Model:
    def respond(self, session):
        return ModelReply('choose', actions=[ToolAction('request_user_input', {'question': 'Target?'})])
logger = InMemoryLogger(sys.argv[1] + '/runs')
engine = ExecutionEngine(Model(), build_builtin_registry(logger), Guardrails(DefaultPolicy(mode='auto'), logger), logger)
runtime = Runtime(engine, logger=logger, task_store=TaskStore(sys.argv[1] + '/tasks.sqlite3'))
runtime.accept(UserRequest('choose', sys.argv[1]), submission_id='process')
assert runtime.advance('process').state == 'waiting_user'
os._exit(17)
'''
    child = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, timeout=10)
    assert child.returncode == 17, child.stderr

    class Model:
        def respond(self, session):
            assert [item.output for item in session.tool_results] == ['local']
            return ModelReply('Selected local.', done=True)

    runtime = runtime_for(tmp_path, Model(), [])
    task = runtime.get_submission('process')
    question, = runtime.task_store.snapshot(task.task_id).pending_interactions
    control = runtime.grant_task_control(task.task_id, {'reply'})
    runtime.reply(control, question.interaction_id, 'local')
    assert runtime.advance('process').outcome == 'completed'


def test_assembled_runtime_restores_run_capabilities_and_typed_context(tmp_path, monkeypatch):
    from saiworks.app import create_runtime
    from saiworks.context.workspace import GitSummary, WorkspaceSummary
    from saiworks.model.prompt import ModelPromptBuilder
    monkeypatch.setenv('SAIWORKS_MODEL_BASE_URL', '')
    monkeypatch.chdir(tmp_path)
    runtime = create_runtime(mode='auto', workspace_root=tmp_path)

    class Model:
        def respond(self, session):
            if session.request.prompt == 'other':
                return ModelReply('Other task complete.', done=True)
            warehouse = runtime.engine.capability_warehouse
            if not session.tool_results:
                manifest = warehouse.open_toolbox('skill:git-helper')
                warehouse.activate([item.id for item in manifest.items], scope='run')
                session.workspace_summary = WorkspaceSummary(str(tmp_path), git=GitSummary(branch='main'))
                return ModelReply('Choose.', actions=[ToolAction('request_user_input', {'question': 'Proceed?'})])
            assert session.workspace_summary.git.branch == 'main'
            assert 'run' in warehouse.activation_scopes().values()
            ModelPromptBuilder().build_messages(session)
            return ModelReply('Complete.', done=True)

    runtime.engine.model = Model()
    task = runtime.accept(UserRequest('choose', str(tmp_path), {'session_id': 'one'}), submission_id='assembled')
    assert runtime.advance('assembled').state == 'waiting_user'
    runtime.execute(UserRequest('other', str(tmp_path), {'session_id': 'two'}))
    question, = runtime.task_store.snapshot(task.task_id).pending_interactions
    control = runtime.grant_task_control(task.task_id, {'reply'})
    runtime.reply(control, question.interaction_id, 'yes')
    assert runtime.advance('assembled').outcome == 'completed'
    runtime.close()


def test_synchronous_user_question_uses_saved_wait_and_releases_slot(tmp_path):
    class Model:
        def respond(self, session):
            if not session.tool_results:
                return ModelReply('Choose.', actions=[ToolAction('request_user_input', {'question': 'Target?', 'options': ['local']})])
            return ModelReply('Selected ' + session.tool_results[-1].output, done=True)

    runtime = runtime_for(tmp_path, Model(), [])
    def answer(question, options):
        assert question == 'Target?'
        assert options == ['local']
        assert not runtime.is_running
        assert runtime.get_submission('synchronous').state == 'waiting_user'
        return 'custom target'
    runtime.configure_interaction(user_input_callback=answer)
    summary = runtime.execute(UserRequest('choose', str(tmp_path)), submission_id='synchronous')
    assert summary.outcome == 'completed'
    assert summary.final_message == 'Selected custom target'


def test_reply_is_bound_to_task_even_with_reply_permission(tmp_path):
    class Model:
        def respond(self, session):
            return ModelReply('Choose.', actions=[ToolAction('request_user_input', {'question': 'Target?'})])
    runtime = runtime_for(tmp_path, Model(), [])
    first = runtime.accept(UserRequest('choose', str(tmp_path)), submission_id='first')
    runtime.advance('first')
    other = runtime.accept(UserRequest('other', str(tmp_path)), submission_id='other')
    question, = runtime.task_store.snapshot(first.task_id).pending_interactions
    control = runtime.grant_task_control(other.task_id, {'reply'})
    with pytest.raises(PermissionError):
        runtime.reply(control, question.interaction_id, 'local')


def test_desktop_cancel_of_waiting_submission_does_not_cancel_other_active_task(tmp_path):
    from saiworks.interaction.desktop import DesktopController
    from saiworks.types import ToolResult
    entered, release = Event(), Event()

    class Model(ApprovalModel):
        def respond(self, session):
            if session.request.prompt == 'other':
                return ModelReply('Other task.', done=True, actions=[ToolAction('slow_read')])
            return super().respond(session)

    runtime = runtime_for(tmp_path, Model(), [])
    def read(*_):
        entered.set()
        assert release.wait(5)
        return ToolResult('slow_read', True, 'ok')
    runtime.engine.tools.register(SimpleTool('slow_read', 'slow read', {}, read))
    runtime.accept(UserRequest('work', str(tmp_path), {'session_id': 'one'}), submission_id='waiting')
    runtime.advance('waiting')
    controller = DesktopController(runtime)
    controller._active_submission_id = 'waiting'
    with ThreadPoolExecutor(max_workers=1) as pool:
        other = pool.submit(runtime.execute, UserRequest('other', str(tmp_path), {'session_id': 'two'}))
        assert entered.wait(5)
        controller.cancel_current()
        release.set()
        assert other.result(timeout=5).outcome == 'completed'
    with pytest.raises(KeyboardInterrupt):
        runtime.advance('waiting')


def test_v8_wait_schema_migration_rolls_back_as_a_unit(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite3'
    store = TaskStore(path)
    store.accept(UserRequest('legacy', str(tmp_path)), submission_id='legacy')
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE run_continuations')
        connection.execute('DROP TABLE task_interactions')
        connection.execute('PRAGMA user_version=8')
    original = TaskStore.create_interaction_schema
    def fail(connection):
        original(connection)
        raise OSError('migration interrupted')
    monkeypatch.setattr(TaskStore, 'create_interaction_schema', staticmethod(fail))
    with pytest.raises(OSError):
        TaskStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 8
        assert connection.execute("SELECT name FROM sqlite_master WHERE name IN ('task_interactions','run_continuations')").fetchall() == []
        assert connection.execute('SELECT state FROM tasks').fetchone()[0] == 'queued'
    monkeypatch.setattr(TaskStore, 'create_interaction_schema', staticmethod(original))
    upgraded = TaskStore(path)
    assert upgraded.get_submission('legacy').state == 'queued'


def test_wait_time_does_not_consume_execution_budget(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('saiworks.orchestration.continuation.time.monotonic', lambda: clock[0])
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    runtime.engine.run_budget_policy = type(runtime.engine.run_budget_policy)(max_run_seconds=5)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='elapsed')
    runtime.advance('elapsed')
    clock[0] += 10_000
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    assert runtime.advance('elapsed').outcome == 'completed'
    assert calls == ['before', 'effect', 'after']


def test_two_runtime_instances_cannot_consume_one_approval_twice(tmp_path):
    from saiworks.types import ToolResult
    calls, entered, release = [], Event(), Event()
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='exclusive')
    runtime.advance('exclusive')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    other = runtime_for(tmp_path, ApprovalModel(), calls)
    def effect(*_):
        calls.append('effect')
        entered.set()
        assert release.wait(5)
        return ToolResult('effect', True, 'ok')
    runtime.engine.tools._tools['effect'].handler = effect
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(runtime.advance, 'exclusive')
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError, match='will not be replayed'):
                other.advance('exclusive')
        finally:
            release.set()
        assert worker.result(timeout=5).outcome == 'completed'
    assert calls == ['before', 'effect', 'after']


def test_wait_does_not_make_new_capabilities_visible_in_the_original_turn(tmp_path):
    from saiworks.capabilities.local_source import LocalToolCapability, LocalToolboxSource, LocalToolboxSpec
    from saiworks.capabilities.warehouse import CapabilityWarehouse
    from saiworks.types import ToolResult
    calls = []

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if self.calls == 1:
                assert 'deferred' not in {item.name for item in session.available_tools}
                warehouse.open_toolbox('local:deferred')
                warehouse.activate(['local:deferred:tool'], scope='run')
                return ModelReply('Choose.', actions=[ToolAction('request_user_input', {'question': 'Proceed?'}), ToolAction('deferred')])
            assert session.tool_results[-1].error_code == 'tool_not_visible_this_turn'
            assert calls == []
            return ModelReply('Use it in this new turn.', done=True, actions=[ToolAction('deferred')])

    model = Model()
    runtime = runtime_for(tmp_path, model, [])
    deferred = SimpleTool('deferred', 'deferred', {}, lambda *_: calls.append('executed') or ToolResult('deferred', True, 'ok'))
    source = LocalToolboxSource((LocalToolboxSpec('local:deferred', 'deferred', 'local', 'deferred tool',
        tools=(LocalToolCapability('local:deferred:tool', deferred),)),))
    warehouse = CapabilityWarehouse([source], runtime.engine.tools, logger=runtime.logger)
    runtime.engine.capability_warehouse = warehouse
    task = runtime.accept(UserRequest('choose', str(tmp_path)), submission_id='visibility')
    runtime.advance('visibility')
    question, = runtime.task_store.snapshot(task.task_id).pending_interactions
    control = runtime.grant_task_control(task.task_id, {'reply'})
    runtime.reply(control, question.interaction_id, 'yes')
    assert runtime.advance('visibility').outcome == 'completed'
    assert calls == ['executed']
    assert model.calls == 2


@pytest.mark.parametrize('finished', [False, True])
def test_advance_revalidates_delegation_before_claiming_parent_submission(tmp_path, finished):
    from saiworks.orchestration.subagent_runner import _issue_subagent_grant
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='parent')
    if finished:
        assert runtime.task_store.claim('parent')
        runtime.task_store.finish('parent', ExecutionSummary('Private parent result.', []))
    runtime.subagent_grant = _issue_subagent_grant(cluster_id='cluster', session_id='child',
        parent_session_id='parent-session', attempt=1, workspace_root=str(tmp_path),
        allowed_effects=['read'], allowed_resources=['safe'])
    before = runtime.task_store.snapshot(task.task_id).revision
    with pytest.raises(RuntimeError, match='execution identity'):
        runtime.advance('parent')
    assert runtime.get_submission('parent').state == ('finished' if finished else 'queued')
    assert runtime.task_store.snapshot(task.task_id).revision == before
    assert calls == []


def test_repeated_cleanup_failure_preserves_answer_and_continuation(tmp_path, monkeypatch):
    calls = []
    class Model:
        def respond(self, session):
            if not session.tool_results:
                return ModelReply('Choose.', actions=[ToolAction('before'), ToolAction('request_user_input', {'question': 'Proceed?'})])
            assert session.tool_results[-1].output == 'yes'
            return ModelReply('Continued.', done=True, actions=[ToolAction('after')])
    runtime = runtime_for(tmp_path, Model(), calls)
    task = runtime.accept(UserRequest('choose', str(tmp_path)), submission_id='cleanup')
    cleanup = runtime.close_tool_state
    def fail():
        raise OSError('cleanup failed')
    monkeypatch.setattr(runtime, 'close_tool_state', fail)
    with pytest.raises(OSError, match='cleanup failed'):
        runtime.advance('cleanup')
    assert runtime.get_submission('cleanup').state == 'waiting_user'
    question, = runtime.task_store.snapshot(task.task_id).pending_interactions
    control = runtime.grant_task_control(task.task_id, {'reply'})
    runtime.reply(control, question.interaction_id, 'yes')
    for _ in range(2):
        with pytest.raises(OSError, match='cleanup failed'):
            runtime.advance('cleanup')
        record = runtime.get_submission('cleanup')
        assert record.state == 'queued'
        assert record.result is None
        saved = runtime.task_store.continuation('cleanup')
        assert saved['interaction']['answer'] == 'yes'
        assert saved['payload']['session']['tool_results'][0]['name'] == 'before'
    monkeypatch.setattr(runtime, 'close_tool_state', cleanup)
    assert runtime.advance('cleanup').outcome == 'completed'
    assert calls == ['before', 'after']


def test_shell_reset_restores_directory_and_returns_pending_actions_to_model(tmp_path):
    (tmp_path / 'sub').mkdir()
    class Model:
        calls = 0
        def respond(self, session):
            self.calls += 1
            if self.calls == 1:
                command = 'printf x >> once.txt; cd sub; export SAI_WAIT_VALUE=original'
            elif self.calls == 2:
                command = 'printf "%s" "$SAI_WAIT_VALUE" > result.txt'
            else:
                assert self.calls == 3
                assert 'variables and functions were not restored' in session.checkpoint.runtime_state['shell_recovery']
                assert session.checkpoint.runtime_state['shell_cwd'] == str(tmp_path / 'sub')
                assert session.tool_results[-1].error_code == 'progress_required'
                assert not (tmp_path / 'result.txt').exists()
                assert not (tmp_path / 'sub' / 'result.txt').exists()
                command = 'printf restored > result.txt'
            return ModelReply('Run command.', actions=[ToolAction('shell_exec', {'command': command})], done=self.calls > 1)
    model = Model()
    runtime = runtime_for(tmp_path, model, [])
    runtime.configure_interaction(approval_callback=lambda *_: True)
    summary = runtime.execute(UserRequest('work', str(tmp_path), {'session_id': 'shell'}))
    assert summary.outcome == 'completed'
    assert (tmp_path / 'sub' / 'result.txt').read_text() == 'restored'
    assert not (tmp_path / 'result.txt').exists()
    assert (tmp_path / 'once.txt').read_text() == 'x'
    assert model.calls == 3
    runtime.close()


def test_cleanup_retry_keeps_the_existing_approval(tmp_path, monkeypatch):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='approved-cleanup')
    cleanup = runtime.close_tool_state
    def fail():
        raise OSError('cleanup failed')
    monkeypatch.setattr(runtime, 'close_tool_state', fail)
    with pytest.raises(OSError):
        runtime.advance('approved-cleanup')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    with pytest.raises(OSError):
        runtime.advance('approved-cleanup')
    assert runtime.task_store.get_approval(approval.approval_id).state == 'approved'
    assert runtime.task_store.continuation('approved-cleanup') is not None
    monkeypatch.setattr(runtime, 'close_tool_state', cleanup)
    assert runtime.advance('approved-cleanup').outcome == 'completed'
    assert calls == ['before', 'effect', 'after']
    assert runtime.task_store.get_approval(approval.approval_id).state == 'consumed'


def test_saved_shell_directory_survives_a_new_runtime_without_replaying_setup(tmp_path):
    (tmp_path / 'sub').mkdir()
    class FirstModel:
        def respond(self, session):
            return ModelReply('Set up and ask.', actions=[
                ToolAction('shell_exec', {'command': 'printf x >> once.txt; cd sub'}),
                ToolAction('request_user_input', {'question': 'Continue?'}),
            ])
    runtime = runtime_for(tmp_path, FirstModel(), [])
    task = runtime.accept(UserRequest('work', str(tmp_path), {'session_id': 'shell'}), submission_id='shell-restart')
    assert runtime.advance('shell-restart').state == 'waiting_approval'
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    assert runtime.advance('shell-restart').state == 'waiting_user'
    runtime.close()

    class ResumedModel:
        def respond(self, session):
            assert session.checkpoint.runtime_state['shell_cwd'] == str(tmp_path / 'sub')
            assert 'shell_recovery' in session.checkpoint.runtime_state
            return ModelReply('Write in the verified directory.', done=True,
                              actions=[ToolAction('shell_exec', {'command': 'pwd > where.txt'})])
    resumed = runtime_for(tmp_path, ResumedModel(), [])
    question, = resumed.task_store.snapshot(task.task_id).pending_interactions
    control = resumed.grant_task_control(task.task_id, {'reply', 'approve'})
    resumed.reply(control, question.interaction_id, 'yes')
    assert resumed.advance('shell-restart').state == 'waiting_approval'
    approval, = resumed.pending_approvals(control)
    resumed.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    assert resumed.advance('shell-restart').outcome == 'completed'
    assert (tmp_path / 'sub' / 'where.txt').read_text().strip() == str(tmp_path / 'sub')
    assert not (tmp_path / 'where.txt').exists()
    assert (tmp_path / 'once.txt').read_text() == 'x'
    resumed.close()


def test_engine_preparation_failure_does_not_finalize_a_saved_wait(tmp_path):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='loader')
    runtime.advance('loader')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    class Loader:
        def load_context(self, *_):
            raise OSError('context unavailable')
    runtime.engine.context_loaders.append(Loader())
    with pytest.raises(OSError, match='context unavailable'):
        runtime.advance('loader')
    assert runtime.get_submission('loader').state == 'queued'
    assert runtime.task_store.continuation('loader') is not None
    assert not any(event.name == 'run.finish' for event in runtime.logger.events)
    runtime.engine.context_loaders.clear()
    assert runtime.advance('loader').outcome == 'completed'
    assert calls == ['before', 'effect', 'after']


def test_saved_workspace_grant_is_dropped_when_its_target_changes(tmp_path):
    from saiworks.orchestration.continuation import restore
    runtime = runtime_for(tmp_path, ApprovalModel(), [])
    runtime.accept(UserRequest('work', str(tmp_path)), submission_id='saved-path')
    runtime.advance('saved-path')
    payload = runtime.task_store.continuation('saved-path')['payload']
    original = tmp_path / 'approved'
    original.mkdir()
    target = tmp_path / 'unapproved'
    target.mkdir()
    payload['permissions'].append({'capability': 'workspace_path', 'path': str(original), 'scope': 'session'})
    payload['session']['workspace_state']['approved_roots'].append(str(original))
    original.rmdir()
    original.symlink_to(target, target_is_directory=True)
    session, _, permissions, *_ = restore(payload, Event())
    assert str(target) not in permissions.workspace_roots()
    assert str(original) not in permissions.workspace_roots()
    assert str(original) not in session.workspace_state.approved_roots


def test_workspace_approval_does_not_follow_a_replaced_symlink(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    approved = tmp_path / 'public.txt'
    private = tmp_path / 'private.txt'
    approved.write_text('public')
    private.write_text('private')

    class Model:
        def respond(self, session):
            return ModelReply('Read.', done=True, actions=[ToolAction('read_file', {'path': str(approved)})])

    runtime = runtime_for(workspace, Model(), [])
    task = runtime.accept(UserRequest('read', str(workspace)), submission_id='path-change')
    assert runtime.advance('path-change').state == 'waiting_approval'
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)
    approved.unlink()
    approved.symlink_to(private)
    assert runtime.advance('path-change').state == 'waiting_approval'
    replacement, = runtime.pending_approvals(control)
    assert replacement.arguments['path'] == str(private)
    assert replacement.approval_id != approval.approval_id
    assert not runtime.task_store.continuation('path-change')['payload']['session']['tool_results']


@pytest.mark.parametrize('budget_exhausted', [False, True])
def test_preparation_interrupt_preserves_completed_results(tmp_path, budget_exhausted):
    calls = []
    runtime = runtime_for(tmp_path, ApprovalModel(), calls)
    task = runtime.accept(UserRequest('work', str(tmp_path)), submission_id='interrupt-loader')
    runtime.advance('interrupt-loader')
    control = runtime.grant_task_control(task.task_id, {'approve'})
    approval, = runtime.pending_approvals(control)
    runtime.decide(control, approval.approval_id, 'approve', expected_action_fingerprint=approval.action_fingerprint)

    class Loader:
        def load_context(self, *_):
            if budget_exhausted:
                from saiworks.orchestration.model_turn import RunBudgetReached
                from saiworks.types import RuntimeBlocker
                raise RunBudgetReached(RuntimeBlocker(error_code='run_budget_exceeded', summary='Budget exhausted'))
            raise KeyboardInterrupt

    runtime.engine.context_loaders.append(Loader())
    if budget_exhausted:
        runtime.advance('interrupt-loader')
    else:
        with pytest.raises(KeyboardInterrupt):
            runtime.advance('interrupt-loader')
    record = runtime.get_submission('interrupt-loader')
    if not budget_exhausted:
        assert record.state == 'cancelled'
    else:
        assert record.result.outcome == 'exhausted'
    assert [result.name for result in record.result.tool_results] == ['before']
    assert calls == ['before']
