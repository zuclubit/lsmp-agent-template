-- =============================================================================
-- 01_legacy_schema.sql
-- Legacy Source Database Schema
-- Simulates Oracle Siebel 8.1 / SAP CRM 7.0 / PostgreSQL source data model
-- Used for local development and demo purposes
-- =============================================================================

-- Create legacy_db if it doesn't exist (run as superuser)
-- CREATE DATABASE legacy_db;

\c legacy_db

-- Drop tables in reverse FK order
DROP TABLE IF EXISTS siebel_opportunities CASCADE;
DROP TABLE IF EXISTS siebel_contacts CASCADE;
DROP TABLE IF EXISTS siebel_accounts CASCADE;
DROP TABLE IF EXISTS sap_industry_codes CASCADE;
DROP TABLE IF EXISTS migration_field_map CASCADE;

-- ---------------------------------------------------------------------------
-- Industry code lookup (SAP CRM reference data)
-- ---------------------------------------------------------------------------
CREATE TABLE sap_industry_codes (
    industry_code   VARCHAR(10) PRIMARY KEY,
    industry_name   VARCHAR(100) NOT NULL,
    sf_industry     VARCHAR(100),       -- Mapped Salesforce Industry picklist value
    active          BOOLEAN DEFAULT TRUE
);

INSERT INTO sap_industry_codes VALUES
    ('TECH',    'Technology',               'Technology',           TRUE),
    ('HLTH',    'Healthcare',               'Healthcare',           TRUE),
    ('FIN',     'Financial Services',       'Finance',              TRUE),
    ('MFG',     'Manufacturing',            'Manufacturing',        TRUE),
    ('RET',     'Retail',                   'Retail',               TRUE),
    ('EDU',     'Education',                'Education',            TRUE),
    ('GOV',     'Government',               'Government',           TRUE),
    ('ENRG',    'Energy & Utilities',       'Energy',               TRUE),
    ('TRNSP',   'Transportation',           'Transportation',       TRUE),
    ('MEDIA',   'Media & Entertainment',    'Media',                TRUE),
    ('CONS',    'Consulting',               'Consulting',           TRUE),
    ('HOSP',    'Hospitality',              'Hospitality',          TRUE);

-- ---------------------------------------------------------------------------
-- Accounts (Siebel S_ORG_EXT equivalent)
-- ---------------------------------------------------------------------------
CREATE TABLE siebel_accounts (
    -- Primary key (Siebel Row ID format)
    acct_id             VARCHAR(40)     PRIMARY KEY,
    acct_name           VARCHAR(255)    NOT NULL,
    acct_type           VARCHAR(20)     NOT NULL DEFAULT 'CUST',  -- CUST, PROSPECT, PARTNER
    acct_status         CHAR(1)         NOT NULL DEFAULT 'A',      -- A=Active, I=Inactive, D=Deleted

    -- Industry / classification
    industry_code       VARCHAR(10)     REFERENCES sap_industry_codes(industry_code),
    annual_revenue      NUMERIC(15,2),
    employee_count      INTEGER,
    sic_code            VARCHAR(10),
    duns_number         VARCHAR(20),
    ticker_symbol       VARCHAR(10),
    naics_code          VARCHAR(10),

    -- Billing address
    bill_addr_line1     VARCHAR(255),
    bill_addr_line2     VARCHAR(255),
    bill_city           VARCHAR(100),
    bill_state          VARCHAR(50),
    bill_postal_code    VARCHAR(20),
    bill_country        VARCHAR(100)    DEFAULT 'United States',

    -- Shipping address
    ship_addr_line1     VARCHAR(255),
    ship_city           VARCHAR(100),
    ship_state          VARCHAR(50),
    ship_postal_code    VARCHAR(20),
    ship_country        VARCHAR(100),

    -- Contact
    phone_number        VARCHAR(30),
    fax_number          VARCHAR(30),
    website             VARCHAR(500),
    email               VARCHAR(255),

    -- Ownership
    parent_acct_id      VARCHAR(40),                              -- Self-referencing FK
    account_owner_id    VARCHAR(40),                             -- Legacy user ID

    -- Migration state
    sf_id               VARCHAR(18),                             -- Populated after migration
    migration_status    VARCHAR(20)     DEFAULT 'PENDING',       -- PENDING, IN_PROGRESS, MIGRATED, FAILED
    migration_error     TEXT,
    migration_attempts  SMALLINT        DEFAULT 0,
    last_migrated_at    TIMESTAMPTZ,

    -- Audit
    created_by          VARCHAR(40)     DEFAULT 'SYSTEM',
    created_ts          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    modified_by         VARCHAR(40)     DEFAULT 'SYSTEM',
    modified_ts         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    is_deleted          BOOLEAN         DEFAULT FALSE
);

