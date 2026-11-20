"""Interaction adapters around Runtime-owned durable waits and execution slots."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .orchestration.continuation import WaitRequested
from .sessions.tasks import TaskRecord, fingerprint_action
from .types import ExecutionSummary, ToolAction, UserRequest


class RuntimeInteractions:
    def __init__(self, runtime):
        self.runtime = runtime

    def execute(self, request: UserRequest, *, background: bool = False,
                submission_id: str | None = None, _session=None,
                _defer_terminal_notification: bool = False) -> ExecutionSummary:
        runtime = self.runtime
        identity = submission_id or uuid4().hex
        while True:
            result = runtime._execute_segment(request, background=background, submission_id=identity,
                                              _session=_session, _defer_terminal_notification=_defer_terminal_notification)
            if not isinstance(result, TaskRecord):
                return result
            with runtime._condition:
                runtime._synchronous_waits.add(identity)
            try:
                self.service_wait(result)
                if result.state in {'queued', 'running'}:
                    with runtime._condition:
                        runtime._condition.wait(timeout=0.1)
                while not runtime.task_store.can_advance(identity):
                    with runtime._condition:
                        if runtime._closed:
                            raise RuntimeError('Runtime is closed; the waiting task remains saved')
                        runtime._condition.wait(timeout=0.2)
            except KeyboardInterrupt:
                runtime.cancel_submission(identity)
                # Let the engine produce the normal interrupted terminal projection.
            finally:
                with runtime._condition:
                    runtime._synchronous_waits.discard(identity)

    def service_wait(self, record):
        runtime = self.runtime
        saved = runtime.task_store.continuation(record.submission_id)
        if saved is None:
            return
        interaction = saved['interaction']
        if interaction['kind'] == 'approval':
            approval = runtime.task_store.get_approval(interaction['interaction_id'])
            callback = runtime.engine.approval_callback
            if approval.state != 'pending' or callback is None:
                return
            approved = callback(ToolAction(approval.action_name, approval.arguments), interaction['question'])
            control = runtime.grant_task_control(record.task_id, {'approve'}, actor='synchronous-adapter')
            try:
                runtime.decide(control, approval.approval_id, 'approve' if approved else 'deny',
                            expected_action_fingerprint=approval.action_fingerprint)
            finally:
                runtime.revoke_task_control(control)
        elif runtime.user_input_callback is not None and interaction['state'] == 'pending':
            answer = runtime.user_input_callback(interaction['question'], json.loads(interaction['options']))
            control = runtime.grant_task_control(record.task_id, {'reply', 'cancel'}, actor='synchronous-adapter')
            try:
                if answer is None:
                    runtime.cancel(control)
                else:
                    runtime.reply(control, interaction['interaction_id'], answer)
            finally:
                runtime.revoke_task_control(control)

    def bind(self, context, record, saved):
        runtime = self.runtime
        context.pending_approval_action = None
        if runtime.subagent_grant is not None:
            context.interaction_handler = None
            return
        pending = saved['interaction'] if saved else None
        if pending and pending['kind'] == 'approval':
            approval = runtime.task_store.get_approval(pending['interaction_id'])
            context.pending_approval_action = (approval.action_name, approval.arguments,
                                               approval.workspace_root, approval.required_scope)
            if (not context.cancellation.is_set() and approval.action_name == 'workspace_access' and approval.state == 'approved'
                    and datetime.fromisoformat(approval.expires_at) > datetime.now(timezone.utc)
                    and str(Path(approval.arguments['path']).resolve(strict=False)) == approval.arguments['path']):
                runtime.task_store.consume_approval(approval.approval_id, record.task_id, record.submission_id,
                    approval.action_name, approval.arguments, approval.workspace_root, approval.required_scope)
                path = approval.arguments['path']
                saved['payload']['permissions'].append({'capability': 'workspace_path', 'path': path, 'scope': 'session'})
                saved['payload']['session']['workspace_state']['approved_roots'].append(path)
                pending = None
                context.pending_approval_action = None

        def interact(kind, action, question, cwd, scope='', options=()):
            nonlocal pending
            if pending and pending['kind'] == kind:
                if kind == 'user':
                    expected = saved['payload']['remaining_actions'][0]
                    if pending['state'] == 'answered' and expected == {'name': action.name, 'arguments': action.arguments}:
                        answer = pending['answer']
                        pending = None
                        return answer
                else:
                    approval = runtime.task_store.get_approval(pending['interaction_id'])
                    fingerprint = fingerprint_action(record.task_id, record.submission_id, action.name, action.arguments, cwd, scope)
                    if approval.action_fingerprint == fingerprint:
                        context.pending_approval_action = None
                        if approval.state == 'denied':
                            pending = None
                            return False
                        if approval.state == 'approved' and datetime.fromisoformat(approval.expires_at) > datetime.now(timezone.utc):
                            context.action_journal.approval_id = approval.approval_id
                            pending = None
                            return True
            if kind == 'approval':
                approval = runtime.task_store.approval_for_action(record.task_id, record.submission_id, action, cwd, scope)
                if approval is not None:
                    if approval.state == 'denied':
                        return False
                    if approval.state == 'approved' and datetime.fromisoformat(approval.expires_at) > datetime.now(timezone.utc):
                        context.action_journal.approval_id = approval.approval_id
                        return True
            raise WaitRequested(kind, action, question, cwd, scope, options)

        context.interaction_handler = interact
