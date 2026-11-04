-- Logs for the admin Logs page and `python -m src.matching.cloud log`: the daily schedule
-- (pg_cron, migration 013), the GitHub dispatches it and uploads made (pg_net; Supabase keeps
-- these responses for about 6 hours), every matching run with its timings, usage and error,
-- upload requests, the docket pipeline's runs, and the saved CVs. Admin links only.
-- No literal question marks or percent signs in this file.

-- Everything since `days` days ago (1 to 60). Not callable from the website: matching_log_overview
-- checks the key first. The cron and net tables are read only when those extensions exist.
CREATE OR REPLACE FUNCTION public.matching_log_data(days integer DEFAULT 7) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    since timestamptz := now() - make_interval(days => least(greatest(coalesce(days, 7), 1), 60));
    since_text text := to_char(since AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS');
    schedule jsonb := '[]'::jsonb;
    dispatches jsonb := '[]'::jsonb;
BEGIN
    IF to_regclass('cron.job_run_details') IS NOT NULL THEN
        EXECUTE $q$SELECT coalesce(jsonb_agg(jsonb_build_object(
                       'at', d.start_time, 'end', d.end_time, 'status', d.status,
                       'message', left(d.return_message, 300)) ORDER BY d.start_time DESC), '[]'::jsonb)
                   FROM cron.job_run_details d JOIN cron.job j ON j.jobid = d.jobid
                   WHERE j.jobname = 'matching-daily-check' AND d.start_time >= $1$q$
            INTO schedule USING since;
    END IF;
    IF to_regclass('net._http_response') IS NOT NULL THEN
        EXECUTE $q$SELECT coalesce(jsonb_agg(jsonb_build_object(
                       'at', r.created, 'status', r.status_code, 'timed_out', r.timed_out,
                       'error', left(r.error_msg, 300)) ORDER BY r.created DESC), '[]'::jsonb)
                   FROM net._http_response r WHERE r.created >= $1$q$
            INTO dispatches USING since;
    END IF;
    RETURN jsonb_build_object(
        'since', since_text, 'now', matching_utc_now(), 'schedule', schedule, 'dispatches', dispatches,
        'runs', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'run_id', r.run_id, 'cv', r.cv_name, 'owner', r.owner, 'state', r.state,
                       'started', r.started_at, 'finished', r.finished_at,
                       'daily', coalesce((s.s->>'daily')::boolean, false), 'days', s.s->'days',
                       'model', s.s->>'model', 'prompt_version', s.s->>'prompt_version',
                       'error', s.s->>'error', 'stats', s.s->'stats', 'usage', s.s->'usage',
                       'actions_url', s.s->>'actions_url',
                       'steps', jsonb_array_length(coalesce(s.s->'steps', '[]'::jsonb)),
                       'candidates', jsonb_array_length(coalesce(e.e->'discovery'->'candidates', '[]'::jsonb)),
                       'cases', jsonb_array_length(coalesce(e.e->'results', '[]'::jsonb)),
                       'tiers', (SELECT jsonb_object_agg(t.tier, t.n) FROM (
                                     SELECT x->'match'->>'tier' AS tier, count(*) AS n
                                     FROM jsonb_array_elements(coalesce(e.e->'results', '[]'::jsonb)) AS xx(x)
                                     GROUP BY 1) t))
                   ORDER BY r.started_at DESC)
            FROM matching_runs r
            CROSS JOIN LATERAL (SELECT r.status_json::jsonb AS s) s
            CROSS JOIN LATERAL (SELECT r.result_json::jsonb->0 AS e) e
            WHERE r.started_at >= since_text), '[]'::jsonb),
        'requests', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'request_id', q.request_id, 'cv', q.cv_name, 'owner', q.owner, 'state', q.state,
                       'created', q.created_at, 'updated', q.updated_at, 'progress', q.progress,
                       'error', q.error, 'run_id', q.run_id) ORDER BY q.created_at DESC)
            FROM matching_requests q WHERE q.created_at >= since_text), '[]'::jsonb),
        'pipeline', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'run_id', p.run_id, 'started', p.started_at, 'finished', p.finished_at, 'status', p.status,
                       'report', p.report_json::jsonb) ORDER BY p.started_at DESC)
            FROM pipeline_runs p WHERE p.started_at >= since_text), '[]'::jsonb),
        'cvs', coalesce((
            SELECT jsonb_agg(jsonb_build_object('name', c.name, 'owner', c.owner, 'active', c.active = 1,
                                                'saved_at', c.saved_at) ORDER BY c.name)
            FROM matching_cvs c), '[]'::jsonb));
END
$$;

CREATE OR REPLACE FUNCTION public.matching_log_overview(access_key text, days integer DEFAULT 7) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    IF NOT (caller->>'admin')::boolean THEN
        RAISE EXCEPTION 'Only an admin link can see the logs.';
    END IF;
    RETURN matching_log_data(days);
END
$$;

-- One run in full: its status (every step with its time, the error and traceback) and its trace
-- (every model call's input and output, searches, the search agent's tries). Admin only: the
-- trace holds the CV's text.
CREATE OR REPLACE FUNCTION public.matching_log_run(access_key text, which_run text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    IF NOT (caller->>'admin')::boolean THEN
        RAISE EXCEPTION 'Only an admin link can see the logs.';
    END IF;
    RETURN (SELECT jsonb_build_object('run_id', r.run_id, 'cv', r.cv_name, 'owner', r.owner, 'state', r.state,
                                      'started', r.started_at, 'finished', r.finished_at,
                                      'status', r.status_json::jsonb, 'trace', coalesce(r.trace_json::jsonb, '[]'::jsonb))
            FROM matching_runs r WHERE r.run_id = which_run);
END
$$;

REVOKE ALL ON FUNCTION public.matching_log_data(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_log_overview(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_log_run(text, text) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_log_data(integer) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_log_overview(text, integer) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_log_run(text, text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_log_overview(text, integer) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_log_run(text, text) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
