from __future__ import annotations

import pytest

from saiworks.intent import RequestIntent, RequestIntentClassifier
from saiworks.observations import Observation, ObservationSpec
from saiworks.orchestration.progress import (
    DefaultProgressPolicy,
    ProgressContext,
)
from saiworks.tools.base import ToolContext
from saiworks.tools.observation_adapters import file_observation, query_observation
from saiworks.types import ToolAction, ToolResult


@pytest.mark.parametrize(
    ("prompt", "project_request", "file_changes"),
    [
        ("review the code changes", True, False),
        ("fix the project tests", True, False),
        ("生成标准项目文件夹", True, False),
        ("What makes a successful science project?", False, False),
        ("read CHANGELOG.md", True, False),
        ("create a project", True, False),
        ("add a file", True, False),
        ("禁止修改任何文件，只输出审查摘要", True, False),
        ("请勿编辑文档，仅阅读并总结", True, False),
        ("不要修改或删除任何文件，只做审查", True, False),
        ("请勿编辑、创建或删除文件，只读检查", True, False),
        ("无需修改源码，直接写报告", True, False),
        ("modify the project", True, False),
        ("generate project files", True, False),
        ("upgrade this project", True, False),
        ("patch the code", True, False),
        ("scaffold a project", True, False),
        ("delete the file", True, False),
        ("remove project code", True, False),
        ("rename app.py", True, False),
        ("move the source folder", True, False),
        ("删除这个文件", True, False),
        ("重命名项目目录", True, False),
    ],
)
def test_request_intent_classifier(prompt, project_request, file_changes):
    intent = RequestIntentClassifier().classify(prompt)

    assert intent.project_request is project_request
    assert intent.file_changes is file_changes


def test_request_intent_classifier_accepts_explicit_overrides():
    intent = RequestIntentClassifier().classify(
        "explain this",
        {
            "include_workspace_context": True,
            "requires_file_changes": True,
        },
    )

    assert intent == RequestIntent(project_request=True, file_changes=True)


@pytest.mark.parametrize("tool,first,second,metadata", [
    ("find_files", {"pattern": "*.tsx"}, {"pattern": "*.vue"}, {}),
    ("find_files", {"pattern": "*.py"}, {"pattern": "*.py", "include_hidden": True}, {}),
    ("find_files", {"pattern": "*.py"}, {"pattern": "*.py", "include_ignored": True}, {}),
    ("search_text", {"query": "React"}, {"query": "Vue"}, {}),
    ("read_file", {"path": "a.md"}, {"path": "a.md"}, {"start_line": 2, "end_line": 2}),
])
def test_observation_identity_distinguishes_search_scope_and_actual_read_range(tool, first, second, metadata):
    original_metadata = {"start_line": 1, "end_line": 1} if tool == "read_file" else {}
    original = ToolResult(tool, True, "same output", metadata={"action_arguments": first, **original_metadata})
    current = ToolResult(tool, True, "same output", metadata={"action_arguments": second, **metadata})
    adapter = file_observation if tool == "read_file" else query_observation("pattern", "query", "include_hidden", "include_ignored")
    def key(result):
        action = ToolAction(tool, result.metadata["action_arguments"])
        return Observation.create(action, result, "/repo", spec=adapter(action, result, ToolContext("/repo"))).evidence_key
    assert key(original) != key(current)


def test_request_intent_classifier_does_not_turn_a_write_request_into_an_evidence_contract():
    intent = RequestIntentClassifier().classify("create a project")

    assert intent == RequestIntent(project_request=True, file_changes=False)


def test_diagnostic_request_with_generation_endpoint_does_not_require_a_file_change():
    intent = RequestIntentClassifier().classify(
        "项目中生成周复盘接口 /v1/ai-coach/weekly-reviews/generate 报错，看下什么原因"
    )

    assert intent == RequestIntent(project_request=True, file_changes=False)


def test_progress_policy_triggers_on_first_duplicate_read_for_change_request():
    result = ToolResult(
        name="read_file",
        success=True,
        output="duplicate skipped",
        metadata={
            "duplicate": True,
            "duplicate_count": 1,
            "action_arguments": {"path": "app.py"},
        },
    )

    result.metadata["_observation"] = Observation.create(ToolAction("read_file", {"path": "app.py"}), result, "/repo",
                                                        spec=ObservationSpec(certain=True)).payload()
    signal = DefaultProgressPolicy().evaluate(
        ProgressContext(
            intent=RequestIntent(project_request=True, file_changes=True),
            results=[result],
            previous_results=[result],
            recovery_sent=False,
        )
    )

    assert signal is not None
    assert signal.repeated_actions == [{"path": "app.py"}]


def test_progress_policy_ignores_read_only_requests_and_prior_recovery():
    result = ToolResult(
        name="read_file",
        success=True,
        output="duplicate skipped",
        metadata={"duplicate": True, "duplicate_count": 1},
    )
    policy = DefaultProgressPolicy()

    assert policy.evaluate(
        ProgressContext(
            intent=RequestIntent(project_request=True, file_changes=False),
            results=[result],
            recovery_sent=False,
        )
    ) is None
    assert policy.evaluate(
        ProgressContext(
            intent=RequestIntent(project_request=True, file_changes=True),
            results=[result],
            recovery_sent=True,
        )
    ) is None
