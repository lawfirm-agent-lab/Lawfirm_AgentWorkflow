-- Saved CVs on the results website: list them, stop or resume their daily checks, or delete a CV
-- with everything made from it. Same rules as 007 and 009: each function checks the access key,
-- the tables stay closed. No literal question marks or percent signs in this file.

CREATE OR REPLACE FUNCTION public.matching_cvs(access_key text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'name', c.name, 'active', c.active = 1, 'saved_at', c.saved_at,
                   'runs', (SELECT count(*) FROM matching_runs r WHERE r.cv_name = c.name),
                   'last_run', (SELECT max(r.started_at) FROM matching_runs r WHERE r.cv_name = c.name))
               ORDER BY c.name)
        FROM matching_cvs c
    ), '[]'::jsonb);
END
$$;

-- Stop (active false) or resume (true) the daily checks of a saved CV. Past results stay.
CREATE OR REPLACE FUNCTION public.matching_cv_active(access_key text, cv text, active boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    UPDATE matching_cvs SET active = CASE WHEN matching_cv_active.active THEN 1 ELSE 0 END WHERE name = cv;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No saved CV with that name.';
    END IF;
    RETURN jsonb_build_object('name', cv, 'active', matching_cv_active.active);
END
$$;

-- Delete a CV for good: the saved PDF, every daily check and upload run made from it, and its
-- upload requests. Copies already on someone's computer (data/matching/runs) are not touched.
CREATE OR REPLACE FUNCTION public.matching_cv_delete(access_key text, cv text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    cvs integer;
    runs integer;
    requests integer;
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    DELETE FROM matching_cvs WHERE name = cv;
    GET DIAGNOSTICS cvs = ROW_COUNT;
    DELETE FROM matching_runs WHERE cv_name = cv;
    GET DIAGNOSTICS runs = ROW_COUNT;
    DELETE FROM matching_requests WHERE cv_name = cv AND state IN ('done', 'failed');
    GET DIAGNOSTICS requests = ROW_COUNT;
    IF cvs + runs + requests = 0 THEN
        RAISE EXCEPTION 'No saved CV or results with that name.';
    END IF;
    RETURN jsonb_build_object('name', cv, 'cvs', cvs, 'runs', runs, 'requests', requests);
END
$$;

REVOKE ALL ON FUNCTION public.matching_cvs(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_cv_active(text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_cv_delete(text, text) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_cvs(text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_cv_active(text, text, boolean) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_cv_delete(text, text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_cvs(text) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_cv_active(text, text, boolean) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_cv_delete(text, text) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
