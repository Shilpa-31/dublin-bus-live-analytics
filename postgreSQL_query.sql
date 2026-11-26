SELECT * FROM public.dublin_bus_data
ORDER BY vehicle_id ASC, "timestamp" ASC LIMIT 100

-- How much data, from when to when, and how big
SELECT count(*) AS rows,
       count(DISTINCT timestamp) AS snapshots,
       min(timestamp) AS first, max(timestamp) AS latest,
       pg_size_pretty(pg_total_relation_size('dublin_bus_data')) AS size
FROM dublin_bus_data;

-- Rows per minute for the last 15 minutes (each should be about 500)
SELECT date_trunc('minute', timestamp) AS minute, count(*)
FROM dublin_bus_data
WHERE timestamp > now() - interval '15 minutes'
GROUP BY 1 ORDER BY 1 DESC;

-- Latest 20 rows
SELECT * FROM dublin_bus_data ORDER BY timestamp DESC LIMIT 20;