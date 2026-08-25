-- The test suite drops and recreates every table on each run. Pointing it at a
-- separate database keeps `pytest` from destroying whatever you seeded into the
-- development database for manual poking.
CREATE DATABASE miniledger_test OWNER miniledger;
