-- Required psql vars:
--   :country_code  e.g. AGO
--   :cell_deg      e.g. 0.1

WITH road_base AS (
    SELECT
        country_code,
        road_row_id,
        ST_X(ST_SnapToGrid(geometry, :'cell_deg'::double precision, :'cell_deg'::double precision)) AS cell_lon,
        ST_Y(ST_SnapToGrid(geometry, :'cell_deg'::double precision, :'cell_deg'::double precision)) AS cell_lat
    FROM eq.road_weekly_factors
    WHERE country_code = :'country_code'
    GROUP BY country_code, road_row_id, geometry
)
INSERT INTO eq.road_cell_map (
    country_code,
    road_row_id,
    cell_id,
    cell_lon,
    cell_lat
)
SELECT
    country_code,
    road_row_id,
    format('%s:%s', round(cell_lon::numeric, 6), round(cell_lat::numeric, 6)) AS cell_id,
    cell_lon,
    cell_lat
FROM road_base
ON CONFLICT (country_code, road_row_id)
DO UPDATE SET
    cell_id = EXCLUDED.cell_id,
    cell_lon = EXCLUDED.cell_lon,
    cell_lat = EXCLUDED.cell_lat;
