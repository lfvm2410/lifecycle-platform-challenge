"""File-based deduplication of already-sent recipients.

The prompt explicitly asks for a "simple file-based approach" so we keep this
narrow: a JSON document on disk, namespaced by ``campaign_id``, mapping to the
list of ``renter_id`` values we have already delivered to.

Reads are cheap (one file open per pipeline run). Writes happen incrementally
after every successful batch and use the *write-temp + os.replace* pattern so
a crash mid-write cannot corrupt the file. The lock is a process-local
``threading.Lock`` because the Airflow task is single-process; multi-worker
deployments should swap this implementation for a transactional store
(BigQuery ``MERGE`` against a ``lifecycle.sent_log`` table).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path


class SentLog:
    """Persistent set of sent ``renter_id`` values keyed by ``campaign_id``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Filesystem path of the underlying JSON sent log."""
        return self._path

    # -- read -------------------------------------------------------------

    def load(self, campaign_id: str) -> set[str]:
        """Return the set of renter_ids already sent for ``campaign_id``.

        A missing or empty file is treated as "nothing sent yet" so that
        first-run callers don't need to special-case bootstrapping.
        """
        with self._lock:
            data = self._read_all()
        return set(data.get(campaign_id, []))

    # -- write ------------------------------------------------------------

    def extend(self, campaign_id: str, renter_ids: Iterable[str]) -> None:
        """Append ``renter_ids`` to the campaign's sent set, atomically."""
        new_ids = list(renter_ids)
        if not new_ids:
            return

        with self._lock:
            data = self._read_all()
            existing = set(data.get(campaign_id, []))
            existing.update(new_ids)
            data[campaign_id] = sorted(existing)
            self._atomic_write(data)

    # -- internals --------------------------------------------------------

    def _read_all(self) -> dict[str, list[str]]:
        if not self._path.exists() or self._path.stat().st_size == 0:
            return {}
        try:
            data: dict[str, list[str]] = json.loads(self._path.read_text())
            return data
        except json.JSONDecodeError:
            # A corrupt log is preferable to losing newer data; rename so an
            # operator can inspect it, and start fresh.
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            self._path.rename(backup)
            return {}

    def _atomic_write(self, data: dict[str, list[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile in the same directory guarantees os.replace is
        # atomic on POSIX.
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