CREATE INDEX idx_siebel_accounts_status      ON siebel_accounts(acct_status);
CREATE INDEX idx_siebel_accounts_migration   ON siebel_accounts(migration_status);
CREATE INDEX idx_siebel_accounts_sf_id       ON siebel_accounts(sf_id);
CREATE INDEX idx_siebel_accounts_industry    ON siebel_accounts(industry_code);
CREATE INDEX idx_siebel_accounts_modified    ON siebel_accounts(modified_ts);

-- ---------------------------------------------------------------------------
-- Contacts (Siebel S_CONTACT equivalent)
-- ---------------------------------------------------------------------------
CREATE TABLE siebel_contacts (
    contact_id          VARCHAR(40)     PRIMARY KEY,
    first_name          VARCHAR(100)    NOT NULL,
    last_name           VARCHAR(100)    NOT NULL,
    middle_name         VARCHAR(100),
    salutation          VARCHAR(20),    -- Mr., Mrs., Dr., etc.
    suffix              VARCHAR(20),    -- Jr., Sr., etc.

    -- Link to account
    acct_id             VARCHAR(40)     REFERENCES siebel_accounts(acct_id),
    contact_type        VARCHAR(20)     DEFAULT 'CONTACT',        -- CONTACT, LEAD, PARTNER_CONTACT
    contact_status      CHAR(1)         DEFAULT 'A',               -- A=Active, I=Inactive

    -- Job
    title               VARCHAR(100),
    department          VARCHAR(100),
    reports_to_id       VARCHAR(40),   -- Manager contact ID

    -- Communication
    email_primary       VARCHAR(255),
    email_secondary     VARCHAR(255),
    phone_work          VARCHAR(30),
    phone_mobile        VARCHAR(30),
    phone_home          VARCHAR(30),
    fax                 VARCHAR(30),

    -- Address (defaults to account if blank)
    mailing_addr_line1  VARCHAR(255),
    mailing_city        VARCHAR(100),
    mailing_state       VARCHAR(50),
    mailing_postal_code VARCHAR(20),
    mailing_country     VARCHAR(100),

    -- Demographics
    date_of_birth       DATE,
    gender              CHAR(1),       -- M, F, U
    preferred_language  VARCHAR(10)    DEFAULT 'en',
    email_opt_out       BOOLEAN        DEFAULT FALSE,
    do_not_call         BOOLEAN        DEFAULT FALSE,

    -- Migration state
    sf_id               VARCHAR(18),
    migration_status    VARCHAR(20)    DEFAULT 'PENDING',
    migration_error     TEXT,
    migration_attempts  SMALLINT       DEFAULT 0,
    last_migrated_at    TIMESTAMPTZ,

    -- Audit
    created_by          VARCHAR(40)    DEFAULT 'SYSTEM',
    created_ts          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    modified_by         VARCHAR(40)    DEFAULT 'SYSTEM',
    modified_ts         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    is_deleted          BOOLEAN        DEFAULT FALSE
);

