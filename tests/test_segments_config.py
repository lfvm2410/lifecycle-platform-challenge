"""Schema validation for ``config/segments.yaml``.

The DAG (and any future tooling that adds new value-model segments) reads
this file at parse time, so a typo here breaks production. This test runs
on every commit so the YAML can never silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "segments.yaml"

REQUIRED_KEYS = {
    "id",
    "campaign_id",
    "sql_template",
    "model_table",
    "model_version_pin",
    "threshold",
    "freshness_sla",
    "on_model_missing",
}

VALID_FALLBACK_POLICIES = {"wait", "skip", "fallback_unscored"}


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    """Parse the YAML once per test module."""
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    assert isinstance(raw, dict), "segments.yaml must be a mapping at the top level"
    assert "segments" in raw, "top-level 'segments' key is required"
    assert isinstance(raw["segments"], list), "'segments' must be a list"
    return raw["segments"]


def test_at_least_one_segment_defined(segments: list[dict]) -> None:
    assert len(segments) >= 1, "at least one segment must be defined"


def test_each_segment_has_required_keys(segments: list[dict]) -> None:
    for segment in segments:
        missing = REQUIRED_KEYS - set(segment.keys())
        assert not missing, f"segment {segment.get('id')} missing keys: {missing}"


def test_segment_ids_are_unique(segments: list[dict]) -> None:
    ids = [s["id"] for s in segments]
    assert len(ids) == len(set(ids)), f"duplicate segment ids: {ids}"


def test_threshold_is_in_unit_interval(segments: list[dict]) -> None:
    for segment in segments:
        threshold = segment["threshold"]
        assert isinstance(
            threshold, int | float
        ), f"segment {segment['id']}: threshold must be numeric, got {type(threshold).__name__}"
        assert (
            0.0 <= threshold <= 1.0
        ), f"segment {segment['id']}: threshold {threshold} outside [0.0, 1.0]"


def test_freshness_sla_is_positive(segments: list[dict]) -> None:
    for segment in segments:
        sla = segment["freshness_sla"]
        assert isinstance(
            sla, int | float
        ), f"segment {segment['id']}: freshness_sla must be numeric"
        assert sla > 0, f"segment {segment['id']}: freshness_sla must be > 0 hours"


def test_fallback_policy_is_known(segments: list[dict]) -> None:
    for segment in segments:
        policy = segment["on_model_missing"]
        assert (
            policy in VALID_FALLBACK_POLICIES
        ), f"segment {segment['id']}: on_model_missing={policy!r} not in {VALID_FALLBACK_POLICIES}"


def test_sql_template_paths_exist(segments: list[dict]) -> None:
    repo_root = CONFIG_PATH.parent.parent
    for segment in segments:
        sql_path = repo_root / segment["sql_template"]
        assert sql_path.exists(), f"segment {segment['id']}: sql_template {sql_path} does not exist"


def test_model_table_is_qualified(segments: list[dict]) -> None:
    """Reject bare table names.

    ``dataset.table`` form is required; bare table names would silently
    point at the wrong dataset in BigQuery.
    """
    for segment in segments:
        table = segment["model_table"]
        assert (
            "." in table
        ), f"segment {segment['id']}: model_table {table!r} must be 'dataset.table' qualified"
