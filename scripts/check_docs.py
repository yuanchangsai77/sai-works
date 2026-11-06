"""Check repository documentation structure; business claims still need human review."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ROOT_DOCS = {"README.md", "architecture.md", "implementation.md", "roadmap.md", "evaluation.md"}
SECTIONS = {"runtime", "integrations", "interfaces", "reference", "design", "archive", "versions"}
WORK_STATES = {"待验收", "待实施", "待核实", "暂缓"}
LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:\s+"[^"]*")?\s*\)')
REFERENCE = re.compile(r'^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?')
TEMPORARY = re.compile(r'(?:^|[-_.])(todo|progress|tracking|audit|temporary|migration)(?:[-_.]|$)', re.I)
WORK_HEADING = re.compile(
    r'^#{1,6}\s+(?:\d+[.、 ]+)?(?:当前工作队列|跟踪清单|下一轮落地顺序|实施顺序与验收|待办清单|TODO|Roadmap|进度跟踪)(?:\s|$|[（(])',
    re.I,
)
LEGACY_STAGE = re.compile(
    r'\bD[123]\b|\bPhase\s+\d+\b|\bV0\.[12][ab]?\b|'
    r'(?:阶段|批次|属于)\s*[A-F](?:[123])?(?![A-Za-z0-9])|'
    r'(?<![A-Za-z0-9])(?!A/B\s*(?:测试|实验|对照))[A-F](?:[123])?(?:/[A-F](?:[123])?)+(?![A-Za-z0-9])|'
    r'(?:^|[；，：、\s])[B-F](?=[\s：、；，]|$)'
)
MARKDOWN_NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\.md')
VERSIONED_TOPIC = re.compile(r'(?:^|-)(?:v\d|phase-[a-f0-9]|stage-[a-f0-9])', re.I)
CURRENT_SECTIONS = {'runtime', 'integrations', 'interfaces', 'reference'}


def prose_lines(text: str):
    """Ignore fenced examples without changing diagnostic line numbers."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if match:
            marker, rest = match.groups()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = None
            continue
        if fence is None:
            yield number, line


def heading_ids(lines) -> set[str]:
    ids = set()
    counts: dict[str, int] = defaultdict(int)
    for _, line in lines:
        ids.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', line))
        match = re.match(r'^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$', line)
        if not match:
            continue
        heading = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', match[1])
        heading = re.sub(r'<[^>]+>', '', heading).lower()
        slug = ''.join(char for char in heading if char.isalnum() or char in ' _-')
        slug = slug.replace(' ', '-')
        count = counts[slug]
        ids.add(slug if count == 0 else f'{slug}-{count}')
        counts[slug] += 1
    return ids


