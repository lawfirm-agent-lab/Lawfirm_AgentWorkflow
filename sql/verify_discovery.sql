-- Read-only cloud acceptance checks. No API requests, writes or case text output.
WITH checks AS (
 SELECT '01_records' AS check_name, count(*)::text AS result FROM public.discovery_records
 UNION ALL SELECT '02_events', count(*)::text FROM public.discovery_events
 UNION ALL SELECT '03_duplicate_record_keys', count(*)::text FROM
   (SELECT stream,record_id FROM public.discovery_records GROUP BY stream,record_id HAVING count(*)>1) d
 UNION ALL SELECT '04_duplicate_event_versions', count(*)::text FROM
   (SELECT stream,record_id,payload_json FROM public.discovery_events GROUP BY stream,record_id,payload_json HAVING count(*)>1) d
 UNION ALL SELECT '05_events_without_record', count(*)::text FROM public.discovery_events e
   WHERE NOT EXISTS (SELECT 1 FROM public.discovery_records r WHERE r.stream=e.stream AND r.record_id=e.record_id)
 UNION ALL SELECT '06_runs_saved', count(*)::text FROM public.discovery_runs
   WHERE run_id IN ('1edfa12b13c84a6a87bd780e2fd0afb5','a41edf9056cd4524a33f4cc69435cd24','edb9eb229b6a4955975225c55cf85e7c') AND finished_at IS NOT NULL
 UNION ALL SELECT '07_checkpoint_' || stream,
   'revision=' || revision::text || '; start=' || start_at || '; pending_page=' || (next_url IS NOT NULL)::text
   FROM public.discovery_state
)
SELECT * FROM checks ORDER BY check_name;
