"""End-to-end behavioural tests for ``execute_campaign_send``.

We replace ``ESPClient.send_batch`` with a programmable fake so each test
exercises a specific code path: happy path, dedup, rate-limit retries, batch
failure isolation, all-fail, and crash-resume safety.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from lifecycle_platform.campaign_sender import execute_campaign_send
from lifecycle_platform.dedup import SentLog
from lifecycle_platform.esp_client import ESPClient, Response

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeESP(ESPClient):
    """ESPClient whose ``send_batch`` is driven by a callable."""

    def __init__(
        self,
        responder: Callable[[str, list[dict]], Response] | None = None,
    ) -> None:
        super().__init__(api_key="fake")
        self.calls: list[tuple[str, list[dict]]] = []
        self._responder = responder or (lambda _id, _b: Response(200, {"ok": True}))

    def send_batch(self, campaign_id: str, recipients: list[dict]) -> Response:
        self.calls.append((campaign_id, list(recipients)))
        return self._responder(campaign_id, recipients)


def _audience(n: int, prefix: str = "r") -> list[dict]:
    return [{"renter_id": f"{prefix}_{i:04d}", "phone": "+1555"} for i in range(n)]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "sent.json"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_sends_full_audience_in_batches_of_100(log_path: Path) -> None:
    audience = _audience(250)
    esp = FakeESP()

    summary = execute_campaign_send("camp_1", audience, esp, str(log_path))

    assert summary["total_sent"] == 250
    assert summary["total_failed"] == 0
    assert summary["total_skipped"] == 0
    assert "elapsed_seconds" in summary

    assert [len(b) for _, b in esp.calls] == [100, 100, 50]
    # All 250 ids made it onto the sent log
    assert SentLog(log_path).load("camp_1") == {r["renter_id"] for r in audience}


def test_sub_batch_audience_uses_one_call(log_path: Path) -> None:
    esp = FakeESP()
    audience = _audience(7)

    summary = execute_campaign_send("camp_1", audience, esp, str(log_path))

    assert summary["total_sent"] == 7
    assert len(esp.calls) == 1


def test_empty_audience_is_a_zero_summary_no_calls(log_path: Path) -> None:
    esp = FakeESP()
    summary = execute_campaign_send("camp_1", [], esp, str(log_path))

    assert summary["total_sent"] == 0
    assert summary["total_failed"] == 0
    assert summary["total_skipped"] == 0
    assert esp.calls == []


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_skips_recipients_already_in_sent_log(log_path: Path) -> None:
    SentLog(log_path).extend("camp_1", ["r_0001", "r_0002"])
    audience = _audience(5)
    esp = FakeESP()

    summary = execute_campaign_send("camp_1", audience, esp, str(log_path))

    assert summary["total_skipped"] == 2
    assert summary["total_sent"] == 3

    sent_ids = [r["renter_id"] for _, batch in esp.calls for r in batch]
    assert "r_0001" not in sent_ids
    assert "r_0002" not in sent_ids


def test_dedup_isolated_per_campaign(log_path: Path) -> None:
    SentLog(log_path).extend("other_campaign", ["r_0001"])
    esp = FakeESP()
    summary = execute_campaign_send("camp_1", _audience(3), esp, str(log_path))

    assert summary["total_skipped"] == 0
    assert summary["total_sent"] == 3


def test_rerun_is_idempotent(log_path: Path) -> None:
    audience = _audience(5)
    esp1 = FakeESP()
    execute_campaign_send("camp_1", audience, esp1, str(log_path))

    esp2 = FakeESP()
    summary = execute_campaign_send("camp_1", audience, esp2, str(log_path))

    assert summary["total_sent"] == 0
    assert summary["total_skipped"] == 5
    assert esp2.calls == []


# ---------------------------------------------------------------------------
# Rate limiting / retries
# ---------------------------------------------------------------------------


def test_429_then_200_succeeds(log_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the backoff sleeps instant.
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda *_: None)

    sequence = [Response(429), Response(429), Response(200, {"ok": True})]

    def responder(campaign_id: str, recipients: list[dict]) -> Response:
        return sequence.pop(0)

    esp = FakeESP(responder)
    summary = execute_campaign_send("camp_1", _audience(3), esp, str(log_path))

    assert summary["total_sent"] == 3
    assert len(esp.calls) == 3


def test_persistent_429_counts_as_failure(log_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda *_: None)
    esp = FakeESP(lambda _c, _b: Response(429))

    summary = execute_campaign_send("camp_1", _audience(3), esp, str(log_path))

    assert summary["total_sent"] == 0
    assert summary["total_failed"] == 3
    # 1 initial + 5 retries per the policy = 6 total calls.
    assert len(esp.calls) == 6


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


def test_one_failed_batch_does_not_abort_run(log_path: Path) -> None:
    """Batch 1 returns 500, batches 0 and 2 succeed."""
    call_index = {"i": 0}

    def responder(_c, _b):
        i = call_index["i"]
        call_index["i"] += 1
        return Response(500) if i == 1 else Response(200)

    esp = FakeESP(responder)
    summary = execute_campaign_send("camp_1", _audience(250), esp, str(log_path))

    # 3 batches of (100, 100, 50). Batch index 1 (100 rows) fails, the other two succeed.
    assert summary["total_sent"] == 150
    assert summary["total_failed"] == 100
    # Failed batch's ids must NOT be marked as sent
    sent = SentLog(log_path).load("camp_1")
    assert len(sent) == 150


def test_send_batch_exception_does_not_abort_run(log_path: Path) -> None:
    call_index = {"i": 0}

    def responder(_c, _b):
        i = call_index["i"]
        call_index["i"] += 1
        if i == 0:
            raise RuntimeError("network exploded")
        return Response(200)

    esp = FakeESP(responder)
    summary = execute_campaign_send("camp_1", _audience(150), esp, str(log_path))

    assert summary["total_sent"] == 50
    assert summary["total_failed"] == 100


def test_all_batches_fail_returns_zero_sent(log_path: Path) -> None:
    esp = FakeESP(lambda _c, _b: Response(503))
    summary = execute_campaign_send("camp_1", _audience(250), esp, str(log_path))

    assert summary["total_sent"] == 0
    assert summary["total_failed"] == 250


# ---------------------------------------------------------------------------
# Crash resume
# ---------------------------------------------------------------------------


def test_crash_after_first_batch_does_not_double_send_on_retry(log_path: Path) -> None:
    """Simulate a crash mid-run: first batch persisted, then exception."""
    state = {"call": 0}

    def crashing(_c, _b):
        state["call"] += 1
        if state["call"] == 1:
            return Response(200)
        raise SystemExit("simulated worker death")

    esp_crash = FakeESP(crashing)
    audience = _audience(150)

    with pytest.raises(SystemExit):
        # call_with_retry treats SystemExit as a non-retryable raise; we let it
        # propagate to mimic a hard crash that bypasses our try/except (we
        # explicitly catch Exception, not BaseException).
        execute_campaign_send("camp_1", audience, esp_crash, str(log_path))

    # First batch's renter_ids should be persisted on disk.
    persisted = SentLog(log_path).load("camp_1")
    assert len(persisted) == 100

    # Now retry with a healthy ESP - only the remaining 50 should ship.
    esp_healthy = FakeESP()
    summary = execute_campaign_send("camp_1", audience, esp_healthy, str(log_path))

    assert summary["total_sent"] == 50
    assert summary["total_skipped"] == 100
    assert len(esp_healthy.calls) == 1


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------


def test_audience_rows_missing_renter_id_are_skipped_with_warning(log_path: Path) -> None:
    audience = [{"renter_id": "r1"}, {"phone": "x"}, {"renter_id": "r2"}]
    esp = FakeESP()
    summary = execute_campaign_send("camp_1", audience, esp, str(log_path))

    assert summary["total_sent"] == 2
    assert summary["total_skipped"] == 1
