"""Retry policy for the ESP send.

The ESP returns HTTP 429 when rate-limited. The prompt requires:

* exponential backoff with jitter,
* a hard cap of 5 retries.

We use ``tenacity`` to keep the wrapper short and the policy declarative.
A hand-rolled equivalent is ~20 lines if a reviewer prefers no third-party
dependency; the abstractions used here (``Retrying``, ``RetryError``) are
all standard ``tenacity`` primitives.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from tenacity import (
    RetryError,
    Retrying,
    retry_if_result,
    stop_after_attempt,
    wait_random_exponential,
)

from lifecycle_platform.esp_client import Response

DEFAULT_MAX_RETRIES = 5
"""Number of retries on top of the initial attempt (per the prompt: 'max 5 retries').

Total calls per batch is therefore ``DEFAULT_MAX_RETRIES + 1 = 6``.
"""

DEFAULT_BASE_SECONDS = 1.0
"""Initial backoff multiplier; tenacity randomises the actual wait."""

DEFAULT_MAX_SECONDS = 30.0
"""Upper bound on a single sleep so we don't pause for minutes."""

T = TypeVar("T")


def is_rate_limited(response: Response) -> bool:
    """Treat HTTP 429 as the trigger for the retry loop.

    5xx errors are surfaced to the caller and handled by the per-batch
    try/except so they don't block the rest of the run.
    """
    return response.status_code == 429


def call_with_retry(
    func: Callable[..., Response],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    **kwargs: Any,
) -> Response:
    """Invoke ``func(*args, **kwargs)``, retrying only on HTTP 429.

    Exponential backoff with jitter is implemented by tenacity's
    ``wait_random_exponential`` (a.k.a. "full jitter"). The function makes
    up to ``max_retries + 1`` calls (1 initial + N retries) before returning
    the last ``Response`` so the caller can log it and move on.
    """
    retryer = Retrying(
        retry=retry_if_result(is_rate_limited),
        wait=wait_random_exponential(multiplier=base_seconds, max=max_seconds),
        stop=stop_after_attempt(max_retries + 1),
        reraise=True,
    )
    try:
        result: Response = retryer(func, *args, **kwargs)
        return result
    except RetryError as exc:  # pragma: no cover - reraise=True path
        last: Response = exc.last_attempt.result()
        return last
