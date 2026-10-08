-- Runs once, on first container start, before the app ever connects.
-- Creating the extension from a migration instead would need superuser rights
-- the application role should not have.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram index for fuzzy title search
