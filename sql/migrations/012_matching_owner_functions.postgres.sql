-- Each link is a person (migration 011): the functions below show and change only the caller's own
-- CVs, runs and uploads; an admin key sees and manages all of them. They replace the versions in
-- 007, 009 and 010. No literal question marks or percent signs in this file.

-- Who a key belongs to: {"who": label, "admin": true|false}, or NULL for an unknown or revoked key.
CREATE OR REPLACE FUNCTION public.matching_caller(access_key text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(*) = 0 THEN NULL
                ELSE jsonb_build_object('who', min(label), 'admin', bool_or(role = 'admin')) END
    FROM matching_access_keys
    WHERE active = 1 AND key_hash = encode(sha256(convert_to(coalesce(access_key, ''), 'UTF8')), 'hex')
$$;

-- For the page header ("You: advisor").
CREATE OR REPLACE FUNCTION public.matching_whoami(access_key text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN caller;
END
$$;

-- Daily checks and upload runs of the caller (all of them for an admin), newest first.
CREATE OR REPLACE FUNCTION public.matching_feed(access_key text, days integer DEFAULT 14) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(run ORDER BY started_at DESC) FROM (
            SELECT r.started_at, jsonb_build_object(
                'run_id', r.run_id, 'cv', r.cv_name, 'started', r.started_at, 'owner', r.owner,
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
              AND ((caller->>'admin')::boolean OR r.owner = caller->>'who')
              AND r.started_at >= to_char(
                  (now() - make_interval(days => least(greatest(coalesce(days, 14), 1), 60))) AT TIME ZONE 'UTC',
                  'YYYY-MM-DD"T"HH24:MI:SS')
        ) runs
    ), '[]'::jsonb);
END
$$;

CREATE OR REPLACE FUNCTION public.matching_run(access_key text, which_run text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN (SELECT jsonb_build_object('run_id', r.run_id, 'cv', r.cv_name, 'started', r.started_at, 'owner', r.owner,
                                      'result', r.result_json::jsonb)
            FROM matching_runs r
            WHERE r.run_id = which_run AND r.state = 'done'
              AND ((caller->>'admin')::boolean OR r.owner = caller->>'who'));
END
$$;

-- As in 009, plus: the upload belongs to the caller, at most 10 uploads a day per person, and a
-- file name another person already uses gets a number (jane.pdf -> jane-2.pdf), so nobody's CV
-- or results are ever replaced or mixed with someone else's.
CREATE OR REPLACE FUNCTION public.matching_submit(access_key text, cv_name text, pdf_base64 text, save boolean DEFAULT true)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
    who text := caller->>'who';
    head bytea;
    base text;
    saved_name text;
    n integer := 2;
    request text := gen_random_uuid()::text;
    stamp text := matching_utc_now();
BEGIN
    IF caller IS NULL THEN
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
    IF (SELECT count(*) FROM matching_requests q WHERE q.owner = who
        AND q.created_at > to_char((now() - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')) >= 10 THEN
        RAISE EXCEPTION 'The limit of 10 uploads a day is reached; try again tomorrow.';
    END IF;
    base := btrim(left(regexp_replace(coalesce(cv_name, ''), '[^A-Za-z0-9._-]+', '_', 'g'), 72), '._');
    base := coalesce(nullif(regexp_replace(base, '[.]pdf$', '', 'i'), ''), 'cv');
    saved_name := base || '.pdf';
    WHILE EXISTS (SELECT 1 FROM matching_cvs c WHERE c.name = saved_name AND c.owner IS DISTINCT FROM who)
          OR EXISTS (SELECT 1 FROM matching_runs r WHERE r.cv_name = saved_name AND r.owner IS DISTINCT FROM who) LOOP
        saved_name := base || '-' || n || '.pdf';
        n := n + 1;
    END LOOP;
    IF save THEN
        INSERT INTO matching_cvs (name, pdf_base64, sha256, active, saved_at, owner)
        VALUES (saved_name, pdf_base64, encode(sha256(decode(pdf_base64, 'base64')), 'hex'), 1, stamp, who)
        ON CONFLICT (name) DO UPDATE SET pdf_base64 = excluded.pdf_base64, sha256 = excluded.sha256, active = 1,
                                         saved_at = excluded.saved_at
        WHERE matching_cvs.owner IS NOT DISTINCT FROM excluded.owner;
    END IF;
    INSERT INTO matching_requests (request_id, cv_name, pdf_base64, save, state, created_at, updated_at, owner)
    VALUES (request, saved_name, pdf_base64, CASE WHEN save THEN 1 ELSE 0 END, 'queued', stamp, stamp, who);
    PERFORM matching_dispatch(request);
    RETURN jsonb_build_object('request_id', request, 'cv_name', saved_name);
END
$$;

CREATE OR REPLACE FUNCTION public.matching_request(access_key text, request text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN (SELECT jsonb_build_object('request_id', q.request_id, 'cv_name', q.cv_name, 'state', q.state,
                                      'run_id', q.run_id, 'progress', q.progress, 'error', q.error,
                                      'created_at', q.created_at, 'updated_at', q.updated_at, 'now', matching_utc_now())
            FROM matching_requests q
            WHERE q.request_id = request AND ((caller->>'admin')::boolean OR q.owner = caller->>'who'));
END
$$;

CREATE OR REPLACE FUNCTION public.matching_cvs(access_key text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(jsonb_build_object(
                   'name', c.name, 'owner', c.owner, 'active', c.active = 1, 'saved_at', c.saved_at,
                   'runs', (SELECT count(*) FROM matching_runs r WHERE r.cv_name = c.name),
                   'last_run', (SELECT max(r.started_at) FROM matching_runs r WHERE r.cv_name = c.name))
               ORDER BY c.name)
        FROM matching_cvs c
        WHERE (caller->>'admin')::boolean OR c.owner = caller->>'who'
    ), '[]'::jsonb);
END
$$;

CREATE OR REPLACE FUNCTION public.matching_cv_active(access_key text, cv text, active boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    UPDATE matching_cvs c SET active = CASE WHEN matching_cv_active.active THEN 1 ELSE 0 END
    WHERE c.name = cv AND ((caller->>'admin')::boolean OR c.owner = caller->>'who');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No saved CV of yours with that name.';
    END IF;
    RETURN jsonb_build_object('name', cv, 'active', matching_cv_active.active);
END
$$;

-- Deletes the CV, and the runs and finished uploads its owner made from it.
CREATE OR REPLACE FUNCTION public.matching_cv_delete(access_key text, cv text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    caller jsonb := matching_caller(access_key);
    target text;
    cvs integer;
    runs integer;
    requests integer;
BEGIN
    IF caller IS NULL THEN
        RAISE EXCEPTION 'invalid access key' USING ERRCODE = '28000';
    END IF;
    SELECT c.owner INTO target FROM matching_cvs c WHERE c.name = cv;
    IF NOT FOUND OR NOT ((caller->>'admin')::boolean OR target IS NOT DISTINCT FROM caller->>'who') THEN
        RAISE EXCEPTION 'No saved CV of yours with that name.';
    END IF;
    DELETE FROM matching_cvs c WHERE c.name = cv;
    GET DIAGNOSTICS cvs = ROW_COUNT;
    DELETE FROM matching_runs r WHERE r.cv_name = cv AND r.owner IS NOT DISTINCT FROM target;
    GET DIAGNOSTICS runs = ROW_COUNT;
    DELETE FROM matching_requests q
    WHERE q.cv_name = cv AND q.owner IS NOT DISTINCT FROM target AND q.state IN ('done', 'failed');
    GET DIAGNOSTICS requests = ROW_COUNT;
    RETURN jsonb_build_object('name', cv, 'cvs', cvs, 'runs', runs, 'requests', requests);
END
$$;

REVOKE ALL ON FUNCTION public.matching_caller(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.matching_whoami(text) FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_caller(text) FROM anon, authenticated;
        REVOKE ALL ON FUNCTION public.matching_whoami(text) FROM anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.matching_whoami(text) TO anon;
    END IF;
END
$grants$;

NOTIFY pgrst, 'reload schema';
