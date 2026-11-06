from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]
check_documentation = runpy.run_path(str(ROOT / 'scripts/check_docs.py'))['check_documentation']


def workspace(tmp_path):
    documents = {
        'README.md': '# Project\n[文档](docs/README.md)\n',
        'docs/README.md': '# 文档\n[路线](roadmap.md)\n[设计](design/README.md)\n',
        'docs/roadmap.md': (
            '# 工作路线\n'
            '| 需求核实 | 待核实 | 核实需求 | 无 | 有外部判据 | [设计](design/feature.md) |\n'
        ),
        'docs/design/README.md': '# 设计\n[专题](feature.md)\n',
        'docs/design/feature.md': '# 专题\n状态：目标设计，不表示已经实现。\n',
    }
    for name, content in documents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return tmp_path


def append(root, name, text):
    path = root / name
    path.write_text(path.read_text(encoding='utf-8') + text, encoding='utf-8')


def test_repository_documentation_obeys_policy():
    assert check_documentation(ROOT) == []


def test_minimal_indexed_documentation_is_valid(tmp_path):
    assert check_documentation(workspace(tmp_path)) == []


@pytest.mark.parametrize('target', ['missing.md', 'feature.md#missing'])
def test_broken_file_and_anchor_are_rejected(tmp_path, target):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', f'[失效]({target})\n')
    assert any('不存在' in error for error in check_documentation(root))


@pytest.mark.parametrize('target', ['http://[invalid', 'missing%00.md'])
def test_malformed_links_report_diagnostics_instead_of_crashing(tmp_path, target):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', f'[错误链接]({target})\n')
    errors = check_documentation(root)
    assert any('docs/design/feature.md:' in error and '无效' in error for error in errors)


def test_unicode_duplicate_and_inline_code_headings_have_stable_anchors(tmp_path):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', (
        '## 观察与 `read_file`\n## 观察与 `read_file`\n'
        '[一](#观察与-read_file) [二](#观察与-read_file-1)\n'
    ))
    assert check_documentation(root) == []


def test_reachable_document_still_requires_its_own_section_index(tmp_path):
    root = workspace(tmp_path)
    (root / 'docs/design/README.md').write_text('# 设计\n', encoding='utf-8')
    errors = check_documentation(root)
    assert any('所属索引缺少入口' in error for error in errors)


def test_orphaned_document_is_rejected(tmp_path):
    root = workspace(tmp_path)
    (root / 'docs/design/orphan.md').write_text('# 孤立\n状态：目标设计\n', encoding='utf-8')
    assert any('孤立材料' in error for error in check_documentation(root))


@pytest.mark.parametrize('name', ['todo.md', 'runtime-progress.md', 'runtime-tracking.md', 'shell-migration.md'])
def test_parallel_progress_documents_are_rejected(tmp_path, name):
    root = workspace(tmp_path)
    (root / 'docs/design' / name).write_text('# 进度\n状态：设计\n', encoding='utf-8')
    append(root, 'docs/design/README.md', f'[新文档]({name})\n')
    assert any('禁止新增' in error for error in check_documentation(root))


@pytest.mark.parametrize('text', [
    '## 跟踪清单\n',
    '- [ ] 另做一个任务\n',
    '| 交付验证 | 待实施 | 其他工作 | 无 | 判据 | [依据](feature.md) |\n',
])
def test_task_queues_cannot_be_duplicated_in_designs(tmp_path, text):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', text)
    assert any('只' in error and 'roadmap.md' in error for error in check_documentation(root))


@pytest.mark.parametrize('row', [
    '| 需求核实 | 已完成 | 工作 | 无 | 判据 | [依据](design/feature.md) |\n',
    '| 需求核实 | 待核实 | 工作 | 无 | | [依据](design/feature.md) |\n',
    '| 需求核实 | 待核实 | 工作 | 无 | 判据 | 没有来源 |\n',
])
def test_work_items_require_known_state_acceptance_and_evidence(tmp_path, row):
    root = workspace(tmp_path)
    (root / 'docs/roadmap.md').write_text('# 工作路线\n' + row, encoding='utf-8')
    assert check_documentation(root)


def test_work_dependencies_must_exist_and_be_acyclic(tmp_path):
    root = workspace(tmp_path)
    roadmap = root / 'docs/roadmap.md'
    original = roadmap.read_text(encoding='utf-8')
    roadmap.write_text(original.replace('| 无 |', '| 「交付验证」 |'), encoding='utf-8')
    assert any('工作依赖不存在' in error for error in check_documentation(root))
    append(root, 'docs/roadmap.md', '| 交付验证 | 待实施 | 工作 | 「需求核实」 | 判据 | [依据](design/feature.md) |\n')
    assert any('循环' in error for error in check_documentation(root))


def test_code_examples_are_ignored_but_adjacent_repository_links_are_rejected(tmp_path):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', (
        '```markdown\n[示例](missing.md)\n- [ ] 示例待办\n```\n'
        '`[行内示例](also-missing.md)`\n[外部](https://example.org/unknown)\n'
    ))
    assert check_documentation(root) == []
    append(root, 'docs/design/feature.md', '[相邻仓库](../../../sibling/README.md)\n')
    assert any('越出仓库' in error for error in check_documentation(root))