CREATE INDEX idx_siebel_contacts_acct        ON siebel_contacts(acct_id);
CREATE INDEX idx_siebel_contacts_status      ON siebel_contacts(contact_status);
CREATE INDEX idx_siebel_contacts_migration   ON siebel_contacts(migration_status);
CREATE INDEX idx_siebel_contacts_email       ON siebel_contacts(email_primary);
CREATE INDEX idx_siebel_contacts_sf_id       ON siebel_contacts(sf_id);

-- ---------------------------------------------------------------------------
-- Opportunities (Siebel S_OPTY equivalent)
-- ---------------------------------------------------------------------------
CREATE TABLE siebel_opportunities (
    opty_id             VARCHAR(40)     PRIMARY KEY,
    opty_name           VARCHAR(255)    NOT NULL,
    acct_id             VARCHAR(40)     REFERENCES siebel_accounts(acct_id),
    primary_contact_id  VARCHAR(40)     REFERENCES siebel_contacts(contact_id),

    -- Stage / Status
    sales_stage         VARCHAR(50)     NOT NULL DEFAULT 'Prospecting',
    opty_status         CHAR(1)         DEFAULT 'A',                -- A=Active, C=Closed, L=Lost
    close_date          DATE,
    probability         SMALLINT        CHECK (probability BETWEEN 0 AND 100),
    forecast_category   VARCHAR(50),    -- Omitted, Pipeline, Best Case, Commit, Closed

    -- Financials
    amount              NUMERIC(15,2),
    currency_code       CHAR(3)         DEFAULT 'USD',
    recurring_revenue   NUMERIC(15,2)   DEFAULT 0,
    contract_length_mo  SMALLINT,

    -- Classification
    opportunity_type    VARCHAR(50),    -- New Business, Renewal, Upsell, Cross-sell
    lead_source         VARCHAR(50),    -- Web, Referral, Campaign, Cold Call, etc.
    campaign_id         VARCHAR(40),

    -- Description
    description         TEXT,
    next_step           TEXT,
    loss_reason         VARCHAR(100),

    -- Ownership
    owner_id            VARCHAR(40),
    secondary_owner_id  VARCHAR(40),

    -- Migration state
    sf_id               VARCHAR(18),
    migration_status    VARCHAR(20)    DEFAULT 'PENDING',
    migration_error     TEXT,
    migration_attempts  SMALLINT       DEFAULT 0,
    last_migrated_at    TIMESTAMPTZ,

    -- Audit
    created_by          VARCHAR(40)    DEFAULT 'SYSTEM',
    created_ts          TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    modified_by         VARCHAR(40)    DEFAULT 'SYSTEM',
    modified_ts         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    is_deleted          BOOLEAN        DEFAULT FALSE
);

CREATE INDEX idx_siebel_opties_acct          ON siebel_opportunities(acct_id);
CREATE INDEX idx_siebel_opties_stage         ON siebel_opportunities(sales_stage);
CREATE INDEX idx_siebel_opties_migration     ON siebel_opportunities(migration_status);
CREATE INDEX idx_siebel_opties_close_date    ON siebel_opportunities(close_date);
CREATE INDEX idx_siebel_opties_sf_id         ON siebel_opportunities(sf_id);

-- ---------------------------------------------------------------------------
-- Field mapping table (legacy column → Salesforce field)
-- ---------------------------------------------------------------------------
CREATE TABLE migration_field_map (
    map_id              SERIAL          PRIMARY KEY,
    source_object       VARCHAR(50)     NOT NULL,   -- siebel_accounts, siebel_contacts, etc.
    source_column       VARCHAR(100)    NOT NULL,
    sf_object           VARCHAR(50)     NOT NULL,   -- Account, Contact, Opportunity
    sf_field            VARCHAR(100)    NOT NULL,
    transform_type      VARCHAR(20),               -- DIRECT, PICKLIST_MAP, CONCAT, FORMULA, SKIP
    transform_rule      TEXT,                       -- JSON or expression
    is_required         BOOLEAN         DEFAULT FALSE,
    is_external_id      BOOLEAN         DEFAULT FALSE,
    notes               TEXT,
    UNIQUE (source_object, source_column)
);

