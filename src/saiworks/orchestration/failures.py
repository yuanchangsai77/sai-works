from __future__ import annotations

from ..model.types import ModelProtocolError, ModelRetryableError


def find_error(error: BaseException, error_type):
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, error_type):
            return current
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return None


def find_model_protocol_error(error: BaseException) -> ModelProtocolError | None:
    """Return a protocol failure from an exception cause chain, if present."""
    return find_error(error, ModelProtocolError)


def runtime_failure_message(error: BaseException) -> str:
    protocol_error = find_model_protocol_error(error)
    if protocol_error is not None:
        return (
            "The model responded, but SaiWorks could not accept its response. "
            f"{protocol_error}. You can retry this task or open diagnostics for details."
        )
    retryable_error = find_error(error, ModelRetryableError)
    if retryable_error is not None:
        return (
            f"Model API is unavailable right now. {retryable_error}. "
            "You can keep this session open and try again later."
        )
    return (
        f"SaiWorks Runtime could not complete this run. {error}. "
        "Open diagnostics for details before retrying."
    )


def runtime_failure_retryability(error: BaseException) -> str:
    return "retryable" if find_error(error, ModelRetryableError) is not None else "conditional"
