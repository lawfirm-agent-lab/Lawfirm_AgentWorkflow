-- Read-only functions for the results website. The site calls them through Supabase's Data API
-- with the publishable key; the tables stay closed (RLS without policies), so these functions are
-- the only way in, and each one checks the access key first. SECURITY DEFINER: they run as the
-- owner, who can read the tables. No literal question marks or percent signs in this file:
-- statements go through the driver's placeholder handling.

CREATE OR REPLACE FUNCTION public.matching_key_ok(access_key text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM matching_access_keys
                   WHERE active = 1
                     AND key_hash = encode(sha256(convert_to(coalesce(access_key, ''), 'UTF8')), 'hex'))
$$;

-- Daily checks of the last `days` days (1 to 60), newest first, each reduced to what the list page
-- shows: per expert the complaints found (with screening) and the cases read, in ranked order.
CREATE OR REPLACE FUNCTION public.matching_feed(access_key text, days integer DEFAULT 14) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(run ORDER BY started_at DESC) FROM (
            SELECT r.started_at, jsonb_build_object(
                'run_id', r.run_id, 'cv', r.cv_name, 'started', r.started_at,
                'experts', coalesce((
                    SELECT jsonb_agg(jsonb_build_object(
                        'name', e->'profile'->>'name',
                        'headline', e->'profile'->>'headline',
                        'since', e->'discovery'->>'since',
                        'skipped', e->'discovery'->'skipped',
                        'candidates', coalesce((
                            SELECT jsonb_agg(jsonb_build_object(
                                'name', c->>'name', 'url', c->>'url', 'court', c->>'court',
                                'filed', c->>'filed', 'has_pdf', c->'has_pdf', 'cost', c->'cost',
                                'screen', c->'screen') ORDER BY ci)
                            FROM jsonb_array_elements(e->'discovery'->'candidates') WITH ORDINALITY AS cc(c, ci)
                        ), '[]'::jsonb),
                        'cases', coalesce((
                            SELECT jsonb_agg(jsonb_build_object(
                                'name', x->'case'->>'name', 'url', x->'case'->>'url',
                                'court', x->'case'->>'court', 'filed', x->'case'->>'filed',
                                'case_type', x->'profile'->>'case_type', 'tier', x->'match'->>'tier',
                                'score', x->'score'->'total', 'summary', x->'match'->>'summary') ORDER BY xi)
                            FROM jsonb_array_elements(e->'results') WITH ORDINALITY AS xx(x, xi)
                        ), '[]'::jsonb)
                    ) ORDER BY ei)
                    FROM jsonb_array_elements(r.result_json::jsonb) WITH ORDINALITY AS ee(e, ei)
                ), '[]'::jsonb)
            ) AS run
            FROM matching_runs r
            WHERE r.state = 'done' AND r.result_json IS NOT NULL
              AND r.started_at >= to_char(
                  (now() - make_interval(days => least(greatest(coalesce(days, 14), 1), 60))) AT TIME ZONE 'UTC',
                  'YYYY-MM-DD"T"HH24:MI:SS')
        ) runs
    ), '[]'::jsonb);
END
$$;

-- One daily check in full (the run's result.json), for the case pages. Not its trace.
CREATE OR REPLACE FUNCTION public.matching_run(access_key text, which_run text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN (SELECT jsonb_build_object('run_id', r.run_id, 'cv', r.cv_name, 'started', r.started_at,
                                      'result', r.result_json::jsonb)
            FROM matching_runs r WHERE r.run_id = which_run AND r.state = 'done');
END
$$;

REVOKE ALL ON FUNCTION public.matching_key_ok(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feed(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_run(text, text) FROM PUBLIC;

-- Supabase's API roles; absent on a plain PostgreSQL (tests).
DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_key_ok(text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feed(text, integer) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_run(text, text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_feed(text, integer) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_run(text, text) TO anon;
    END IF;
END
$grants$;

-- Let the Data API see the new functions now rather than at its next schema reload.
NOTIFY pgrst, 'reload schema';
