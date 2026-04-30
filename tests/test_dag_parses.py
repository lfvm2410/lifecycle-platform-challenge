"""Smoke test that the DAG file parses cleanly under Airflow.

Skipped automatically when ``apache-airflow`` is not installed (the package
lives in the optional ``airflow`` Poetry group). CI runs this in a separate
job that installs the airflow group.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

airflow = pytest.importorskip("airflow", reason="apache-airflow not installed")

DAG_DIR = Path(__file__).resolve().parent.parent / "airflow" / "dags"


@pytest.fixture(scope="module")
def dag_bag():
    """Load every DAG under ``airflow/dags/`` once per test module."""
    from airflow.models import DagBag

    bag = DagBag(dag_folder=str(DAG_DIR), include_examples=False)
    assert not bag.import_errors, f"DAG import errors: {bag.import_errors}"
    return bag


def test_dag_imports_without_errors(dag_bag) -> None:
    assert "reactivation_campaign" in dag_bag.dags


def test_dag_schedule_and_concurrency(dag_bag) -> None:
    dag = dag_bag.get_dag("reactivation_campaign")
    assert dag.schedule_interval == "0 5 * * *"
    assert dag.max_active_runs == 1
    assert dag.catchup is False


def test_default_args_match_spec(dag_bag) -> None:
    dag = dag_bag.get_dag("reactivation_campaign")
    args = dag.default_args
    assert args["retries"] == 2
    assert args["retry_delay"] == timedelta(minutes=5)
    assert args["sla"] == timedelta(hours=3)


def test_tasks_in_linear_order(dag_bag) -> None:
    """The DAG is the Part-3 deliverable evolved with Part-4's freshness sensor.

    Linear chain (5 tasks):
      wait_for_model_freshness -> run_audience_query -> validate_audience
                               -> send_campaign      -> report_and_notify
    """
    dag = dag_bag.get_dag("reactivation_campaign")
    expected = [
        "wait_for_model_freshness",
        "run_audience_query",
        "validate_audience",
        "send_campaign",
        "report_and_notify",
    ]
    assert sorted(t.task_id for t in dag.tasks) == sorted(expected)

    by_id = {t.task_id: t for t in dag.tasks}
    assert by_id["wait_for_model_freshness"].upstream_task_ids == set()
    assert by_id["run_audience_query"].upstream_task_ids == {"wait_for_model_freshness"}
    assert by_id["validate_audience"].upstream_task_ids == {"run_audience_query"}
    assert by_id["send_campaign"].upstream_task_ids == {"validate_audience"}
    assert by_id["report_and_notify"].upstream_task_ids == {"send_campaign"}


def test_freshness_sensor_uses_reschedule_mode(dag_bag) -> None:
    dag = dag_bag.get_dag("reactivation_campaign")
    sensor = dag.get_task("wait_for_model_freshness")
    assert sensor.mode == "reschedule"
    assert sensor.poke_interval == 5 * 60
    assert sensor.timeout == 2 * 60 * 60


def test_failure_callback_is_wired(dag_bag) -> None:
    dag = dag_bag.get_dag("reactivation_campaign")
    assert dag.default_args.get("on_failure_callback") is not None
    assert dag.sla_miss_callback is not None
