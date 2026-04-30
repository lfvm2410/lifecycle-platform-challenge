"""Tests for sql/part1_audience.sql.

Each test seeds a small in-memory DuckDB database with profiles, activity, and
suppression rows that target a specific eligibility rule, then asserts that the
audience query keeps or excludes the renter for the right reason.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tests.conftest import DEFAULT_RUN_DATE, SQL_DIR, render_sql_for_duckdb

PART1_SQL = SQL_DIR / "part1_audience.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(renter_id: str, **overrides: object) -> dict[str, object]:
    """Build a renter_profiles row that defaults to passing every rule.

    Tests then override the single field they want to test.
    """
    base: dict[str, object] = {
        "renter_id": renter_id,
        "email": f"{renter_id}@example.com",
        "phone": "+15555550100",
        "last_login": datetime(2025, 1, 15, 10, 0, 0),  # 104 days before run_date
        "subscription_status": "churned",
        "sms_consent": True,
        "email_consent": True,
        "dnd_until": None,
        "created_at": datetime(2024, 1, 1),
    }
    base.update(overrides)
    return base


def _searches(renter_id: str, count: int, base_day: int = 15) -> list[dict[str, object]]:
    """Build `count` 'search' events all comfortably inside the 90-day window."""
    return [
        {
            "renter_id": renter_id,
            "event_type": "search",
            "event_timestamp": datetime(2025, 3, base_day, 12, 0, 0).replace(
                day=min(base_day + i, 28)
            ),
            "property_id": f"p_{i}",
            "channel": "web",
            "utm_source": "organic",
        }
        for i in range(count)
    ]


def _run_audience(duck) -> list[tuple[object, ...]]:
    """Render and execute Part 1 against the active DuckDB connection."""
    sql = render_sql_for_duckdb(PART1_SQL, run_date=DEFAULT_RUN_DATE)
    return duck.execute(sql).fetchall()


def _renter_ids(rows: list[tuple[object, ...]]) -> set[str]:
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_required_columns(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_pass")])
    insert("renter_activity", _searches("r_pass", 3))

    sql = render_sql_for_duckdb(PART1_SQL, run_date=DEFAULT_RUN_DATE)
    cursor = duck.execute(sql)
    column_names = [d[0] for d in cursor.description]

    assert column_names == [
        "renter_id",
        "email",
        "phone",
        "last_login",
        "search_count",
        "days_since_login",
    ]


def test_happy_path_includes_eligible_renter(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_pass")])
    insert("renter_activity", _searches("r_pass", 5))

    rows = _run_audience(duck)

    assert len(rows) == 1
    renter_id, email, phone, last_login, search_count, days_since_login = rows[0]
    assert renter_id == "r_pass"
    assert email == "r_pass@example.com"
    assert phone == "+15555550100"
    assert search_count == 5
    # 2025-04-29 minus 2025-01-15 = 104 days
    assert days_since_login == 104


def test_excludes_active_subscription(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_active", subscription_status="active"),
            _profile("r_never", subscription_status="never_subscribed"),
            _profile("r_churned"),
        ],
    )
    insert(
        "renter_activity",
        _searches("r_active", 3) + _searches("r_never", 3) + _searches("r_churned", 3),
    )

    assert _renter_ids(_run_audience(duck)) == {"r_churned"}


def test_excludes_recent_login(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            # 30 days exactly is NOT > 30, so excluded; 31 days is included.
            _profile("r_30_days", last_login=datetime(2025, 3, 30, 0, 0, 0)),
            _profile("r_31_days", last_login=datetime(2025, 3, 29, 0, 0, 0)),
        ],
    )
    insert("renter_activity", _searches("r_30_days", 3) + _searches("r_31_days", 3))

    assert _renter_ids(_run_audience(duck)) == {"r_31_days"}


def test_excludes_missing_or_empty_phone(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_no_phone", phone=None),
            _profile("r_empty_phone", phone=""),
            _profile("r_with_phone"),
        ],
    )
    insert(
        "renter_activity",
        _searches("r_no_phone", 3) + _searches("r_empty_phone", 3) + _searches("r_with_phone", 3),
    )

    assert _renter_ids(_run_audience(duck)) == {"r_with_phone"}


def test_excludes_no_sms_consent(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_no_consent", sms_consent=False),
            _profile("r_consent"),
        ],
    )
    insert("renter_activity", _searches("r_no_consent", 3) + _searches("r_consent", 3))

    assert _renter_ids(_run_audience(duck)) == {"r_consent"}


def test_excludes_active_dnd(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_dnd_future", dnd_until=datetime(2099, 1, 1)),
            _profile("r_dnd_past", dnd_until=datetime(2024, 1, 1)),
            _profile("r_dnd_null", dnd_until=None),
        ],
    )
    insert(
        "renter_activity",
        _searches("r_dnd_future", 3) + _searches("r_dnd_past", 3) + _searches("r_dnd_null", 3),
    )

    assert _renter_ids(_run_audience(duck)) == {"r_dnd_past", "r_dnd_null"}


def test_excludes_suppressed_for_any_reason(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_unsub"),
            _profile("r_bounced"),
            _profile("r_clean"),
        ],
    )
    insert(
        "renter_activity",
        _searches("r_unsub", 3) + _searches("r_bounced", 3) + _searches("r_clean", 3),
    )
    insert(
        "suppression_list",
        [
            {
                "renter_id": "r_unsub",
                "suppression_reason": "unsubscribed",
                "suppressed_at": datetime(2024, 6, 1),
            },
            {
                "renter_id": "r_bounced",
                "suppression_reason": "bounced",
                "suppressed_at": datetime(2024, 6, 1),
            },
        ],
    )

    assert _renter_ids(_run_audience(duck)) == {"r_clean"}


def test_search_count_threshold(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_2"),
            _profile("r_3"),
            _profile("r_10"),
            _profile("r_0"),
        ],
    )
    insert("renter_activity", _searches("r_2", 2) + _searches("r_3", 3) + _searches("r_10", 10))

    rows = _run_audience(duck)
    by_id = {row[0]: row[4] for row in rows}  # renter_id -> search_count
    assert by_id == {"r_3": 3, "r_10": 10}


def test_only_search_events_counted(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_mixed")])
    # 2 searches + 5 page_views in window: should be excluded (< 3 searches)
    insert(
        "renter_activity",
        _searches("r_mixed", 2)
        + [
            {
                "renter_id": "r_mixed",
                "event_type": "page_view",
                "event_timestamp": datetime(2025, 3, 20),
                "property_id": "p",
                "channel": "web",
                "utm_source": "x",
            }
            for _ in range(5)
        ],
    )

    assert _run_audience(duck) == []


def test_searches_outside_90_day_window_ignored(duck, insert) -> None:
    insert("renter_profiles", [_profile("r_window")])
    # 2 in-window searches + 5 old searches (well outside 90 days) -> excluded
    in_window = _searches("r_window", 2)
    out_of_window = [
        {
            "renter_id": "r_window",
            "event_type": "search",
            "event_timestamp": datetime(2024, 6, 1),
            "property_id": "p",
            "channel": "web",
            "utm_source": "x",
        }
        for _ in range(5)
    ]
    insert("renter_activity", in_window + out_of_window)

    assert _run_audience(duck) == []


def test_excludes_null_last_login(duck, insert) -> None:
    insert(
        "renter_profiles",
        [
            _profile("r_null_login", last_login=None),
            _profile("r_normal"),
        ],
    )
    insert("renter_activity", _searches("r_null_login", 3) + _searches("r_normal", 3))

    assert _renter_ids(_run_audience(duck)) == {"r_normal"}


def test_query_is_idempotent(duck, insert) -> None:
    insert("renter_profiles", [_profile("a"), _profile("b")])
    insert("renter_activity", _searches("a", 3) + _searches("b", 4))

    first = _run_audience(duck)
    second = _run_audience(duck)

    assert first == second
    assert _renter_ids(first) == {"a", "b"}


@pytest.mark.parametrize(
    ("run_date", "expected_days"),
    [
        (date(2025, 4, 29), 104),
        (date(2025, 5, 30), 135),
    ],
)
def test_days_since_login_anchored_on_run_date(duck, insert, run_date, expected_days) -> None:
    insert("renter_profiles", [_profile("r_pass")])
    insert("renter_activity", _searches("r_pass", 3))

    sql = render_sql_for_duckdb(PART1_SQL, run_date=run_date)
    rows = duck.execute(sql).fetchall()
    assert rows[0][5] == expected_days
