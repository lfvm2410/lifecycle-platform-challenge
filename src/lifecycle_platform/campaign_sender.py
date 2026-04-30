"""Campaign send orchestration.

This is the entrypoint required by the prompt. It pulls together the four
supporting concerns (batching, dedup, retries, metrics) and exposes a single
function ``execute_campaign_send`` that an Airflow task or local script can
call directly.

Behaviour summary
-----------------
1. Load the per-campaign sent log; filter the audience down to recipients
   we have not delivered to yet (``total_skipped`` counter).
2. Iterate the remainder in batches of ``DEFAULT_BATCH_SIZE``.
3. For each batch, call the ESP through the retry wrapper; treat any 2xx
   as success and anything else as a failed batch.
4. On success, persist the batch's renter_ids to the sent log immediately
   so a crash mid-run cannot double-send on retry.
5. A single failed batch logs the failure and the run continues — one bad
   batch must never abort the campaign.
6. Return a ``CampaignSummary`` (as a plain dict, per the contract).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from lifecycle_platform.backoff import call_with_retry
from lifecycle_platform.batching import DEFAULT_BATCH_SIZE, chunk
from lifecycle_platform.dedup import SentLog
from lifecycle_platform.esp_client import ESPClient, Response
from lifecycle_platform.logging_config import get_logger
from lifecycle_platform.metrics import CampaignSummary

logger = get_logger(__name__)


def _is_success(response: Response) -> bool:
    """Treat any 2xx as a delivered batch."""
    return 200 <= response.status_code < 300


def _renter_ids(recipients: Iterable[dict[str, Any]]) -> list[str]:
    return [r["renter_id"] for r in recipients]


def execute_campaign_send(
    campaign_id: str,
    audience: list[dict[str, Any]],
    esp_client: ESPClient,
    sent_log_path: str = "sent_renters.json",
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, float | int]:
    """Send ``audience`` to the ESP in batches with retry, dedup, and metrics.

    Returns a dict with the four keys the prompt specifies:
    ``total_sent``, ``total_failed``, ``total_skipped``, ``elapsed_seconds``.
    """
    started = time.perf_counter()
    log = logger.bind(campaign_id=campaign_id, audience_size=len(audience))
    log.info("campaign_send.start")

    sent_log = SentLog(sent_log_path)
    already_sent = sent_log.load(campaign_id)

    to_send: list[dict[str, Any]] = []
    skipped = 0
    for recipient in audience:
        renter_id = recipient.get("renter_id")
        if renter_id is None:
            # Defensive: drop malformed rows but count them as skipped so the
            # caller can spot them in the summary.
            skipped += 1
            log.warning("campaign_send.skip", reason="missing_renter_id")
            continue
        if renter_id in already_sent:
            skipped += 1
        else:
            to_send.append(recipient)

    log.info(
        "campaign_send.dedup",
        to_send=len(to_send),
        skipped=skipped,
    )

    total_sent = 0
    total_failed = 0

    for batch_index, batch in enumerate(chunk(to_send, batch_size)):
        batch_log = log.bind(batch_index=batch_index, batch_size=len(batch))
        batch_started = time.perf_counter()
        try:
            response = call_with_retry(esp_client.send_batch, campaign_id, batch)
        except Exception as exc:
            # Network errors, transport timeouts, or backoff exhaustion that
            # raised - log the renter_ids so an operator can replay manually.
            total_failed += len(batch)
            batch_log.error(
                "campaign_send.batch_exception",
                error=str(exc),
                error_type=exc.__class__.__name__,
                renter_ids=_renter_ids(batch),
            )
            continue

        elapsed_ms = round((time.perf_counter() - batch_started) * 1000, 2)
        if _is_success(response):
            sent_log.extend(campaign_id, _renter_ids(batch))
            total_sent += len(batch)
            batch_log.info(
                "campaign_send.batch_ok",
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        else:
            total_failed += len(batch)
            batch_log.error(
                "campaign_send.batch_failed",
                status_code=response.status_code,
                response_body=response.json(),
                elapsed_ms=elapsed_ms,
                renter_ids=_renter_ids(batch),
            )

    elapsed = round(time.perf_counter() - started, 3)
    summary = CampaignSummary(
        total_sent=total_sent,
        total_failed=total_failed,
        total_skipped=skipped,
        elapsed_seconds=elapsed,
    )
    log.info(
        "campaign_send.done",
        total_sent=summary.total_sent,
        total_failed=summary.total_failed,
        total_skipped=summary.total_skipped,
        elapsed_seconds=summary.elapsed_seconds,
    )
    return summary.as_dict()
