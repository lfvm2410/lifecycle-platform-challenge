"""Tests for sql/part4_audience_scored.sql.

These tests reuse the DuckDB harness built in conftest.py and add a parameter
for the @threshold value.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tests.conftest import DEFAULT_RUN_DATE, SQL_DIR, render_sql_for_duckdb

PART4_SQL = SQL_DIR / "part4_audience_scored.sql"


def _profile(renter_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "renter_id": renter_id,
        "email": f"{renter_id}@example.com",
        "phone": "+15555550100",
        "last_login": datetime(2025, 1, 15, 10, 0, 0),
        "subscription_status": "churned",
        "sms_consent": True,
        "email_consent": True,
        "dnd_until": None,
        "created_at": datetime(2024, 1, 1),
    }
    base.update(overrides)
    return base


def _searches(renter_id: str, count: int) -> list[dict[str, object]]:
    return [
        {
            "renter_id": renter_id,
            "event_type": "search",
            "event_timestamp": (
                datetime(2025, 3, 15 + i, 12, 0, 0)
                if 15 + i <= 28
                else datetime(2025, 4, (15 + i) - 28, 12, 0, 0)
            ),
            "property_id": f"p_{i}",
            "channel": "web",
            "utm_source": "x",
        }
        for i in range(count)
    ]


def _score(
    renter_id: str,
    prob: float,
    *,
    scored_on: date = DEFAULT_RUN_DATE,
    model_version: str = "v1",
    hour: int = 4,
) -> dict[str, object]:
    return {
        "renter_id": renter_id,
        "predicted_conversion_probability": prob,
        "model_version": model_version,
        "scored_at": datetime(scored_on.year, scored_on.month, scored_on.day, hour, 0, 0),
    }


def _run(duck, threshold: float) -> list[tuple[object, ...]]:
    sql = render_sql_for_duckdb(
        PART4_SQL,
        run_date=DEFAULT_RUN_DATE,
        extra_params={"threshold": str(threshold)},
    )
    return duck.execute(sql).fetchall()


def _ids(rows) -> set[str]:
    return {r[0] for r in rows}


def test_returns_score_columns(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_pass")])
    insert("renter_activity", _searches("r_pass", 3))
    insert("renter_send_scores", [_score("r_pass", 0.5)])

    sql = render_sql_for_duckdb(
        PART4_SQL, run_date=DEFAULT_RUN_DATE, extra_params={"threshold": "0.3"}
    )
    cur = duck.execute(sql)
    cols = [d[0] for d in cur.description]

    assert cols == [
        "renter_id",
        "email",
        "phone",
        "last_login",
        "search_count",
        "days_since_login",
        "predicted_conversion_probability",
        "model_version",
    ]


def test_threshold_filters_low_scores(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_high"),
            _profile("r_mid"),
            _profile("r_low"),
        ],
    )
    insert(
        "renter_activity", _searches("r_high", 3) + _searches("r_mid", 3) + _searches("r_low", 3)
    )
    insert(
        "renter_send_scores",
        [
            _score("r_high", 0.85),
            _score("r_mid", 0.31),  # just above threshold
            _score("r_low", 0.29),  # just below
        ],
    )

    assert _ids(_run(duck, threshold=0.30)) == {"r_high", "r_mid"}


def test_threshold_inclusive_at_boundary(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_eq")])
    insert("renter_activity", _searches("r_eq", 3))
    insert("renter_send_scores", [_score("r_eq", 0.30)])

    # >= 0.30 includes 0.30 exactly
    assert _ids(_run(duck, threshold=0.30)) == {"r_eq"}


def test_renters_without_scores_excluded(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_scored"), _profile("r_unscored")])
    insert("renter_activity", _searches("r_scored", 3) + _searches("r_unscored", 3))
    insert("renter_send_scores", [_score("r_scored", 0.7)])

    assert _ids(_run(duck, threshold=0.30)) == {"r_scored"}


def test_only_todays_scores_used(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_old"), _profile("r_today")])
    insert("renter_activity", _searches("r_old", 3) + _searches("r_today", 3))
    insert(
        "renter_send_scores",
        [
            _score("r_old", 0.95, scored_on=date(2025, 4, 28)),  # yesterday's score
            _score("r_today", 0.85, scored_on=DEFAULT_RUN_DATE),
        ],
    )

    assert _ids(_run(duck, threshold=0.30)) == {"r_today"}


def test_duplicate_scores_dedup_to_latest(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_dup")])
    insert("renter_activity", _searches("r_dup", 3))
    # Two rows for the same renter on the same day; latest is the keeper.
    insert(
        "renter_send_scores",
        [
            _score("r_dup", 0.10, hour=2, model_version="v1"),
            _score("r_dup", 0.95, hour=10, model_version="v2"),
        ],
    )

    rows = _run(duck, threshold=0.30)
    assert len(rows) == 1
    assert rows[0][6] == pytest.approx(0.95)
    assert rows[0][7] == "v2"


def test_eligibility_rules_still_enforced(duck, insert) -> None:
    """An ineligible renter with a great score must still be excluded."""
    insert(
        "renter_profiles",
        [
            _profile("r_active", subscription_status="active"),  # excluded by Part 1
            _profile("r_pass"),
        ],
    )
    insert("renter_activity", _searches("r_active", 3) + _searches("r_pass", 3))
    insert(
        "renter_send_scores",
        [
            _score("r_active", 0.99),
            _score("r_pass", 0.50),
        ],
    )

    assert _ids(_run(duck, threshold=0.30)) == {"r_pass"}


def test_no_scores_today_returns_empty(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_pass")])
    insert("renter_activity", _searches("r_pass", 3))
    # No scores written at all.

    assert _run(duck, threshold=0.30) == []


def test_results_ordered_by_score_desc(duck, insert) -> None:
    insert("renter_profiles", [_profile(f"r_{i}") for i in range(3)])
    insert("renter_activity", sum((_searches(f"r_{i}", 3) for i in range(3)), []))
    insert(
        "renter_send_scores",
        [
            _score("r_0", 0.40),
            _score("r_1", 0.90),
            _score("r_2", 0.65),
        ],
    )

    rows = _run(duck, threshold=0.30)
    probs = [r[6] for r in rows]
    assert probs == sorted(probs, reverse=True)
