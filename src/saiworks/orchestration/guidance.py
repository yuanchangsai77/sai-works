from __future__ import annotations

from dataclasses import dataclass

from .session import SessionContext


@dataclass(frozen=True, slots=True)
class GuidanceGroup:
    tools: frozenset[str]
    lines: tuple[str, ...]
    errors: frozenset[str] = frozenset()


GROUPS = (
    GuidanceGroup(frozenset({"warehouse_list", "toolbox_open", "capability_activate"}), (
        "- Answer directly when current knowledge and tools suffice. For specialized capabilities, use the optional catalog below or warehouse_list, then open a relevant toolbox, inspect its items and activate only what you need. Activated capabilities become available next model turn.",
        "- Only activate the smallest set of capabilities needed for the current step, normally one to three items.",
        "- If a requested toolbox cannot be opened, report its manifest status. Do not inspect project source, config files, or environment variables unless the user explicitly asks to troubleshoot it.",
        "- Never infer that credentials, transports, or integrations are missing from an unopened toolbox; health is unknown until it is opened.",
    )),
    GuidanceGroup(frozenset({"workspace_open"}), (
        "- Use workspace_open only when the task needs a directory to become the active workspace; ordinary external access does not change the active workspace.",
    )),
    GuidanceGroup(frozenset({"search_text"}), (
        "- search_text returns nearby context and counts only the returned lines as inspected.",
    )),
    GuidanceGroup(frozenset({"patch"}), (
        "- A patch with old lines matching the current file at its stated location can apply directly. Inspect the affected area with read_file or search_text when the location or old content is uncertain; unrelated parts need no inspection.",
        "- If patch returns file_not_read, follow its read hint, inspect only the missing range, and retry the patch without asking the user.",
        "- If patch returns file_changed_since_read, use search_text to re-locate the intended content, inspect its current surrounding lines, and regenerate the hunk without asking the user.",
        "- If a successful patch reports automatic relocation, accept the resolved location, do not repeat the patch, and account for the new line positions in later work.",
        "- Do not call read_file, search_text, or another tool solely to verify a successful automatic relocation. If the requested change is complete, finish using the reported location.",
        "- If the user asked for file changes and you already inspected the affected lines, stop inspecting and use patch.",
        "- For requests to create, generate, scaffold, or upgrade a standard project structure, after locating the target root, create or update the standard files directly instead of repeatedly listing directories.",
        "- Do not require a complete directory tree before making a reasonable scoped change.",
    ), frozenset({"file_not_read", "file_changed_since_read", "patch_context_mismatch"})),
    GuidanceGroup(frozenset({"shell_exec"}), (
        "- shell_exec keeps shell state within the current session, including across resumed runs. Its projected cwd below is authoritative when present.",
        "- Use shell_exec cwd when you need to start or reset the persistent shell working directory explicitly.",
        "- Prefer available structured tools over shell_exec when they can do the job; do not use shell_exec to create or edit files when patch is available.",
    )),
    GuidanceGroup(frozenset({"subagent_spawn", "subagent_resume", "subagent_run_ready", "subagent_status"}), (
        "- A completed subagent result is a delegated handoff. Reuse its summary and reported verification; do not reread or reimplement its artifact solely to confirm it.",
        "- Verify a completed subagent only when evidence is missing, results conflict, the user requests review, or the change is security-sensitive. Keep verification targeted to the claim.",
        "- Send feedback or a failed verification back to the same child with subagent_resume. Do not spawn a replacement child for the same artifact unless isolation or independent parallel exploration is required.",
    )),
)


def select_operation_guidance(session: SessionContext) -> tuple[str, ...]:
    """Use runtime visibility and observations, never inferred user intent or permissions."""
    tools = {tool.name for tool in session.available_tools}
    tools.update(result.name for result in session.tool_results)
    tools.update(observation.tool for observation in session.recovered_observations)
    errors = {result.error_code for result in session.tool_results if result.error_code}
    errors.update(observation.error_code for observation in session.recovered_observations if observation.error_code)
    return tuple(
        line for group in GROUPS
        if group.tools & tools or group.errors & errors
        for line in group.lines
    )
