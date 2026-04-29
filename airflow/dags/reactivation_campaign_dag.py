"""Daily SMS reactivation campaign DAG.

Pipeline (linear, 4 tasks):

    run_audience_query  ->  validate_audience  ->  send_campaign  ->  report_and_notify

Operational requirements
------------------------
* schedule       : daily at 05:00 UTC
* retries        : 2 per task, 5-minute delay
* SLA            : 3 hours (alert if not finished by 08:00 UTC)
* concurrency    : ``max_active_runs=1`` is the first line of defense against
                   double-sends triggered by manual re-runs.
* idempotency    : the staging table is suffixed with ``{{ ds_nodash }}`` so a
                   re-run on the same day overwrites itself; the sender's
                   per-campaign sent log filters anything we have already
                   delivered to.

Provider imports
----------------
We keep BigQuery and Slack imports *lazy* (inside task bodies). That way the
DAG parses with a vanilla ``apache-airflow`` install — useful for CI and for
reviewers who don't want to pull the entire Google / Slack provider stacks
just to lint the DAG. Production deployments add the providers to
``requirements.txt``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException, AirflowSkipException

# Make the in-repo ``src`` importable from the Airflow worker. In production
# the package would be ``pip install``ed into the worker image.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Constants - in production these are Airflow Variables / Connections
# ---------------------------------------------------------------------------

CAMPAIGN_ID = "reactivation_sms_v1"
GCP_PROJECT = "lifecycle-prod"
DATASET = "lifecycle"
STAGING_DATASET = "staging"
REPORTING_TABLE = f"{GCP_PROJECT}.reporting.campaign_runs"
SLACK_CONN_ID = "slack_lifecycle_alerts"
ESP_API_KEY_VAR = "esp_api_key"
SENT_LOG_PATH = "/var/lifecycle/sent_renters.json"
ANOMALY_MULTIPLIER = 2.0  # validate fails if today's audience > 2x rolling mean


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _slack_notify(text: str) -> None:
    """Send a single message to the lifecycle alerts Slack channel.

    Imported lazily so DAG parse works without the slack provider installed.
    """
    try:
        from airflow.providers.slack.hooks.slack_webhook import SlackWebhookHook
    except ImportError:  # pragma: no cover - production has the provider
        import logging

        logging.getLogger(__name__).warning("slack provider not installed; alert: %s", text)
        return
    SlackWebhookHook(slack_webhook_conn_id=SLACK_CONN_ID).send(text=text)


def _on_failure(context: dict[str, Any]) -> None:
    task_id = context.get("task_instance").task_id  # type: ignore[union-attr]
    dag_id = context.get("task_instance").dag_id  # type: ignore[union-attr]
    exec_date = context.get("logical_date") or context.get("execution_date")
    _slack_notify(
        f":rotating_light: *{dag_id}* / `{task_id}` failed at {exec_date} - " f"see Airflow logs."
    )


def _on_sla_miss(dag, task_list, blocking_task_list, slas, blocking_tis) -> None:  # noqa: ANN001
    _slack_notify(
        f":warning: SLA miss on *{dag.dag_id}* "
        f"({len(slas)} task(s) overdue): {[s.task_id for s in slas]}"
    )


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

DEFAULT_ARGS: dict[str, Any] = {
    "owner": "lifecycle-platform",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "sla": timedelta(hours=3),
    "email_on_failure": False,
    "email_on_retry": False,
    "on_failure_callback": _on_failure,
}


@dag(
    dag_id="reactivation_campaign",
    description="Daily SMS reactivation send (Part 3 deliverable).",
    schedule="0 5 * * *",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    sla_miss_callback=_on_sla_miss,
    tags=["lifecycle", "sms", "reactivation"],
)
def reactivation_campaign() -> None:
    """Define the four-task linear pipeline."""

    @task
    def run_audience_query(ds: str) -> str:
        """Materialise today's audience into a date-suffixed staging table.

        Returns the fully qualified staging table id so downstream tasks have
        a single source of truth (passed through XCom).
        """
        from airflow.providers.google.cloud.operators.bigquery import (
            BigQueryInsertJobOperator,
        )

        staging_table = (
            f"{GCP_PROJECT}.{STAGING_DATASET}." f"reactivation_audience_{ds.replace('-', '')}"
        )
        sql_path = _REPO_ROOT / "sql" / "part1_audience.sql"
        rendered = sql_path.read_text()

        op = BigQueryInsertJobOperator(
            task_id="_inline_bq_audience",
            configuration={
                "query": {
                    "query": (f"CREATE OR REPLACE TABLE `{staging_table}` AS\n" f"{rendered}"),
                    "useLegacySql": False,
                    "queryParameters": [
                        {
                            "name": "run_date",
                            "parameterType": {"type": "DATE"},
                            "parameterValue": {"value": ds},
                        }
                    ],
                }
            },
            location="US",
        )
        op.execute(context={})
        return staging_table

    @task
    def validate_audience(staging_table: str) -> dict[str, Any]:
        """Block sends on empty or anomalously large audiences.

        Compares today's row count to the 14-day rolling mean from the
        reporting table. Fails (not skips) on anomaly so an operator must
        review before the next run. Returns a dict that carries both the
        staging table and the validated audience size so the downstream
        task has a single upstream dependency (strict linear DAG).
        """
        from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

        bq = BigQueryHook(use_legacy_sql=False)
        size = bq.get_first(f"SELECT COUNT(1) FROM `{staging_table}`")[0]  # type: ignore[index]

        if size == 0:
            raise AirflowFailException("Audience query returned 0 rows; aborting send.")

        baseline_row = bq.get_first(
            f"SELECT AVG(audience_size) FROM `{REPORTING_TABLE}` "
            f"WHERE run_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY) "
            f"AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        )
        baseline = (baseline_row or [None])[0]
        if baseline and size > ANOMALY_MULTIPLIER * baseline:
            raise AirflowFailException(
                f"Audience size {size} exceeds {ANOMALY_MULTIPLIER}x the "
                f"14-day baseline ({baseline:.0f}); manual review required."
            )

        return {"staging_table": staging_table, "audience_size": int(size)}

    @task
    def send_campaign(validated: dict[str, Any]) -> dict[str, Any]:
        """Stream the staging table to the ESP via execute_campaign_send."""

        staging_table = validated["staging_table"]
        audience_size = validated["audience_size"]

        if audience_size == 0:
            raise AirflowSkipException("Empty audience - nothing to send.")

        from airflow.models import Variable
        from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

        from lifecycle_platform.campaign_sender import execute_campaign_send
        from lifecycle_platform.esp_client import ESPClient

        bq = BigQueryHook(use_legacy_sql=False)
        rows = bq.get_records(f"SELECT renter_id, email, phone FROM `{staging_table}`")
        audience = [{"renter_id": r[0], "email": r[1], "phone": r[2]} for r in rows]

        esp = ESPClient(api_key=Variable.get(ESP_API_KEY_VAR))
        return execute_campaign_send(
            campaign_id=CAMPAIGN_ID,
            audience=audience,
            esp_client=esp,
            sent_log_path=SENT_LOG_PATH,
        )

    @task
    def report_and_notify(summary: dict[str, Any], ds: str) -> None:
        """Persist the summary + post a Slack message with the headline numbers."""

        from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

        bq = BigQueryHook(use_legacy_sql=False)
        bq.insert_all(
            project_id=GCP_PROJECT,
            dataset_id="reporting",
            table_id="campaign_runs",
            rows=[
                {
                    "json": {
                        "campaign_id": CAMPAIGN_ID,
                        "run_date": ds,
                        "audience_size": summary["total_sent"]
                        + summary["total_failed"]
                        + summary["total_skipped"],
                        "total_sent": summary["total_sent"],
                        "total_failed": summary["total_failed"],
                        "total_skipped": summary["total_skipped"],
                        "elapsed_seconds": summary["elapsed_seconds"],
                    },
                    # Idempotency key matches the unique constraint on the
                    # reporting table (campaign_id, run_date) - retries are no-op.
                    "insertId": f"{CAMPAIGN_ID}:{ds}",
                }
            ],
        )

        _slack_notify(
            f":white_check_mark: *{CAMPAIGN_ID}* {ds}\n"
            f"sent={summary['total_sent']} "
            f"failed={summary['total_failed']} "
            f"skipped={summary['total_skipped']} "
            f"elapsed={summary['elapsed_seconds']}s"
        )

    # ------------------------------------------------------------------
    # Linear dependency graph
    # ------------------------------------------------------------------
    staging = run_audience_query()
    validated = validate_audience(staging)
    summary = send_campaign(validated)
    report_and_notify(summary)


dag_instance = reactivation_campaign()
