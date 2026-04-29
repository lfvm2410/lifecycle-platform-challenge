# Part 5 - Observability Design

## 1. Datadog metrics and alerts

The pipeline emits metrics at three layers: Airflow (DAG/task durations and
states), the campaign sender (one counter and one histogram per batch), and
the BigQuery query layer (job byte/row counts via the GCP integration).
The headline custom metrics, all tagged with `dag_id`, `campaign_id`, and
`segment_id`, are: `airflow.dag.duration` (histogram, monitor warns at p95
> 2 h and pages on SLA miss); `lifecycle.audience.size` (gauge, anomaly
monitor on `anomalies(avg:lifecycle.audience.size{*}, 'agile', 3)` against
the 14-day baseline; warn ±2σ, page ±3σ); `lifecycle.esp.requests` (counter
tagged with `status_code`; monitor on `error_rate{status_code:5xx} > 5%`
over 10 min); and `lifecycle.campaign.send_completion_ratio = sent/(sent +
failed)` exposed as an SLO with a 99% target on a 30-day window. We also
import `airflow.sla_miss` from the official Airflow integration so the SLA
defined in `default_args` (`timedelta(hours=3)`) becomes a monitor without
extra code. Each monitor links to a runbook entry and the matching
Airflow log query so the on-call can triage in under a minute.

The dashboard groups these into four columns — *Schedule health*,
*Audience size*, *ESP delivery*, and *Business outcome (sent vs.
unsubscribes)* — and adds a separate "deploy markers" overlay (release
tags + DAG file checksum changes) so a regression that lands with a deploy
is obvious. The send-completion SLO budget is the single most useful
artifact for engineering reviews: if we burn >25% of the monthly budget
in any week, we automatically open a "freeze new sends" issue.

## 2. Detecting and preventing double-sends

Defense in depth, four layers, each catching a different failure mode:
**(a) Airflow-level** `max_active_runs=1` plus `catchup=False` on the DAG
prevent a manual re-trigger and a scheduled run from racing; (b) the
**staging table is date-suffixed** (`staging.reactivation_audience_{{
ds_nodash }}`) and written with `CREATE OR REPLACE` so reruns hit the same
audience snapshot; (c) the **per-campaign sent log**
(`SentLog.extend(campaign_id, ids)`) is consulted before every batch and
written incrementally after every successful batch — re-running on the same
day finds every renter in the log and skips them; (d) the **reporting
table has a `UNIQUE(campaign_id, run_date, renter_id)` constraint**
enforced via BigQuery `MERGE` (and a matching `insertId` on streaming
inserts), so even a logic bug in (a)–(c) cannot create duplicate billable
rows downstream. Datadog watches all of these: `airflow.dag.concurrent_runs
> 1` pages immediately, and a daily check joins the sent log against the
ESP's delivery export to fire on `delta > 0`. In production we would
replace the file-based `SentLog` with a BigQuery `MERGE` against a
`lifecycle.sent_log` table so the dedup is transactional across multiple
workers — the file-based store is fine for the single-process Airflow task
shown here but does not survive horizontal scale-out.

## 3. ESP outage / circuit-breaker recovery

The send wraps each batch in a per-process **circuit breaker** (a small
state machine; `pybreaker` works, or ~40 lines of code). The breaker
counts consecutive non-2xx responses; after `N=5` it opens, halts the
remaining batches in the run, and raises a *retryable* Airflow exception
(`AirflowException`, not `AirflowFailException`). Airflow then waits
`retry_delay=5 min` and retries the task; on retry, the sent log
short-circuits every batch we already delivered, so resumption is cheap and
correct. The breaker's half-open probe sends a single canary batch (size
1) before allowing the run to resume — preventing a thundering-herd recovery
that pushes the still-fragile ESP back into 5xx. If the outage outlasts
all task retries the DAG fails and the on-failure callback posts to Slack;
the next day's run picks up automatically because the sent log is durable
across runs and contains everyone we already delivered to. The two
guardrails we explicitly *don't* rely on are (a) "let it 429-loop forever"
— our `max_attempts=5` rate-limit policy ensures one bad batch never
monopolises a worker; and (b) "buffer to disk and replay later" — the
sent-log + retry-with-dedup pattern delivers the same eventual consistency
without needing a separate replay process to reason about.
