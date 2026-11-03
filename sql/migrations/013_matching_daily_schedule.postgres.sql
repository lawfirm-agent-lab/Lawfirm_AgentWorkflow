-- Start the daily check on time. GitHub's own schedule can run hours late (the first scheduled
-- run, due 11:30 UTC on 2026-11-03, ran at 18:36), so the database starts it instead: pg_cron runs
-- matching_start_daily() every day at 11:30 UTC, which asks GitHub to run daily-matching.yml
-- through pg_net with the token in Vault, as matching_dispatch does for uploads (009). GitHub's
-- schedule stays as a later backup that only checks CVs not checked that day.
--
-- Everything here is guarded: without pg_cron (a plain PostgreSQL in tests) or if Supabase refuses
-- the extension, the migration still applies and only the backup schedule runs.
-- No literal question marks or percent signs in this file.

CREATE OR REPLACE FUNCTION public.matching_start_daily() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    token text;
BEGIN
    SELECT decrypted_secret INTO token FROM vault.decrypted_secrets WHERE name = 'matching_github_token' LIMIT 1;
    IF token IS NULL THEN
        RAISE WARNING 'matching_start_daily: the GitHub token is not set';
        RETURN NULL;
    END IF;
    RETURN net.http_post(
        url := 'https://api.github.com/repos/ctrterry/Lawfirm_AgentWorkflow/actions/workflows/daily-matching.yml/dispatches',
        body := jsonb_build_object('ref', 'main', 'inputs', jsonb_build_object('days', '2')),
        headers := jsonb_build_object('Authorization', 'Bearer ' || token, 'Accept', 'application/vnd.github+json',
                                      'Content-Type', 'application/json', 'User-Agent', 'expert-match',
                                      'X-GitHub-Api-Version', '2022-11-28'));
END
$$;

REVOKE ALL ON FUNCTION public.matching_start_daily() FROM PUBLIC;

DO $grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.matching_start_daily() FROM anon, authenticated;
    END IF;
END
$grants$;

DO $cron$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        CREATE EXTENSION IF NOT EXISTS pg_cron;
        -- Same name again replaces the job, so re-running this is harmless.
        PERFORM cron.schedule('matching-daily-check', '30 11 * * *', 'SELECT public.matching_start_daily()');
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING USING MESSAGE = 'matching daily schedule not set up: ' || SQLERRM;
END
$cron$;
