CREATE SCHEMA IF NOT EXISTS eq;

-- Weekly road-level factors already sampled onto roads.
-- This table is the DB-only contract for all downstream analytics.
CREATE TABLE IF NOT EXISTS eq.road_weekly_factors (
    country_code text NOT NULL,
    week_start date NOT NULL,
    road_row_id bigint NOT NULL,
    surface_group text NOT NULL,
    era5_tp_sum_weekly_mm double precision,
    era5_tp_1h_max_weekly_mm_per_h double precision,
    geometry geometry(Point, 4326),
    PRIMARY KEY (country_code, week_start, road_row_id)
);

CREATE INDEX IF NOT EXISTS road_weekly_factors_country_week_idx
    ON eq.road_weekly_factors (country_code, week_start);
CREATE INDEX IF NOT EXISTS road_weekly_factors_surface_idx
    ON eq.road_weekly_factors (surface_group);
CREATE INDEX IF NOT EXISTS road_weekly_factors_geometry_gist
    ON eq.road_weekly_factors USING GIST (geometry);

-- Source-specific cell-to-road mapping for cell-level analytics.
CREATE TABLE IF NOT EXISTS eq.cell_road_segments (
    country_code text NOT NULL,
    source text NOT NULL,
    cell_m double precision NOT NULL,
    cell_id text NOT NULL,
    cell_ix bigint NOT NULL,
    cell_iy bigint NOT NULL,
    road_row_id bigint NOT NULL,
    PRIMARY KEY (country_code, source, cell_m, road_row_id)
);

CREATE INDEX IF NOT EXISTS cell_road_segments_country_source_cell_idx
    ON eq.cell_road_segments (country_code, source, cell_m, cell_id);

-- DB-native boxplot stats output.
CREATE TABLE IF NOT EXISTS eq.boxplot_stats_weekly (
    country_code text NOT NULL,
    week_start date NOT NULL,
    scenario text NOT NULL,
    surface_scope text NOT NULL,
    factor text NOT NULL,
    n_values bigint NOT NULL,
    min_value double precision,
    q25 double precision,
    median double precision,
    q75 double precision,
    max_value double precision,
    PRIMARY KEY (country_code, week_start, scenario, surface_scope, factor)
);

CREATE INDEX IF NOT EXISTS boxplot_stats_weekly_country_week_idx
    ON eq.boxplot_stats_weekly (country_code, week_start);
