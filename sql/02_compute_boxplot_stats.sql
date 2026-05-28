-- Required psql vars:
--   :country_code   e.g. AGO
--   :start_date     e.g. 2024-01-01
--   :end_date       e.g. 2024-12-31

WITH base AS (
    SELECT
        country_code,
        week_start,
        lower(coalesce(surface_group, 'unknown')) AS surface_group,
        era5_tp_sum_weekly_mm,
        era5_tp_1h_max_weekly_mm_per_h
    FROM eq.road_weekly_factors
    WHERE country_code = :'country_code'
      AND week_start >= :'start_date'::date
      AND week_start <= :'end_date'::date
),
scenario_rows AS (
    SELECT
        b.*,
        s.scenario,
        CASE
            WHEN s.scenario = 'actual_unpaved' THEN b.surface_group
            WHEN s.scenario = 'unknown_as_paved' THEN CASE WHEN b.surface_group = 'unknown' THEN 'paved' ELSE b.surface_group END
            WHEN s.scenario = 'unknown_as_unpaved' THEN CASE WHEN b.surface_group = 'unknown' THEN 'unpaved' ELSE b.surface_group END
            ELSE b.surface_group
        END AS effective_surface
    FROM base b
    CROSS JOIN (VALUES
        ('actual_unpaved'),
        ('unknown_as_paved'),
        ('unknown_as_unpaved')
    ) AS s(scenario)
),
flat AS (
    SELECT
        s.country_code,
        s.week_start,
        s.scenario,
        scope.surface_scope,
        factor.factor,
        factor.value
    FROM scenario_rows s
    CROSS JOIN LATERAL (
        VALUES
            ('all'::text, true),
            ('paved'::text, s.effective_surface = 'paved'),
            ('unpaved'::text, s.effective_surface = 'unpaved')
    ) AS scope(surface_scope, keep_row)
    CROSS JOIN LATERAL (
        VALUES
            ('era5_tp_sum_weekly_mm'::text, s.era5_tp_sum_weekly_mm),
            ('era5_tp_1h_max_weekly_mm_per_h'::text, s.era5_tp_1h_max_weekly_mm_per_h)
    ) AS factor(factor, value)
    WHERE scope.keep_row
      AND factor.value IS NOT NULL
),
agg AS (
    SELECT
        country_code,
        week_start,
        scenario,
        surface_scope,
        factor,
        COUNT(*) AS n_values,
        MIN(value) AS min_value,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY value) AS q25,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY value) AS median,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY value) AS q75,
        MAX(value) AS max_value
    FROM flat
    GROUP BY country_code, week_start, scenario, surface_scope, factor
)
INSERT INTO eq.boxplot_stats_weekly (
    country_code, week_start, scenario, surface_scope, factor,
    n_values, min_value, q25, median, q75, max_value
)
SELECT
    country_code, week_start, scenario, surface_scope, factor,
    n_values, min_value, q25, median, q75, max_value
FROM agg
ON CONFLICT (country_code, week_start, scenario, surface_scope, factor)
DO UPDATE SET
    n_values = EXCLUDED.n_values,
    min_value = EXCLUDED.min_value,
    q25 = EXCLUDED.q25,
    median = EXCLUDED.median,
    q75 = EXCLUDED.q75,
    max_value = EXCLUDED.max_value;
