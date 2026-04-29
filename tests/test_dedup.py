"""Tests for the file-based ``SentLog`` dedup store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lifecycle_platform.dedup import SentLog


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "sent_renters.json"


def test_load_returns_empty_set_when_file_missing(log_path: Path) -> None:
    log = SentLog(log_path)
    assert log.load("camp_1") == set()


def test_extend_then_load_round_trips(log_path: Path) -> None:
    log = SentLog(log_path)
    log.extend("camp_1", ["a", "b", "c"])

    assert log.load("camp_1") == {"a", "b", "c"}
    assert log.load("camp_other") == set()


def test_extend_is_set_like_no_duplicates(log_path: Path) -> None:
    log = SentLog(log_path)
    log.extend("camp_1", ["a", "b"])
    log.extend("camp_1", ["b", "c", "a"])

    assert log.load("camp_1") == {"a", "b", "c"}


def test_campaigns_are_isolated(log_path: Path) -> None:
    log = SentLog(log_path)
    log.extend("camp_1", ["a", "b"])
    log.extend("camp_2", ["c", "d"])

    assert log.load("camp_1") == {"a", "b"}
    assert log.load("camp_2") == {"c", "d"}


def test_extend_with_no_ids_is_noop(log_path: Path) -> None:
    log = SentLog(log_path)
    log.extend("camp_1", [])
    assert not log_path.exists()


def test_atomic_write_no_temp_file_left_behind(log_path: Path) -> None:
    log = SentLog(log_path)
    log.extend("camp_1", ["a", "b"])

    leftovers = [
        p for p in log_path.parent.iterdir() if p.name != log_path.name and p.suffix == ".tmp"
    ]
    assert leftovers == []


def test_corrupt_file_is_quarantined_and_log_resets(log_path: Path) -> None:
    log_path.write_text("{not valid json")

    log = SentLog(log_path)
    assert log.load("camp_1") == set()  # corrupt -> empty

    backup = log_path.with_suffix(log_path.suffix + ".corrupt")
    assert backup.exists(), "corrupt log should be moved aside"


def test_persistence_across_instances(log_path: Path) -> None:
    SentLog(log_path).extend("camp_1", ["x", "y"])
    assert SentLog(log_path).load("camp_1") == {"x", "y"}


def test_on_disk_format_is_human_readable(log_path: Path) -> None:
    SentLog(log_path).extend("camp_1", ["b", "a"])

    raw = json.loads(log_path.read_text())
    # ids are sorted for stable diffs in PR review
    assert raw == {"camp_1": ["a", "b"]}
