"""Tiny batching helper.

Kept as a standalone module so the chunking logic can be tested in isolation
and reused by other senders (e.g. an email channel) without pulling in the
full ESP pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import TypeVar

T = TypeVar("T")

DEFAULT_BATCH_SIZE = 100


def chunk(items: Iterable[T], size: int = DEFAULT_BATCH_SIZE) -> Iterator[list[T]]:
    """Yield successive lists of up to ``size`` items from ``items``.

    Memory-friendly: works on any iterable (lists, generators, query cursors)
    and never materialises the whole input at once.

    Raises ``ValueError`` for non-positive ``size`` so a misconfiguration
    surfaces immediately rather than running an infinite loop.
    """

    if size <= 0:
        raise ValueError(f"batch size must be positive, got {size}")

    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch
