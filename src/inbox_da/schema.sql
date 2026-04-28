PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    received_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    extracted_text TEXT,
    parsed_payload_json TEXT,
    vision_summary_json TEXT,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS structured_extraction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    document_type TEXT NOT NULL,
    counterparty TEXT NOT NULL,
    account_or_customer TEXT,
    period_start TEXT,
    period_end TEXT,
    currency TEXT NOT NULL,
    amount REAL NOT NULL,
    due_date TEXT,
    reference_number TEXT,
    financial_category TEXT NOT NULL,
    line_items_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    verification_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    run_type TEXT NOT NULL,
    model TEXT NOT NULL,
    raw_output_json TEXT NOT NULL,
    normalized_output_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER,
    task_name TEXT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    error TEXT,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_extraction_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    actor TEXT NOT NULL,
    event_name TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_processing_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    model_calls INTEGER NOT NULL,
    input_cost REAL NOT NULL,
    output_cost REAL NOT NULL,
    total_cost REAL NOT NULL,
    currency TEXT NOT NULL,
    input_price_per_1m REAL NOT NULL,
    output_price_per_1m REAL NOT NULL,
    pricing_source TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id INTEGER NOT NULL,
    extraction_id INTEGER,
    action TEXT NOT NULL,
    reviewer TEXT,
    override_payload_json TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE,
    FOREIGN KEY (extraction_id) REFERENCES structured_extraction(id) ON DELETE SET NULL
);
