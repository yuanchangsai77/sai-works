from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from ...types import StoredSession
    from ..cli import CLI


def handle_resume(cli: CLI, args: list[str], session: StoredSession | None = None, conversation: list[dict[str, str]] | None = None, **kwargs) -> bool:
    target_id = args[0] if args else None
    cli.handle_resume_command(target_id=target_id, current_session=session, conversation=conversation)
    return False


def handle_history(cli: CLI, args: list[str], session: StoredSession | None = None, **kwargs) -> bool:
    store = getattr(cli, "session_store", None)
    if store is None or session is None:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print("[SaiWorks] archived session history is unavailable")
        return False

    archive_ids = list(getattr(session, "compaction_archive_ids", []))
    if not archive_ids:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print("[SaiWorks] this session has no compacted history archives")
        return False

    if not args:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print("[SaiWorks] archived history references:")
            for archive_id in archive_ids:
                cli.presenter._print(f"  {archive_id}")
            cli.presenter._print("Use /history <archive_id> to view an archive.")
        return False

    archive_id = args[0]
    if archive_id not in archive_ids:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print(f"[SaiWorks] archive '{archive_id}' is not referenced by this session")
        return False
    messages = cli.runtime.load_archived_messages(session.session_id, archive_id)
    if messages is None:
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print(f"[SaiWorks] archive '{archive_id}' could not be loaded")
        return False
    if cli.presenter and hasattr(cli.presenter, "show_session_history"):
        cli.presenter.show_session_history([
            {
                "role": "system",
                "content": f"Archived history: {archive_id} (view with /history {archive_id})",
            },
            *messages,
        ])
    return False


def handle_reset(cli: CLI, args: list[str], session: StoredSession | None = None, conversation: list[dict[str, str]] | None = None, **kwargs) -> bool:
    old_id = session.session_id if session is not None else ""
    new_session = cli.runtime.reset_session(session, conversation if conversation is not None else [])
    if new_session is not None:
        cli.active_session = new_session
        if cli.presenter and hasattr(cli.presenter, "_print"):
            cli.presenter._print(
                f"   \033[1;36mStarted new session:\033[0m \033[1;33m{new_session.session_id}\033[0m "
                f"(inherited environment from {old_id})"
            )
    if cli.presenter and hasattr(cli.presenter, "show_context_reset"):
        cli.presenter.show_context_reset()
    return False


def handle_compact(
    cli: CLI, args: list[str], session: StoredSession | None = None,
    conversation: list[dict[str, str]] | None = None, **kwargs,
) -> bool:
    presenter = cli.presenter
    if not conversation or len(conversation) <= 1:
        if presenter and hasattr(presenter, "_print"):
            presenter._print("\nConversation context is empty or short. No compaction needed.\n")
        return False
    handle = None
    if presenter and hasattr(presenter, "show_status_bar"):
        presenter.show_status_bar(engine=cli.runtime.view(), is_running=True)
    if presenter and hasattr(presenter, "model_started"):
        handle = presenter.model_started(message="Compacting conversation context...")
    try:
        result = cli.runtime.compact_session(session, conversation)
    except Exception as error:
        if presenter and hasattr(presenter, "_print"):
            presenter._print(f"[SaiWorks] could not compact session; original context was kept: {error}")
        return False
    finally:
        if handle is not None and presenter and hasattr(presenter, "model_finished"):
            presenter.model_finished(handle)
        if presenter and hasattr(presenter, "clear_running_status_bar"):
            presenter.clear_running_status_bar(0)
    if presenter and hasattr(presenter, "show_context_compacted"):
        presenter.show_context_compacted(old_count=result.old_count, new_count=result.new_count,
                                         archive_id=result.archive_id, summary=result.summary)
    return False
