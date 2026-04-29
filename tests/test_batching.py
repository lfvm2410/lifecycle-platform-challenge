"""Tests for ``lifecycle_platform.batching.chunk``."""

from __future__ import annotations

import pytest

from lifecycle_platform.batching import chunk


def test_splits_into_full_batches() -> None:
    items = list(range(250))
    batches = list(chunk(items, size=100))

    assert [len(b) for b in batches] == [100, 100, 50]
    assert sum(batches, []) == items


def test_handles_smaller_than_batch_size() -> None:
    assert list(chunk([1, 2, 3], size=10)) == [[1, 2, 3]]


def test_empty_input_yields_nothing() -> None:
    assert list(chunk([], size=10)) == []


def test_works_with_generators_lazily() -> None:
    """The helper must not materialise the whole input upfront."""

    def gen():
        yield from range(5)

    batches = list(chunk(gen(), size=2))
    assert batches == [[0, 1], [2, 3], [4]]


@pytest.mark.parametrize("bad_size", [0, -1, -100])
def test_rejects_non_positive_size(bad_size: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        list(chunk([1, 2, 3], size=bad_size))
