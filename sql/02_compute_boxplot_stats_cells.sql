-- Required psql vars:
--   :country_code   e.g. AGO
--   :start_date     e.g. 2024-01-01
--   :end_date       e.g. 2024-12-31

WITH base AS (
    SELECT
        r.country_code,
        r.week_start,
        m.cell_id,
        lower(coalesce(r.surface_group, 'unknown')) AS surface_group,
        r.era5_tp_sum_weekly_mm,
        r.era5_tp_1h_max_weekly_mm_per_h
    FROM eq.road_weekly_factors r
    JOIN eq.road_cell_map m
      ON m.country_code = r.country_code
     AND m.road_row_id = r.road_row_id
    WHERE r.country_code = :'country_code'
      AND r.week_start >= :'start_date'::date
      AND r.week_start <= :'end_date'::date
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
cell_scoped AS (
    SELECT
        country_code,
        week_start,
        scenario,
        'all'::text AS surface_scope,
        cell_id,
        AVG(era5_tp_sum_weekly_mm) AS era5_tp_sum_weekly_mm,
        AVG(era5_tp_1h_max_weekly_mm_per_h) AS era5_tp_1h_max_weekly_mm_per_h
    FROM scenario_rows
    GROUP BY country_code, week_start, scenario, cell_id

    UNION ALL

    SELECT
        country_code,
        week_start,
        scenario,
        'paved'::text AS surface_scope,
        cell_id,
        AVG(era5_tp_sum_weekly_mm) AS era5_tp_sum_weekly_mm,
        AVG(era5_tp_1h_max_weekly_mm_per_h) AS era5_tp_1h_max_weekly_mm_per_h
    FROM scenario_rows
    WHERE effective_surface = 'paved'
    GROUP BY country_code, week_start, scenario, cell_id

    UNION ALL

    SELECT
        country_code,
        week_start,
        scenario,
        'unpaved'::text AS surface_scope,
        cell_id,
        AVG(era5_tp_sum_weekly_mm) AS era5_tp_sum_weekly_mm,
        AVG(era5_tp_1h_max_weekly_mm_per_h) AS era5_tp_1h_max_weekly_mm_per_h
    FROM scenario_rows
    WHERE effective_surface = 'unpaved'
    GROUP BY country_code, week_start, scenario, cell_id
),
flat AS (
    SELECT
        country_code, week_start, scenario, surface_scope,
        'era5_tp_sum_weekly_mm'::text AS factor,
        era5_tp_sum_weekly_mm AS value
    FROM cell_scoped
    UNION ALL
    SELECT
        country_code, week_start, scenario, surface_scope,
        'era5_tp_1h_max_weekly_mm_per_h'::text AS factor,
        era5_tp_1h_max_weekly_mm_per_h AS value
    FROM cell_scoped
),
agg AS (
    SELECT
        country_code,
        week_start,
        scenario,
        surface_scope,
        factor,
        COUNT(*) FILTER (WHERE value IS NOT NULL) AS n_values,
        MIN(value) AS min_value,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY value) AS q25,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY value) AS median,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY value) AS q75,
        MAX(value) AS max_value
    FROM flat
    WHERE value IS NOT NULL
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
