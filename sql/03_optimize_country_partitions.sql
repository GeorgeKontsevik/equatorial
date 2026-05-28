CREATE SCHEMA IF NOT EXISTS eq;

CREATE OR REPLACE FUNCTION eq.ensure_list_partition(
    p_schema text,
    p_parent text,
    p_country_code text
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_child text;
BEGIN
    v_child := format('%s_%s', p_parent, lower(p_country_code));
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I FOR VALUES IN (%L)',
        p_schema, v_child, p_schema, p_parent, upper(p_country_code)
    );
END
$$;

DO $$
DECLARE
    v_relkind char;
BEGIN
    SELECT c.relkind
      INTO v_relkind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'eq' AND c.relname = 'road_weekly_factors';

    IF v_relkind = 'r' THEN
        EXECUTE 'ALTER TABLE eq.road_weekly_factors RENAME TO road_weekly_factors_old';

        EXECUTE $SQL$
            CREATE TABLE eq.road_weekly_factors (
                country_code text NOT NULL,
                week_start date NOT NULL,
                road_row_id bigint NOT NULL,
                surface_group text NOT NULL,
                era5_tp_sum_weekly_mm double precision,
                era5_tp_1h_max_weekly_mm_per_h double precision,
                geometry geometry(Point, 4326),
                PRIMARY KEY (country_code, week_start, road_row_id)
            ) PARTITION BY LIST (country_code)
        $SQL$;

        EXECUTE 'CREATE TABLE IF NOT EXISTS eq.road_weekly_factors_default PARTITION OF eq.road_weekly_factors DEFAULT';
        EXECUTE 'INSERT INTO eq.road_weekly_factors SELECT * FROM eq.road_weekly_factors_old';
        EXECUTE 'DROP TABLE eq.road_weekly_factors_old';
    ELSIF v_relkind IS NULL THEN
        EXECUTE $SQL$
            CREATE TABLE eq.road_weekly_factors (
                country_code text NOT NULL,
                week_start date NOT NULL,
                road_row_id bigint NOT NULL,
                surface_group text NOT NULL,
                era5_tp_sum_weekly_mm double precision,
                era5_tp_1h_max_weekly_mm_per_h double precision,
                geometry geometry(Point, 4326),
                PRIMARY KEY (country_code, week_start, road_row_id)
            ) PARTITION BY LIST (country_code)
        $SQL$;
        EXECUTE 'CREATE TABLE IF NOT EXISTS eq.road_weekly_factors_default PARTITION OF eq.road_weekly_factors DEFAULT';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS road_weekly_factors_country_week_idx
    ON eq.road_weekly_factors (country_code, week_start);
CREATE INDEX IF NOT EXISTS road_weekly_factors_country_week_surface_idx
    ON eq.road_weekly_factors (country_code, week_start, surface_group);
CREATE INDEX IF NOT EXISTS road_weekly_factors_geometry_gist
    ON eq.road_weekly_factors USING GIST (geometry);

DO $$
DECLARE
    v_relkind char;
BEGIN
    SELECT c.relkind
      INTO v_relkind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'eq' AND c.relname = 'boxplot_stats_weekly';

    IF v_relkind = 'r' THEN
        EXECUTE 'ALTER TABLE eq.boxplot_stats_weekly RENAME TO boxplot_stats_weekly_old';

        EXECUTE $SQL$
            CREATE TABLE eq.boxplot_stats_weekly (
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
            ) PARTITION BY LIST (country_code)
        $SQL$;

        EXECUTE 'CREATE TABLE IF NOT EXISTS eq.boxplot_stats_weekly_default PARTITION OF eq.boxplot_stats_weekly DEFAULT';
        EXECUTE 'INSERT INTO eq.boxplot_stats_weekly SELECT * FROM eq.boxplot_stats_weekly_old';
        EXECUTE 'DROP TABLE eq.boxplot_stats_weekly_old';
    ELSIF v_relkind IS NULL THEN
        EXECUTE $SQL$
            CREATE TABLE eq.boxplot_stats_weekly (
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
            ) PARTITION BY LIST (country_code)
        $SQL$;
        EXECUTE 'CREATE TABLE IF NOT EXISTS eq.boxplot_stats_weekly_default PARTITION OF eq.boxplot_stats_weekly DEFAULT';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS boxplot_stats_weekly_country_week_idx
    ON eq.boxplot_stats_weekly (country_code, week_start);
