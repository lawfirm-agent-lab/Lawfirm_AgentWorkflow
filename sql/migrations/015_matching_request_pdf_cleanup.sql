-- Uploads that ended before this fix kept their CV copy when the CV was also saved or the run
-- failed. The copy is only needed while the run is queued or running, so drop the old ones.
UPDATE matching_requests SET pdf_base64 = NULL WHERE state IN ('done', 'failed') AND pdf_base64 IS NOT NULL;