def test_historical_evidence_does_not_become_a_current_queue(tmp_path):
    root = workspace(tmp_path)
    archive = root / 'docs/archive'
    archive.mkdir()
    (archive / 'README.md').write_text('# 历史\n[旧记录](old-audit.md)\n', encoding='utf-8')
    (archive / 'old-audit.md').write_text('# 当时记录\n- [ ] 当时未完成\n', encoding='utf-8')
    append(root, 'docs/README.md', '[历史](archive/README.md)\n')
    assert check_documentation(root) == []


def test_unclassified_root_document_and_unmarked_design_are_rejected(tmp_path):
    root = workspace(tmp_path)
    (root / 'docs/notes.md').write_text('# 临时笔记\n', encoding='utf-8')
    (root / 'docs/design/feature.md').write_text('# 专题\n', encoding='utf-8')
    errors = check_documentation(root)
    assert any('未知文档位置' in error for error in errors)
    assert any('声明目标或设计状态' in error for error in errors)


@pytest.mark.parametrize('text', [
    'D1 已实现。\n',
    '阶段 E 尚未实施。\n',
    'Phase 2：继续实现。\n',
    '桌面 V0.1a 的目标。\n',
    'B/C 的后续。\n',
    '跨 D/E 阶段。\n',
])
def test_current_documents_cannot_reintroduce_stage_aliases(tmp_path, text):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', text)
    assert any('使用中文能力名称' in error for error in check_documentation(root))


def test_real_versions_code_identifiers_and_historical_aliases_are_preserved(tmp_path):
    root = workspace(tmp_path)
    append(root, 'docs/design/feature.md', (
        '发布快照 v0.1；`schema_version=2`；字段 `D1` 保留原名。\n'
        'A/B 对照实验须以真实证据验收。\n'
        '```mermaid\nD1 --> E\n```\n'
    ))
    assert check_documentation(root) == []


@pytest.mark.parametrize('name', ['Feature_Design.md', 'feature-v1.md', 'phase-d.md'])
def test_current_filenames_use_stable_topics(tmp_path, name):
    root = workspace(tmp_path)
    (root / 'docs/design' / name).write_text('# 功能设计\n状态：目标设计\n', encoding='utf-8')
    append(root, 'docs/design/README.md', f'[设计]({name})\n')
    assert any('文件名' in error for error in check_documentation(root))


def test_current_document_titles_explain_the_subject_in_chinese(tmp_path):
    root = workspace(tmp_path)
    (root / 'docs/design/feature.md').write_text('# Execution Runtime\n状态：目标设计\n', encoding='utf-8')
    assert any('中文主题名称' in error for error in check_documentation(root))


@pytest.mark.parametrize('row', [
    '| X01 | 待核实 | 核实需求 | 无 | 判据 | [依据](design/feature.md) |\n',
    '| 需求核实 | 待核实 | 核实需求 | X01 | 判据 | [依据](design/feature.md) |\n',
])
def test_work_and_dependencies_use_complete_names(tmp_path, row):
    root = workspace(tmp_path)
    (root / 'docs/roadmap.md').write_text('# 工作路线\n' + row, encoding='utf-8')
    assert any('编号' in error for error in check_documentation(root))


def test_work_names_are_unique(tmp_path):
    root = workspace(tmp_path)
    append(root, 'docs/roadmap.md', '| 需求核实 | 待实施 | 再做一次 | 无 | 判据 | [依据](design/feature.md) |\n')
    assert any('重复工作名称' in error for error in check_documentation(root))


def mechanism_workspace(tmp_path, links):
    root = workspace(tmp_path)
    (root / 'docs/runtime').mkdir()
    (root / 'src').mkdir()
    (root / 'tests').mkdir()
    (root / 'src/example.py').write_text('# implementation entry\n', encoding='utf-8')
    (root / 'tests/test_example.py').write_text('# regression entry\n', encoding='utf-8')
    (root / 'docs/runtime/README.md').write_text('# 机制\n[能力](example.md)\n', encoding='utf-8')
    (root / 'docs/runtime/example.md').write_text('# 当前能力\n' + links, encoding='utf-8')
    append(root, 'docs/README.md', '[机制](runtime/README.md)\n')
    return root


@pytest.mark.parametrize(('links', 'missing'), [
    ('', '源码'),
    ('[实现](../../src/example.py)\n', '回归入口'),
    ('[回归](../../tests/test_example.py)\n', '源码'),
])
def test_current_mechanisms_require_source_and_regression_entries(tmp_path, links, missing):
    root = mechanism_workspace(tmp_path, links)
    assert any(missing in error for error in check_documentation(root))


def test_valid_source_and_regression_entries_satisfy_structural_check(tmp_path):
    root = mechanism_workspace(tmp_path, '[实现](../../src/example.py)\n[回归](../../tests/test_example.py)\n')
    assert check_documentation(root) == []


def test_link_examples_do_not_count_as_implementation_evidence(tmp_path):
    root = mechanism_workspace(tmp_path, (
        '```markdown\n[实现](../../src/example.py)\n[回归](../../tests/test_example.py)\n```\n'
    ))
    errors = check_documentation(root)
    assert any('源码' in error for error in errors)
    assert any('回归入口' in error for error in errors)
