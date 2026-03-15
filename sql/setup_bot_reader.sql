-- AMC Bot Reader: Read-Only PostgreSQL Role with RLS
--
-- For EXISTING databases, run this manually:
--   machinectl shell amc-backend
--   sudo -u amc psql amc < /path/to/setup_bot_reader.sql
--
-- For NEW databases, this is handled automatically by the NixOS
-- initialScript in flake.nix (ensureUsers + initialScript).
--
-- The password should be set separately:
--   ALTER USER amc_bot_reader WITH PASSWORD '<your-secret>';

-- 1. Grant read-only access to all existing and future tables
GRANT USAGE ON SCHEMA public TO amc_bot_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO amc_bot_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO amc_bot_reader;

-- 2. Enable Row-Level Security on sensitive finance tables
ALTER TABLE amc_finance_account ENABLE ROW LEVEL SECURITY;
ALTER TABLE amc_finance_ledgerentry ENABLE ROW LEVEL SECURITY;
ALTER TABLE amc_finance_journalentry ENABLE ROW LEVEL SECURITY;

-- 3. RLS policies: deny amc_bot_reader from all finance table rows (idempotent)
DROP POLICY IF EXISTS bot_deny_account ON amc_finance_account;
DROP POLICY IF EXISTS bot_deny_ledger ON amc_finance_ledgerentry;
DROP POLICY IF EXISTS bot_deny_journal ON amc_finance_journalentry;

CREATE POLICY bot_deny_account ON amc_finance_account
    FOR SELECT TO amc_bot_reader USING (false);

CREATE POLICY bot_deny_ledger ON amc_finance_ledgerentry
    FOR SELECT TO amc_bot_reader USING (false);

CREATE POLICY bot_deny_journal ON amc_finance_journalentry
    FOR SELECT TO amc_bot_reader USING (false);

-- 4. Ensure the main application user bypasses RLS
ALTER USER amc BYPASSRLS;

-- Verification (run manually):
-- SET ROLE amc_bot_reader;
-- SELECT * FROM amc_finance_account LIMIT 1;        -- Should return 0 rows
-- SELECT * FROM amc_finance_ledgerentry LIMIT 1;     -- Should return 0 rows
-- SELECT COUNT(*) FROM amc_player;                   -- Should return count
-- RESET ROLE;
