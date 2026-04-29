"""Result objects returned by ``execute_campaign_send``.

A small dataclass keeps the call site readable while still being trivially
serialisable to a plain dict (for XCom transport in Airflow).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CampaignSummary:
    """Aggregate counters for a single campaign send invocation."""

    total_sent: int
    total_failed: int
    total_skipped: int
    elapsed_seconds: float

    def as_dict(self) -> dict[str, float | int]:
        """Return the summary in the exact shape the prompt's contract requires."""
        return asdict(self)
