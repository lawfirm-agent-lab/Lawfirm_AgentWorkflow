-- One link per person: a key's label names its holder, and an admin key sees and manages every
-- CV. CVs, runs and upload requests record their owner (the uploader's label); NULL means made by
-- the admin (everything before this migration), which only admin keys can see.
ALTER TABLE matching_access_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'user';

ALTER TABLE matching_cvs ADD COLUMN owner TEXT;

ALTER TABLE matching_runs ADD COLUMN owner TEXT;

ALTER TABLE matching_requests ADD COLUMN owner TEXT;
