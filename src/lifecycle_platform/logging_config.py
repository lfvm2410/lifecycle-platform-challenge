"""Structured JSON logging via ``structlog``.

Every log line carries the fields ingestion pipelines (Datadog, GCP Cloud
Logging) need to slice and dice without grepping free text:
``event``, ``timestamp``, ``level``, plus whatever the caller binds via
``logger.bind(...)`` (campaign_id, batch_index, attempt, etc.).

Call ``configure_logging()`` once at process start (the Airflow task entrypoint
does this for us). It is idempotent so re-importing the module in tests is safe.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure stdlib logging and structlog to emit JSON to stdout."""

    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger; ensures ``configure_logging`` has run."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
