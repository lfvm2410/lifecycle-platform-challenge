"""Tests for the rate-limit retry policy."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lifecycle_platform.backoff import call_with_retry, is_rate_limited
from lifecycle_platform.esp_client import Response


def _seq(*responses: Response) -> Iterator[Response]:
    yield from responses


class _Counter:
    """Callable that returns the next scripted response on each invocation."""

    def __init__(self, responses: list[Response]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> Response:
        self.calls += 1
        return self._responses.pop(0)


def test_is_rate_limited_only_for_429() -> None:
    assert is_rate_limited(Response(429))
    assert not is_rate_limited(Response(200))
    assert not is_rate_limited(Response(500))


def test_returns_immediately_on_first_success() -> None:
    fn = _Counter([Response(200, {"ok": True})])
    result = call_with_retry(fn, base_seconds=0.0, max_seconds=0.0)

    assert result.status_code == 200
    assert fn.calls == 1


def test_retries_on_429_then_succeeds() -> None:
    fn = _Counter([Response(429), Response(429), Response(200, {"ok": True})])
    result = call_with_retry(fn, base_seconds=0.0, max_seconds=0.0)

    assert result.status_code == 200
    assert fn.calls == 3


def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    fn = _Counter([Response(429)] * 5)
    result = call_with_retry(
        fn,
        max_attempts=5,
        base_seconds=0.0,
        max_seconds=0.0,
    )
    assert result.status_code == 429
    assert fn.calls == 5


def test_non_429_errors_are_not_retried() -> None:
    """5xx is the campaign sender's job to deal with, not the backoff layer."""
    fn = _Counter([Response(500, {"err": "down"})])
    result = call_with_retry(fn, base_seconds=0.0, max_seconds=0.0)

    assert result.status_code == 500
    assert fn.calls == 1


def test_passes_args_through_to_callable() -> None:
    captured: dict[str, object] = {}

    def fn(campaign_id: str, recipients: list[dict]) -> Response:
        captured["campaign_id"] = campaign_id
        captured["recipients"] = recipients
        return Response(200)

    call_with_retry(fn, "camp_1", [{"renter_id": "a"}], base_seconds=0.0, max_seconds=0.0)
    assert captured == {"campaign_id": "camp_1", "recipients": [{"renter_id": "a"}]}