def check_documentation(root: Path) -> list[str]:
    root = root.resolve()
    docs = root / 'docs'
    errors: list[str] = []
    files = sorted(docs.rglob('*.md'))
    root_files = [root / name for name in ('README.md', 'AGENTS.md') if (root / name).is_file()]
    texts = {path: path.read_text(encoding='utf-8') for path in [*files, *root_files]}
    lines = {path: list(prose_lines(text)) for path, text in texts.items()}
    anchors = {path: heading_ids(value) for path, value in lines.items()}
    graph: dict[Path, set[Path]] = defaultdict(set)
    roadmap = docs / 'roadmap.md'
    entry = docs / 'README.md'

    def report(path, number, message):
        errors.append(f'{path.relative_to(root)}:{number}: {message}')

    def historical(path):
        return path.is_relative_to(docs / 'archive') or path.is_relative_to(docs / 'versions')

    for required in (entry, roadmap):
        if required not in texts:
            report(required, 1, '缺少唯一文档入口或工作队列')

    work = {}
    for path, values in lines.items():
        if path in files:
            relative = path.relative_to(docs)
            if (len(relative.parts) == 1 and path.name not in ROOT_DOCS) or (
                len(relative.parts) > 1 and relative.parts[0] not in SECTIONS
            ):
                report(path, 1, '未知文档位置；先选择已有权威文档和目录职责')
            if not historical(path) and TEMPORARY.search(path.name):
                report(path, 1, '当前文档禁止新增独立 TODO/进度/临时审查或迁移计划')
            if not historical(path):
                if path.name != 'README.md' and not MARKDOWN_NAME.fullmatch(path.name):
                    report(path, 1, '文档文件名须使用小写英文主题词和连字符')
                if VERSIONED_TOPIC.search(path.stem):
                    report(path, 1, '当前主题文件名不得使用阶段代号或内部版本号')
                heading = next((line for _, line in values if line.startswith('# ')), '')
                if not re.search(r'[\u3400-\u9fff]', heading):
                    report(path, 1, '当前文档标题须用能直接理解的中文主题名称')
            if path.parent == docs / 'design' and path.name != 'README.md':
                if not re.search(r'^(?:-\s*)?状态[:：]', '\n'.join(texts[path].splitlines()[:12]), re.M):
                    report(path, 1, '专项设计须在开头声明目标或设计状态，不能暗示已交付')

        for number, line in values:
            # Ignore link examples in inline code, but keep code text in heading IDs.
            navigation = re.sub(r'(`+).*?\1', '', line)
            targets = [match[1] or match[2] for match in LINK.finditer(navigation)]
            reference = REFERENCE.match(line)
            if reference:
                targets.append(reference[1])
            for target in targets:
                try:
                    url = urlsplit(target)
                except ValueError:
                    report(path, number, f'无效链接格式：{target}')
                    continue
                if url.scheme or url.netloc:
                    continue  # No network checks or assumptions about external projects.
                try:
                    linked = (path.parent / unquote(url.path)).resolve() if url.path else path
                except (ValueError, OSError):
                    report(path, number, f'无效本地链接路径：{target}')
                    continue
                if not linked.is_relative_to(root):
                    report(path, number, f'本地链接越出仓库，不能依赖相邻项目布局：{target}')
                elif not linked.exists():
                    report(path, number, f'本地链接不存在：{target}')
                else:
                    graph[path].add(linked)
                    if url.fragment and linked in anchors and unquote(url.fragment) not in anchors[linked]:
                        report(path, number, f'本地锚点不存在：{target}')

            if historical(path):
                continue
            if LEGACY_STAGE.search(navigation):
                report(path, number, '当前文档使用中文能力名称，不使用字母阶段或内部里程碑代号')
            if path != roadmap and WORK_HEADING.match(line):
                report(path, number, '实施顺序和进度清单只维护在 docs/roadmap.md')
            if path != roadmap and re.match(r'^\s*[-*]\s+\[[ xX]\]\s+', line):
                report(path, number, '当前待办复选框只允许在 docs/roadmap.md 维护')
            if not line.startswith('|'):
                continue
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if cells[0] == '工作项' or all(re.fullmatch(r'[-: ]+', cell) for cell in cells):
                continue
            if path != roadmap:
                if len(cells) > 1 and cells[1] in WORK_STATES:
                    report(path, number, '当前工作状态表只能位于 docs/roadmap.md')
                continue
            if len(cells) != 6 or not all(cells):
                report(path, number, '工作项必须完整填写名称、状态、范围、依赖、完成判据、依据')
                continue
            key, state, _, dependencies, _, evidence = cells
            if not re.search(r'[\u3400-\u9fff]', key) or re.search(r'\b[A-Z]\d+\b', key):
                report(path, number, '工作项须使用完整中文名称，不使用字母数字编号')
            if key in work:
                report(path, number, f'重复工作名称：{key}')
            if state not in WORK_STATES:
                report(path, number, f'未知工作状态：{state}')
            if not LINK.search(evidence):
                report(path, number, f'工作项 {key} 缺少设计或证据链接')
            if re.search(r'\b[A-Z]\d+\b', dependencies):
                report(path, number, '依赖须用「完整工作名称」引用，不能使用编号')
            work[key] = (number, set(re.findall(r'「([^」]+)」', dependencies)))

    if roadmap in texts and not work:
        report(roadmap, 1, '路线图须明确列出工作项，不能另建未受约束的进度清单')
    visited, active = set(), set()

    def visit_work(key):
        if key in active:
            report(roadmap, work[key][0], f'工作依赖形成循环：{key}')
            return
        if key in visited:
            return
        visited.add(key)
        active.add(key)
        for dependency in sorted(work[key][1]):
            if dependency not in work:
                report(roadmap, work[key][0], f'工作依赖不存在：{dependency}')
            else:
                visit_work(dependency)
        active.remove(key)

    for key in sorted(work):
        visit_work(key)

    reachable = set()
    queue = deque([entry])
    while queue:
        path = queue.popleft()
        if path in reachable:
            continue
        reachable.add(path)
        queue.extend(graph[path] - reachable)
    for path in files:
        if path not in reachable:
            report(path, 1, '文档未从 docs/README.md 可达，属于孤立材料')
        if path.name == 'README.md':
            continue
        relative = path.relative_to(docs)
        current = not historical(path) and (
            relative.parts[0] in CURRENT_SECTIONS or relative.as_posix() in {'implementation.md', 'evaluation.md'}
        )
        if current:
            if not any(target.is_file() and target.is_relative_to(root / 'src') for target in graph[path]):
                report(path, 1, '当前实现文档须链接到仓库内有效源码，不能只写文件简称')
            if not any(target.is_file() and target.is_relative_to(root / 'tests') for target in graph[path]):
                report(path, 1, '当前实现文档须给出可定位的回归入口；链接不代表已经验收')
        if historical(path):
            index = docs / 'archive' / 'README.md'
        else:
            index = path.parent / 'README.md'
        if path not in graph[index]:
            report(path, 1, f'所属索引缺少入口：{index.relative_to(root)}')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check_documentation(args.root)
    if errors:
        for error in errors[:50]:
            print(error)
        print(f'文档检查失败：{len(errors)} 项（最多展示 50 项）')
        return 1
    print('文档检查通过：结构、命名、来源与单一工作队列符合约束。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
