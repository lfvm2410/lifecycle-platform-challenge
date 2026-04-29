# Part 4 - Value-Model Integration Design

This document describes how the SMS reactivation pipeline incorporates the
data-science conversion-probability model, and how the architecture remains
clean when the second model arrives in ~6 weeks.

> Concrete artefacts:
> - SQL: [`sql/part4_audience_scored.sql`](../sql/part4_audience_scored.sql)
> - Config: [`config/segments.yaml`](../config/segments.yaml)
> - DAG: [`airflow/dags/reactivation_campaign_dag.py`](../airflow/dags/reactivation_campaign_dag.py) (current shape; the multi-segment evolution is described below).

---

## 1. SQL: incorporating model scores

### Diff vs. Part 1

Two additions, both isolated in dedicated CTEs so the existing eligibility
logic is untouched:

1. **`todays_scores` CTE** filters `ml_predictions.renter_send_scores` to rows
   where `DATE(scored_at) = @run_date`. A `ROW_NUMBER()` window deduplicates
   on `renter_id` (taking the most recent row) so the JOIN against the
   audience is guaranteed 1:1 — defensive against an upstream model that
   accidentally emits duplicates.
2. **`INNER JOIN`** on `dedup_scores` with a parameterised `@threshold`.
   Using INNER JOIN means renters not present in today's scores are
   *excluded* by default (we never send to an unscored renter unless the
   operator explicitly opts into the `fallback_unscored` policy described in
   §3).

### Threshold parameterisation

`@threshold` is a script parameter, not a hard-coded literal, so:

- the same SQL serves multiple segments,
- production / staging / shadow runs can sweep thresholds without code
  changes,
- a tuning notebook can call the SQL with `@threshold = 0.0` to inspect the
  full score distribution.

### Idempotency under late-arriving model rows

The model job may rewrite rows for a given `renter_id` during the day. We
anchor on `DATE(scored_at) = @run_date` and pick the latest `scored_at` per
renter, so:

- a re-run later in the day uses the freshest score,
- a re-run anchored on the *same* `@run_date` parameter still produces the
  same set, because BigQuery's window functions are deterministic on a fixed
  input.

If we needed bit-for-bit reproducibility against an exact wall-clock instant,
we would also accept a `@score_cutoff_ts` parameter — out of scope for the
current ask but called out in `config/segments.yaml` for the future.

---

## 2. DAG: depending on a fresh model table

### Sensor placement

Add a single sensor task between `run_audience_query` and `validate_audience`:

```text
run_audience_query
        |
        v
wait_for_model_freshness   <-- new
        |
        v
validate_audience
        |
        v
send_campaign
        |
        v
report_and_notify
```

Implementation choice: **`BigQueryTablePartitionExistenceSensor`** when the
score table is daily-partitioned (the cheap path), or a custom
`@task.sensor` running `SELECT MAX(scored_at) FROM {model_table} WHERE
DATE(scored_at) = '{{ ds }}'` when it isn't:

```python
@task.sensor(poke_interval=300, timeout=2 * 3600, mode="reschedule")
def wait_for_model_freshness(model_table: str, ds: str) -> PokeReturnValue:
    bq = BigQueryHook(use_legacy_sql=False)
    row = bq.get_first(
        f"SELECT MAX(scored_at) FROM `{model_table}` "
        f"WHERE DATE(scored_at) = '{ds}'"
    )
    fresh = row and row[0] is not None
    return PokeReturnValue(is_done=bool(fresh))
```

`mode="reschedule"` releases the worker slot between pokes — important since
the model job typically takes 30-90 min and we don't want to monopolise a
slot.

### Multi-segment generalisation

Rather than copying the DAG per segment, we **dynamically map** the same
task group across the entries in `config/segments.yaml`:

```python
segments = load_segments()  # parsed at DAG-parse time

@task_group(group_id="segment")
def per_segment(segment: dict[str, Any]) -> None:
    staging = run_audience_query(segment)
    fresh = wait_for_model_freshness(segment["model_table"])
    validated = validate_audience(staging, fresh)
    summary = send_campaign(segment, validated)
    report_and_notify(segment, summary)

per_segment.expand(segment=segments)
```

A new model = a new YAML entry. No code change, no DAG redesign, and each
segment's failure does not affect the others (Airflow isolates mapped task
instances).

---

## 3. Handling a delayed model job

The sensor described above eventually times out (`freshness_sla` from the
YAML, default 2 h). What happens next is **per-segment policy**:

| `on_model_missing` | Behaviour | When to choose it |
| ------------------ | --------- | ----------------- |
| `wait` *(default)* | Sensor reschedules until SLA, then the segment task fails. The DAG-level `on_failure_callback` posts a Slack alert; the next-day run uses the still-current sent log so no double-send risk. | Standard reactivation segments where missing one day is acceptable. |
| `skip` | Sensor times out -> raise `AirflowSkipException`. The segment is recorded as "skipped (model unavailable)" in the reporting table. | High-precision segments where unscored sends would be actively harmful (e.g. premium audiences with low spam tolerance). |
| `fallback_unscored` | Switch the SQL template to `sql/part1_audience.sql`, send without scoring. | Rare. Best for time-critical campaigns (welcome series, retention saves) where the cost of *not* sending exceeds the cost of imprecise targeting. |

### The business tradeoff, made explicit

Two failure modes:

1. **Sending without scores** wastes SMS volume on low-converting renters,
   raises unsubscribe / spam-flag rates, and erodes long-term deliverability
   reputation. The hidden cost compounds.
2. **Not sending at all** loses a day of reactivation revenue but is
   recoverable: the audience rolls into tomorrow's run. The cost is bounded.

For most segments we therefore prefer `wait` -> escalate -> retry tomorrow
over `fallback_unscored`. The YAML knob exists so an exception can be made
explicitly, with a paper trail, rather than as a silent failure mode.

---

## 4. Why this design ages well

- **Config, not code, for the second model.** Adding a segment is a small
  YAML diff that a non-engineer could review.
- **Per-segment thresholds and freshness SLAs.** Marketing can tune one
  segment without touching another.
- **Per-segment failure isolation.** Airflow's mapped task instances mean
  segment B keeps shipping if segment A's model is delayed.
- **One canonical SQL.** Both the unscored and scored paths live in `sql/`
  and share the eligibility CTE structure; future audit / refactor stays
  cheap.
- **Tested.** The Part 4 SQL has its own test suite
  (`tests/test_sql_part4_scored.py`) covering the threshold filter, the
  freshness predicate, and the multi-row dedup behaviour.