-- Account field mappings
INSERT INTO migration_field_map (source_object, source_column, sf_object, sf_field, transform_type, is_required) VALUES
    ('siebel_accounts', 'acct_id',          'Account', 'Legacy_ID__c',          'DIRECT',       TRUE),
    ('siebel_accounts', 'acct_name',         'Account', 'Name',                  'DIRECT',       TRUE),
    ('siebel_accounts', 'acct_type',         'Account', 'Type',                  'PICKLIST_MAP', FALSE),
    ('siebel_accounts', 'industry_code',     'Account', 'Industry',              'PICKLIST_MAP', FALSE),
    ('siebel_accounts', 'annual_revenue',    'Account', 'AnnualRevenue',         'DIRECT',       FALSE),
    ('siebel_accounts', 'employee_count',    'Account', 'NumberOfEmployees',     'DIRECT',       FALSE),
    ('siebel_accounts', 'phone_number',      'Account', 'Phone',                 'DIRECT',       FALSE),
    ('siebel_accounts', 'website',           'Account', 'Website',               'DIRECT',       FALSE),
    ('siebel_accounts', 'bill_addr_line1',   'Account', 'BillingStreet',         'DIRECT',       FALSE),
    ('siebel_accounts', 'bill_city',         'Account', 'BillingCity',           'DIRECT',       FALSE),
    ('siebel_accounts', 'bill_state',        'Account', 'BillingState',          'DIRECT',       FALSE),
    ('siebel_accounts', 'bill_postal_code',  'Account', 'BillingPostalCode',     'DIRECT',       FALSE),
    ('siebel_accounts', 'bill_country',      'Account', 'BillingCountry',        'DIRECT',       FALSE);

-- Contact field mappings
INSERT INTO migration_field_map (source_object, source_column, sf_object, sf_field, transform_type, is_required) VALUES
    ('siebel_contacts', 'contact_id',       'Contact', 'Legacy_ID__c',          'DIRECT',       TRUE),
    ('siebel_contacts', 'first_name',        'Contact', 'FirstName',             'DIRECT',       TRUE),
    ('siebel_contacts', 'last_name',         'Contact', 'LastName',              'DIRECT',       TRUE),
    ('siebel_contacts', 'acct_id',           'Contact', 'AccountId',             'SF_LOOKUP',    FALSE),
    ('siebel_contacts', 'title',             'Contact', 'Title',                 'DIRECT',       FALSE),
    ('siebel_contacts', 'department',        'Contact', 'Department',            'DIRECT',       FALSE),
    ('siebel_contacts', 'email_primary',     'Contact', 'Email',                 'DIRECT',       FALSE),
    ('siebel_contacts', 'phone_work',        'Contact', 'Phone',                 'DIRECT',       FALSE),
    ('siebel_contacts', 'phone_mobile',      'Contact', 'MobilePhone',           'DIRECT',       FALSE),
    ('siebel_contacts', 'email_opt_out',     'Contact', 'HasOptedOutOfEmail',    'DIRECT',       FALSE),
    ('siebel_contacts', 'do_not_call',       'Contact', 'DoNotCall',             'DIRECT',       FALSE);

COMMENT ON TABLE siebel_accounts    IS 'Source: Oracle Siebel 8.1 S_ORG_EXT equivalent — legacy accounts';
COMMENT ON TABLE siebel_contacts    IS 'Source: Oracle Siebel 8.1 S_CONTACT equivalent — legacy contacts';
COMMENT ON TABLE siebel_opportunities IS 'Source: Oracle Siebel 8.1 S_OPTY equivalent — legacy opportunities';
COMMENT ON TABLE migration_field_map IS 'Field-level mapping from legacy columns to Salesforce API fields';
