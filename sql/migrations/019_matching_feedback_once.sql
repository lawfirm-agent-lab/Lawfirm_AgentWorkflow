-- One feedback per person and case, saved only when the person submits it (site/app.js). Until now
-- every tap was saved and a newer one replaced the older (current 0); those replaced taps are
-- not feedback anyone confirmed, so they go. The index makes a second rating impossible.
DELETE FROM matching_feedback WHERE kind = 'rating' AND current = 0;

CREATE UNIQUE INDEX IF NOT EXISTS matching_feedback_one_rating ON matching_feedback (owner, run_id, case_index)
WHERE kind = 'rating';
