-- Feedback is given once per case (migration 019): matching_feedback_give refuses a second rating
-- instead of replacing the first, and an admin can delete a feedback row (test feedback, say).
-- Replaces matching_feedback_give from 017. No literal question marks or percent signs in this file.

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
    IF kind = 'rating' AND EXISTS (SELECT 1 FROM matching_feedback f WHERE f.owner = who AND f.run_id = which_run
                                   AND f.case_index = idx AND f.kind = 'rating') THEN
        RAISE EXCEPTION 'You already gave feedback on this case.';
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

-- Delete one feedback row (admin only). Returns how many rows were deleted (0 or 1).
CREATE OR REPLACE FUNCTION public.matching_feedback_delete(access_key text, feedback text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
    deleted integer;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    IF NOT (caller->>'admin')::boolean THEN
        RAISE EXCEPTION 'Only an admin link can delete feedback.';
    END IF;
    DELETE FROM matching_feedback f WHERE f.feedback_id = feedback;
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END
$$;

REVOKE ALL ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feedback_delete(text, text) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feedback_delete(text, text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_feedback_give(text, text, integer, text, text, text, boolean, text) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_feedback_delete(text, text) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
