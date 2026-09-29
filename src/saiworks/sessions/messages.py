from __future__ import annotations

from collections.abc import Mapping

SESSION_REQUEST_MESSAGE_FORMAT = "stable_request_v1"


def model_session_message_content(message: Mapping[str, object]) -> str:
    content = message.get("content", "")
    if message.get("session_message_format") == SESSION_REQUEST_MESSAGE_FORMAT:
        return content if isinstance(content, str) else ""
    display_content = message.get("display_content")
    runtime_markers = (
        "\nSession trace summary:",
        "\nResume state:",
        "\nRecent current-run history:",
        "\nRuntime checkpoint (authoritative):",
    )
    if (
        message.get("role") == "user"
        and isinstance(content, str)
        and content.startswith("Current working directory:")
        and any(marker in content for marker in runtime_markers)
    ):
        cwd_line = content.splitlines()[0]
        if isinstance(display_content, str):
            return f"{cwd_line}\nUser request: {display_content}"
        marker = "\nUser request: "
        if marker in content:
            request = content.split(marker, 1)[1]
            for runtime_marker in runtime_markers:
                request = request.split(runtime_marker, 1)[0]
            return f"{cwd_line}{marker}{request}"
    return content if isinstance(content, str) else ""


def display_session_message(
    message: Mapping[str, object], *, include_cwd: bool = False
) -> str:
    display_content = message.get("display_content")
    content = message.get("content", "")
    if (
        include_cwd
        and message.get("role") == "user"
        and isinstance(content, str)
        and content.startswith("Current working directory:")
    ):
        cwd_line = content.splitlines()[0]
        request = display_content if isinstance(display_content, str) else ""
        if not request:
            marker = "\nUser request: "
            if marker in content:
                request = content.split(marker, 1)[1]
        if request:
            return f"{cwd_line}\nUser request: {request}"
    if isinstance(display_content, str) and display_content:
        return display_content

    if not isinstance(content, str):
        return ""
    if message.get("role") == "user" and content.startswith("Current working directory:"):
        marker = "\nUser request: "
        if marker in content:
            return content.split(marker, 1)[1]
    return content
