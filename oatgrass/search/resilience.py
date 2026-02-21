"""Shared resilience helpers for transient API failures and payload guards."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiohttp import ClientConnectionError, ClientResponseError, ServerTimeoutError

RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
THROTTLE_SHAPE_HINT = "possible rate-limit/throttle response"

_T = TypeVar("_T")


def expect_dict(value: object, context: str) -> dict:
    if isinstance(value, dict):
        return value
    value_type = type(value).__name__
    raise ValueError(f"{context} has unexpected type '{value_type}' ({THROTTLE_SHAPE_HINT})")


def optional_dict(container: dict, key: str, context: str) -> dict:
    value = container.get(key, {})
    if value is None:
        return {}
    return expect_dict(value, f"{context}.{key}")


def optional_list(container: dict, key: str, context: str) -> list:
    value = container.get(key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return value
    value_type = type(value).__name__
    raise ValueError(f"{context}.{key} has unexpected type '{value_type}' ({THROTTLE_SHAPE_HINT})")


def optional_list_of_dicts(container: dict, key: str, context: str) -> list[dict]:
    values = optional_list(container, key, context)
    output: list[dict] = []
    for idx, value in enumerate(values):
        output.append(expect_dict(value, f"{context}.{key}[{idx}]"))
    return output


def response_payload(payload: object, context: str) -> dict:
    root = expect_dict(payload, f"{context} payload")
    status_value = root.get("status")
    status = status_value.strip().lower() if isinstance(status_value, str) else ""
    if status == "failure" and "response" not in root:
        error_text = root.get("error")
        detail = f": {error_text}" if isinstance(error_text, str) and error_text else ""
        raise ValueError(f"{context} returned status=failure without response{detail} ({THROTTLE_SHAPE_HINT})")
    return optional_dict(root, "response", context)


def is_retryable_exception(exc: Exception) -> bool:
    return (
        isinstance(exc, (asyncio.TimeoutError, ClientConnectionError, ServerTimeoutError))
        or (isinstance(exc, ClientResponseError) and exc.status in RETRYABLE_HTTP_STATUSES)
        or (isinstance(exc, ValueError) and THROTTLE_SHAPE_HINT in str(exc).lower())
    )


async def run_with_retries(
    operation: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int,
    on_retry: Callable[[int, int, int, Exception], None] | None = None,
) -> _T:
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable_exception(exc):
                raise
            delay = 2 ** attempt
            if on_retry is not None:
                on_retry(attempt, max_attempts, delay, exc)
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable retry exit")
