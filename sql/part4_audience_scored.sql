-- =============================================================================
-- Part 4 - SMS Reactivation Audience with Value-Model Scoring
-- =============================================================================
-- Same eligibility rules as Part 1, intersected with a configurable threshold
-- on the data-science conversion-probability model.
--
-- Parameters:
--   @run_date   DATE    (idempotency anchor; defaults to today in scheduling)
--   @threshold  FLOAT64 (minimum predicted conversion probability)
--   @model_table STRING  Optional - defaults to ml_predictions.renter_send_scores;
--                       passed via Airflow rendering for multi-segment support.
--
-- Idempotency: every time-based filter is anchored on @run_date. The model
-- freshness predicate uses DATE(scored_at) = @run_date so a rerun on the
-- same UTC day yields identical results regardless of when the model job
-- finished within that day.
-- =============================================================================

DECLARE run_date  DATE     DEFAULT @run_date;
DECLARE threshold FLOAT64  DEFAULT @threshold;
DECLARE run_ts    TIMESTAMP DEFAULT TIMESTAMP(run_date);

WITH recent_searches AS (
    SELECT
        renter_id,
        COUNT(1) AS search_count
    FROM `renter_activity`
    WHERE event_type = 'search'
      AND event_timestamp >= TIMESTAMP_SUB(run_ts, INTERVAL 90 DAY)
      AND event_timestamp <  run_ts
    GROUP BY renter_id
    HAVING COUNT(1) >= 3
),

eligible_profiles AS (
    SELECT
        p.renter_id,
        p.email,
        p.phone,
        p.last_login
    FROM `renter_profiles` AS p
    WHERE p.subscription_status = 'churned'
      AND p.last_login IS NOT NULL
      AND p.last_login < TIMESTAMP_SUB(run_ts, INTERVAL 30 DAY)
      AND p.phone IS NOT NULL
      AND p.phone != ''
      AND p.sms_consent = TRUE
      AND (p.dnd_until IS NULL OR p.dnd_until <= run_ts)
),

-- One score per renter per day. If the model accidentally writes multiple
-- rows we take the most recent so the JOIN remains 1:1.
todays_scores AS (
    SELECT
        renter_id,
        predicted_conversion_probability,
        model_version,
        scored_at,
        ROW_NUMBER() OVER (
            PARTITION BY renter_id
            ORDER BY scored_at DESC
        ) AS rn
    FROM `ml_predictions.renter_send_scores`
    WHERE DATE(scored_at) = run_date
),

dedup_scores AS (
    SELECT renter_id, predicted_conversion_probability, model_version, scored_at
    FROM todays_scores
    WHERE rn = 1
)

SELECT
    e.renter_id,
    e.email,
    e.phone,
    e.last_login,
    s.search_count,
    TIMESTAMP_DIFF(run_ts, e.last_login, DAY) AS days_since_login,
    sc.predicted_conversion_probability,
    sc.model_version
FROM eligible_profiles AS e
INNER JOIN recent_searches AS s
    USING (renter_id)
INNER JOIN dedup_scores AS sc
    USING (renter_id)
LEFT JOIN `suppression_list` AS sup
    ON sup.renter_id = e.renter_id
WHERE sup.renter_id IS NULL
  AND sc.predicted_conversion_probability >= threshold
ORDER BY sc.predicted_conversion_probability DESC, e.renter_id;
