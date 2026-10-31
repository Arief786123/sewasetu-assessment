-- Sewa Setu Old Age Pension Portal - schema
-- NOTE: seed.sql (shipped separately) loads after this and contains the
-- production data. Do not re-run against a live database.

CREATE TABLE IF NOT EXISTS applications (
    id              SERIAL PRIMARY KEY,
    application_no  VARCHAR(20),
    applicant_name  VARCHAR(100),
    mobile          VARCHAR(15),
    dob             DATE,
    gender          VARCHAR(10),
    marital_status  VARCHAR(20),
    husband_name    VARCHAR(100),
    husband_employer VARCHAR(100),
    village         VARCHAR(100),
    block           VARCHAR(50),
    bank_account    VARCHAR(30),
    ifsc            VARCHAR(15),
    doc_path        VARCHAR(200),
    status          VARCHAR(20) DEFAULT 'PENDING',
    submitted_at    TIMESTAMP,
    decided_at      TIMESTAMP,
    decided_by      VARCHAR(50)
);

-- Status portal accounts (created at submission time)
CREATE TABLE IF NOT EXISTS portal_users (
    mobile        VARCHAR(15) PRIMARY KEY,
    password_hash VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS otps (
    id         SERIAL PRIMARY KEY,
    mobile     VARCHAR(15),
    code       VARCHAR(64),
    created_at TIMESTAMP
);

-- Query-path indexes for the citizen portal, staff queue, and OTP lookup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_application_no_unique
    ON applications (application_no) WHERE application_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_applications_mobile_submitted
    ON applications (mobile, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_applications_status_submitted
    ON applications (status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_otps_mobile_id
    ON otps (mobile, id DESC);

ALTER TABLE otps ALTER COLUMN code TYPE VARCHAR(64);

ALTER TABLE portal_users ALTER COLUMN password_hash TYPE VARCHAR(255);
