-- =============================================================================
-- Part 1 - SMS Reactivation Audience
-- =============================================================================
-- Builds the daily audience for the SMS reactivation campaign.
--
-- Eligibility rules (all must hold):
--   1. Last login was strictly more than 30 days ago.
--   2. subscription_status = 'churned'.
--   3. At least 3 'search' events in the trailing 90 days.
--   4. phone IS NOT NULL AND phone != ''.
--   5. sms_consent = TRUE.
--   6. Renter is NOT present in suppression_list (any reason).
--   7. dnd_until IS NULL OR dnd_until <= run timestamp (no active DND).
--
-- Idempotency
-- -----------
-- Every time-based filter is anchored on the script parameter @run_date so a
-- rerun on the same UTC day yields a byte-identical result set. To execute
-- ad-hoc, run with:
--     bq query --use_legacy_sql=false \
--         --parameter='run_date:DATE:2025-04-29' \
--         "$(cat sql/part1_audience.sql)"
--
-- When called from Airflow we pass {{ ds }} as the @run_date parameter.
-- =============================================================================

DECLARE run_date DATE DEFAULT @run_date;
DECLARE run_ts   TIMESTAMP DEFAULT TIMESTAMP(run_date);

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
)

SELECT
    e.renter_id,
    e.email,
    e.phone,
    e.last_login,
    s.search_count,
    TIMESTAMP_DIFF(run_ts, e.last_login, DAY) AS days_since_login
FROM eligible_profiles AS e
INNER JOIN recent_searches AS s
    USING (renter_id)
LEFT JOIN `suppression_list` AS sup
    ON sup.renter_id = e.renter_id
WHERE sup.renter_id IS NULL
ORDER BY e.renter_id;
