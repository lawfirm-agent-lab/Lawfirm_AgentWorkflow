-- Upload a CV from the results website and start a cloud run for it, then follow the run.
-- Same rules as 007: the site's publishable key may only execute the key-checking functions below;
-- tables stay closed. No literal question marks or percent signs in this file.
--
-- Starting the run: matching_dispatch asks GitHub to run .github/workflows/matching-request.yml
-- through pg_net (an HTTP call made by the database after the transaction commits), with a GitHub
-- token kept in Supabase Vault under the name matching_github_token (set it with
-- python -m src.matching.cloud set-dispatch-token). Without pg_net or the token the request stays
-- queued with an error saying why.

DO $ext$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_net') THEN
        CREATE EXTENSION IF NOT EXISTS pg_net;
    END IF;
END
$ext$;

CREATE OR REPLACE FUNCTION public.matching_utc_now() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00'
$$;

CREATE OR REPLACE FUNCTION public.matching_dispatch(request text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    token text;
BEGIN
    SELECT decrypted_secret INTO token FROM vault.decrypted_secrets WHERE name = 'matching_github_token' LIMIT 1;
    IF token IS NULL THEN
        UPDATE matching_requests SET error = 'The cloud run cannot start: the GitHub token is not set.',
               updated_at = matching_utc_now() WHERE request_id = request;
        RETURN;
    END IF;
    PERFORM net.http_post(
        url := 'https://api.github.com/repos/ctrterry/Lawfirm_AgentWorkflow/actions/workflows/matching-request.yml/dispatches',
        body := jsonb_build_object('ref', 'main', 'inputs', jsonb_build_object('request_id', request)),
        headers := jsonb_build_object('Authorization', 'Bearer ' || token, 'Accept', 'application/vnd.github+json',
                                      'Content-Type', 'application/json', 'User-Agent', 'expert-match',
                                      'X-GitHub-Api-Version', '2022-11-28'));
EXCEPTION WHEN OTHERS THEN
    UPDATE matching_requests SET error = 'The cloud run cannot start: ' || SQLERRM,
           updated_at = matching_utc_now() WHERE request_id = request;
END
$$;

-- Save the CV (for daily checks too, when save is true), queue a run and start it.
-- At most 10 uploads a day across all links, to cap model and CourtListener use.
CREATE OR REPLACE FUNCTION public.matching_submit(access_key text, cv_name text, pdf_base64 text, save boolean DEFAULT true)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    saved_name text;
    head bytea;
    request text := gen_random_uuid()::text;
    stamp text := matching_utc_now();
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    IF length(coalesce(pdf_base64, '')) < 100 OR length(pdf_base64) > 14000000 THEN
        RAISE EXCEPTION 'The CV must be a PDF of at most 10 MB.';
    END IF;
    BEGIN
        head := decode(left(pdf_base64, 1368), 'base64');
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'The file is not a PDF.';
    END;
    IF position('\x255044462d'::bytea in head) = 0 THEN
        RAISE EXCEPTION 'The file is not a PDF.';
    END IF;
    IF (SELECT count(*) FROM matching_requests
        WHERE created_at > to_char((now() - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')) >= 10 THEN
        RAISE EXCEPTION 'The limit of 10 uploads a day is reached; try again tomorrow.';
    END IF;
    saved_name := btrim(left(regexp_replace(coalesce(cv_name, ''), '[^A-Za-z0-9._-]+', '_', 'g'), 76), '._');
    saved_name := regexp_replace(saved_name, '[.]pdf$', '', 'i');
    saved_name := coalesce(nullif(saved_name, ''), 'cv') || '.pdf';
    IF save THEN
        INSERT INTO matching_cvs VALUES (saved_name, pdf_base64, encode(sha256(decode(pdf_base64, 'base64')), 'hex'), 1, stamp)
        ON CONFLICT (name) DO UPDATE SET pdf_base64 = excluded.pdf_base64, sha256 = excluded.sha256, active = 1,
                                         saved_at = excluded.saved_at;
    END IF;
    INSERT INTO matching_requests VALUES (request, saved_name, pdf_base64, CASE WHEN save THEN 1 ELSE 0 END, 'queued',
                                          NULL, NULL, NULL, stamp, stamp);
    PERFORM matching_dispatch(request);
    RETURN jsonb_build_object('request_id', request, 'cv_name', saved_name);
END
$$;

-- A request's state for the progress page.
CREATE OR REPLACE FUNCTION public.matching_request(access_key text, request text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT matching_key_ok(access_key) THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN (SELECT jsonb_build_object('request_id', q.request_id, 'cv_name', q.cv_name, 'state', q.state,
                                      'run_id', q.run_id, 'progress', q.progress, 'error', q.error,
                                      'created_at', q.created_at, 'updated_at', q.updated_at, 'now', matching_utc_now())
            FROM matching_requests q WHERE q.request_id = request);
END
$$;

-- The list now tells daily checks from runs started by an upload.
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
                'daily', coalesce((r.status_json::jsonb->>'daily')::boolean, false),
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
                                'screen', c->'screen', 'elsewhere', c->'elsewhere') ORDER BY ci)
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

REVOKE ALL ON FUNCTION public.matching_utc_now() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_dispatch(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_submit(text, text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_request(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_feed(text, integer) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_utc_now() FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_dispatch(text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_submit(text, text, text, boolean) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_request(text, text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_feed(text, integer) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_submit(text, text, text, boolean) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_request(text, text) TO anon;
        GRANT EXECUTE ON FUNCTION public.matching_feed(text, integer) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
