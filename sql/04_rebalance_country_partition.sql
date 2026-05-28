-- Required psql vars:
--   :country_code   e.g. AGO

DROP TABLE IF EXISTS eq.road_weekly_factors_tmp_rebalance;
CREATE TABLE eq.road_weekly_factors_tmp_rebalance AS
SELECT *
FROM eq.road_weekly_factors_default
WHERE country_code = upper(:'country_code');

DELETE FROM eq.road_weekly_factors_default
WHERE country_code = upper(:'country_code');

SELECT eq.ensure_list_partition('eq', 'road_weekly_factors', upper(:'country_code'));

INSERT INTO eq.road_weekly_factors
SELECT *
FROM eq.road_weekly_factors_tmp_rebalance
ON CONFLICT (country_code, week_start, road_row_id) DO NOTHING;

DROP TABLE IF EXISTS eq.road_weekly_factors_tmp_rebalance;

DROP TABLE IF EXISTS eq.boxplot_stats_weekly_tmp_rebalance;
CREATE TABLE eq.boxplot_stats_weekly_tmp_rebalance AS
SELECT *
FROM eq.boxplot_stats_weekly_default
WHERE country_code = upper(:'country_code');

DELETE FROM eq.boxplot_stats_weekly_default
WHERE country_code = upper(:'country_code');

SELECT eq.ensure_list_partition('eq', 'boxplot_stats_weekly', upper(:'country_code'));

INSERT INTO eq.boxplot_stats_weekly
SELECT *
FROM eq.boxplot_stats_weekly_tmp_rebalance
ON CONFLICT (country_code, week_start, scenario, surface_scope, factor) DO NOTHING;

DROP TABLE IF EXISTS eq.boxplot_stats_weekly_tmp_rebalance;
