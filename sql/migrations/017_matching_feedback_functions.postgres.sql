-- Feedback from the results website (table in 016). Same rules as 012: each function checks the
-- access key; a person rates only cases from runs they can see; only an admin reads everyone's
-- feedback. The snapshot is built here from the stored run, not sent by the page, so it cannot be
-- made up. No literal question marks or percent signs in this file.

-- What the system decided for one case of a run: level, score, evidence, source, versions.
CREATE OR REPLACE FUNCTION public.matching_feedback_snapshot(which_run text, idx integer) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object(
        'tier', x->'match'->>'tier', 'model_tier', x->'match'->>'model_tier',
        'score', x->'score'->'total', 'direct', x->'score'->'direct', 'related', x->'score'->'related',
        'reasons', jsonb_array_length(coalesce(x->'match'->'matches', '[]'::jsonb)),
        'match_summary', x->'match'->>'summary',
        'case_type', x->'profile'->>'case_type', 'case_summary', x->'profile'->>'summary',
        'court', x->'case'->>'court', 'filed', x->'case'->>'filed', 'docket_number', x->'case'->>'docket_number',
        'source', CASE WHEN x->'case'->>'from_web' IS NULL THEN 'courtlistener'
                       ELSE 'web ' || (x->'case'->>'from_web') END,
        'web_source', x->'case'->>'web_source',
        'pages', x->'profile'->'pages', 'pages_read', x->'profile'->'pages_read',
        'found_by', x->'case'->>'found_by',
        'screen', (SELECT c->'screen' FROM jsonb_array_elements(e->'discovery'->'candidates') AS cc(c)
                   WHERE c->>'url' = x->'case'->>'url' LIMIT 1),
        'expert', e->'profile'->>'name',
        'daily', coalesce((r.status_json::jsonb->>'daily')::boolean, false),
        'run_started', r.started_at,
        'model', r.status_json::jsonb->>'model',
        'prompt_version', r.status_json::jsonb->>'prompt_version')
    FROM matching_runs r,
         LATERAL (SELECT r.result_json::jsonb->0 AS e) ee,
         LATERAL (SELECT ee.e->'results'->idx AS x) xx
    WHERE r.run_id = which_run AND xx.x IS NOT NULL
$$;

-- Save a rating (verdict up or down) or an email action (gmail, mail, copy) on case `idx` of a run.
-- Returns the saved rating, as matching_feedback_case does.
CREATE OR REPLACE FUNCTION public.matching_feedback_give(access_key text, which_run text, idx integer, verdict text,
                                                         reason text DEFAULT NULL, should_be text DEFAULT NULL,
                                                         would_contact boolean DEFAULT false, note text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
    who text := caller->>'who';
    run_cv text;
    result jsonb;
    snap jsonb;
    kind text := CASE WHEN verdict IN ('up', 'down') THEN 'rating' ELSE 'email' END;
    -- Microseconds, so feedback given within one second keeps its order.
    stamp text := to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US');
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    SELECT r.cv_name, r.result_json::jsonb INTO run_cv, result FROM matching_runs r
    WHERE r.run_id = which_run AND r.state = 'done' AND ((caller->>'admin')::boolean OR r.owner = who);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No result of yours with that id.';
    END IF;
    snap := CASE WHEN idx >= 0 THEN matching_feedback_snapshot(which_run, idx) END;
    IF snap IS NULL THEN
        RAISE EXCEPTION 'No such case in that result.';
    END IF;
    IF verdict IS NULL OR verdict NOT IN ('up', 'down', 'gmail', 'mail', 'copy') THEN
        RAISE EXCEPTION 'Unknown feedback.';
    END IF;
    IF reason IS NOT NULL AND reason NOT IN ('not_relevant', 'wrong_level', 'misread', 'contact_info', 'already_known', 'other') THEN
        RAISE EXCEPTION 'Unknown reason.';
    END IF;
    IF should_be IS NOT NULL AND should_be NOT IN ('high', 'medium', 'low', 'none') THEN
        RAISE EXCEPTION 'Unknown level.';
    END IF;
    IF length(coalesce(note, '')) > 1000 THEN
        RAISE EXCEPTION 'The note is longer than 1000 characters.';
    END IF;
    IF (SELECT count(*) FROM matching_feedback f WHERE f.owner = who
        AND f.created_at > to_char((now() - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')) >= 300 THEN
        RAISE EXCEPTION 'The limit of 300 feedback a day is reached.';
    END IF;
    IF kind = 'rating' THEN
        UPDATE matching_feedback f SET current = 0
        WHERE f.owner = who AND f.run_id = which_run AND f.case_index = idx AND f.kind = 'rating' AND f.current = 1;
    END IF;
    INSERT INTO matching_feedback (feedback_id, owner, run_id, case_index, case_url, case_name, cv_name, kind, verdict,
                                   reason, should_be, would_contact, note, snapshot_json, current, created_at)
    VALUES (gen_random_uuid()::text, who, which_run, idx,
            result->0->'results'->idx->'case'->>'url', result->0->'results'->idx->'case'->>'name', run_cv, kind, verdict,
            CASE WHEN kind = 'rating' THEN reason END, CASE WHEN kind = 'rating' THEN should_be END,
            CASE WHEN kind = 'rating' AND would_contact THEN 1 ELSE 0 END,
            CASE WHEN kind = 'rating' THEN nullif(btrim(note), '') END, snap::text, 1, stamp);
    RETURN matching_feedback_case(access_key, which_run, idx);
END
$$;

-- The caller's current rating of one case, or NULL.
CREATE OR REPLACE FUNCTION public.matching_feedback_case(access_key text, which_run text, idx integer) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN (SELECT jsonb_build_object('verdict', f.verdict, 'reason', f.reason, 'should_be', f.should_be,
                                      'would_contact', f.would_contact = 1, 'note', f.note, 'at', f.created_at)
            FROM matching_feedback f
            WHERE f.owner = caller->>'who' AND f.run_id = which_run AND f.case_index = idx
              AND f.kind = 'rating' AND f.current = 1
            ORDER BY f.created_at DESC LIMIT 1);
END
$$;

-- Every feedback row, history included, newest first (admin only), for the Feedback page.
CREATE OR REPLACE FUNCTION public.matching_feedback_all(access_key text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    IF NOT (caller->>'admin')::boolean THEN
        RAISE EXCEPTION 'Only an admin link can see all feedback.';
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'id', f.feedback_id, 'who', f.owner, 'run_id', f.run_id, 'case_index', f.case_index,
                   'case_url', f.case_url, 'case_name', f.case_name, 'cv', f.cv_name, 'kind', f.kind,
                   'verdict', f.verdict, 'reason', f.reason, 'should_be', f.should_be,
                   'would_contact', f.would_contact = 1, 'note', f.note, 'current', f.current = 1,
                   'at', f.created_at, 'snapshot', f.snapshot_json::jsonb)
               ORDER BY f.created_at DESC, f.feedback_id)
        FROM matching_feedback f
    ), '[]'::jsonb);
END
$$;

REVOKE ALL ON FUNCTION public.matching_feedback_snapshot(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feedback_case(text, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feedback_all(text) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_feedback_snapshot(text, integer) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feedback_case(text, text, integer) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feedback_all(text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_feedback_case(text, text, integer) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_feedback_all(text) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
