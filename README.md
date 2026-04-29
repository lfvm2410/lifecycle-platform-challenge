# Lifecycle Platform Challenge

Reference implementation for the Senior Lifecycle Platform Engineer take-home.
The repository covers the five parts of the prompt:

| Part | Topic | Deliverable |
| ---- | ----- | ----------- |
| 1 | Audience segmentation (BigQuery SQL) | [`sql/part1_audience.sql`](sql/part1_audience.sql) |
| 2 | Pipeline orchestration (Python) | [`src/lifecycle_platform/`](src/lifecycle_platform/) |
| 3 | Airflow DAG skeleton | [`airflow/dags/reactivation_campaign_dag.py`](airflow/dags/reactivation_campaign_dag.py) |
| 4 | Value-model integration (design) | [`docs/part4_value_model_design.md`](docs/part4_value_model_design.md) + [`sql/part4_audience_scored.sql`](sql/part4_audience_scored.sql) + [`config/segments.yaml`](config/segments.yaml) |
| 5 | Observability design | [`docs/part5_observability.md`](docs/part5_observability.md) |

> AI usage logs live in [`ai-session/`](ai-session/) — see the AI Fluency note at
> the bottom of this file.

## Quick start

Requirements: Python 3.11 (a `.python-version` file pins `3.11.8`) and Poetry 1.8+.

```bash
# 1. Install runtime + dev deps into an in-project .venv
make install

# 2. Run the test suite (SQL harness + Python unit tests)
make test

# 3. Lint, type-check, and test together
make check

# 4. (Optional) install Airflow and parse the DAG
make install-airflow
make dag-check
```

The Airflow group is **opt-in** because it pulls a large dependency tree that is
not needed for the SQL or sender tests. CI runs `make check` for the fast lane
and `make install-airflow dag-check` as a separate job.

## Layout

```
sql/                            BigQuery queries (Parts 1 and 4)
src/lifecycle_platform/         Python package: ESP sender, dedup, backoff, batching
airflow/dags/                   DAG skeleton for daily campaign send
config/segments.yaml            Per-segment campaign configuration (Part 4)
docs/                           Part 4 (value model) and Part 5 (observability) writeups
tests/                          pytest suite + DuckDB SQL harness (in-memory dict fixtures)
ai-session/                     Exported AI assistant chats (AI Fluency)
```

## Assumptions

1. **BigQuery dialect**: queries use `@run_date` script parameters
   (`DECLARE`/`SET`) so they are idempotent on re-run. Tests transpile them to
   DuckDB via `sqlglot` so the suite runs offline.
2. **The `ESPClient` is provided by the platform**; the in-repo class is a
   thin stub matching the documented signature. It is replaced by a fake in tests.
3. **Sent-log persistence**: the local file-based dedup layer is appropriate
   for a single-worker Airflow task. In production, the same interface would be
   backed by a transactional store (BigQuery `MERGE` or Redis). Discussed in
   the "with more time" section.
4. **Airflow 2.9.x** with the TaskFlow API. The DAG itself only imports stable
   public operators; provider extras are noted inline.
5. **Slack webhook**, **BigQuery service account**, and **ESP API key** are
   referenced via Airflow Connections / Variables — no secrets are stored
   in the repo.

## Design decisions and tradeoffs

- **Poetry** over plain `pip` to lock transitive dependencies and make the
  Airflow group opt-in.
- **`structlog`** for JSON logging — every log line carries `campaign_id`,
  `batch_index`, `attempt`, etc., so the same logs feed Datadog ingestion.
- **`tenacity`** for retry/backoff. Re-implementing this is a 30-line
  exercise, but using a battle-tested library is the production-shaped choice
  and the wrapper is small enough to read in one sitting.
- **DuckDB + sqlglot** for SQL tests so reviewers can run the full suite with
  no GCP credentials. The canonical SQL stays in BigQuery dialect.
- **YAML-driven segment config** (Part 4) so adding the second predictive
  model in 6 weeks is a configuration PR, not code.
- **Layered double-send defense** (Part 5): `max_active_runs=1`,
  per-`(campaign_id, renter_id)` dedup, date-suffixed staging tables, and a
  unique constraint on the reporting table.

## What I would do differently with more time

- Replace the file-based sent log with a BigQuery `MERGE` against a
  `lifecycle.sent_log` table (true transactional dedup across workers).
- Add an end-to-end integration test against a sandboxed ESP using
  `responses` / `pytest-httpserver`.
- Provision the BigQuery datasets and Airflow connections via Terraform.
- Emit OpenLineage events from the DAG to wire it into a data-catalog.
- Ship a Datadog dashboard JSON in `observability/datadog/` and an Airflow
  Helm values file in `deploy/`.
- Build a Pydantic model for the segment config and validate it on DAG parse.

## AI Fluency

The full Cursor chat transcript that produced this repo is in
[`ai-session/cursor-log.md`](ai-session/cursor-log.md). The session shows the
plan-first workflow: clarifying questions, an explicit plan reviewed before
any code was written, and a step-by-step implementation that kept tests green
at each milestone.
