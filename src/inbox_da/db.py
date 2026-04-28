"""SQLite repository helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any


def connect(database_path: Path | str) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = files("inbox_da").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()


def insert_email(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    sender: str,
    received_at: str,
    subject: str,
    body: str,
    status: str = "new",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO emails (message_id, sender, received_at, subject, body, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, sender, received_at, subject, body, status),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_attachment(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    filename: str,
    mime_type: str,
    storage_path: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO attachments (email_id, filename, mime_type, storage_path)
        VALUES (?, ?, ?, ?)
        """,
        (email_id, filename, mime_type, storage_path),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_email(conn: sqlite3.Connection, email_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        raise LookupError(f"Email not found: {email_id}")
    return row


def list_email_dashboard_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            WITH latest_extractions AS (
                SELECT email_id, MAX(id) AS id
                FROM structured_extraction
                GROUP BY email_id
            ),
            latest_costs AS (
                SELECT email_id, MAX(id) AS id
                FROM email_processing_costs
                GROUP BY email_id
            ),
            latest_reviews AS (
                SELECT email_id, MAX(id) AS id
                FROM review_actions
                GROUP BY email_id
            )
            SELECT
                e.id,
                e.sender,
                e.received_at,
                e.subject,
                e.status,
                e.processed_at,
                s.id AS extraction_id,
                s.document_type,
                s.counterparty,
                s.amount,
                s.currency,
                s.verification_status,
                c.total_cost,
                c.total_tokens,
                c.model_calls,
                c.currency,
                r.action AS review_action,
                r.created_at AS review_created_at
            FROM emails e
            LEFT JOIN latest_extractions le ON le.email_id = e.id
            LEFT JOIN structured_extraction s ON s.id = le.id
            LEFT JOIN latest_costs lc ON lc.email_id = e.id
            LEFT JOIN email_processing_costs c ON c.id = lc.id
            LEFT JOIN latest_reviews lr ON lr.email_id = e.id
            LEFT JOIN review_actions r ON r.id = lr.id
            ORDER BY e.received_at DESC, e.id DESC
            """
        )
    )


def latest_structured_extraction(conn: sqlite3.Connection, email_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM structured_extraction
        WHERE email_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (email_id,),
    ).fetchone()


def list_attachments(conn: sqlite3.Connection, email_id: int) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM attachments WHERE email_id = ?", (email_id,)))


def update_attachment_parse(
    conn: sqlite3.Connection,
    *,
    attachment_id: int,
    extracted_text: str,
    parsed_payload: dict[str, Any] | list[Any] | None,
    vision_summary: dict[str, Any] | None,
) -> None:
    conn.execute(
        """
        UPDATE attachments
        SET extracted_text = ?, parsed_payload_json = ?, vision_summary_json = ?
        WHERE id = ?
        """,
        (
            extracted_text,
            json.dumps(parsed_payload or {}, sort_keys=True),
            json.dumps(vision_summary or {}, sort_keys=True),
            attachment_id,
        ),
    )
    conn.commit()


def insert_structured_extraction(
    conn: sqlite3.Connection, *, email_id: int, extraction: dict[str, Any]
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO structured_extraction (
            email_id, document_type, counterparty, account_or_customer, period_start,
            period_end, currency, amount, due_date, reference_number, financial_category,
            line_items_json, evidence_json, confidence, verification_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            extraction["document_type"],
            extraction["counterparty"],
            extraction.get("account_or_customer"),
            extraction.get("period_start"),
            extraction.get("period_end"),
            extraction["currency"],
            extraction["amount"],
            extraction.get("due_date"),
            extraction.get("reference_number"),
            extraction["financial_category"],
            json.dumps(extraction.get("line_items", []), sort_keys=True),
            json.dumps(extraction.get("evidence", []), sort_keys=True),
            extraction["confidence"],
            extraction["verification_status"],
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_extraction_run(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    run_type: str,
    model: str,
    raw_output: dict[str, Any],
    normalized_output: dict[str, Any],
    status: str,
    error: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO extraction_runs (
            email_id, run_type, model, raw_output_json, normalized_output_json,
            status, error, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            email_id,
            run_type,
            model,
            json.dumps(raw_output, sort_keys=True),
            json.dumps(normalized_output, sort_keys=True),
            status,
            error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_trace(
    conn: sqlite3.Connection,
    *,
    email_id: int | None,
    task_name: str,
    step_name: str,
    status: str,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO task_traces (
            email_id, task_name, step_name, status, detail_json, completed_at, error
        )
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """,
        (email_id, task_name, step_name, status, json.dumps(detail or {}, sort_keys=True), error),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_email_extraction_trace(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    actor: str,
    event_name: str,
    status: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO email_extraction_traces (
            email_id, actor, event_name, status, message, detail_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            actor,
            event_name,
            status,
            message,
            json.dumps(detail or {}, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_email_processing_cost(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    model: str,
    usage: dict[str, Any],
    cost: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO email_processing_costs (
            email_id, model, input_tokens, output_tokens, total_tokens, model_calls,
            input_cost, output_cost, total_cost, currency, input_price_per_1m,
            output_price_per_1m, pricing_source, detail_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            model,
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("total_tokens", 0)),
            int(usage.get("model_calls", 0)),
            float(cost.get("input_cost", 0.0)),
            float(cost.get("output_cost", 0.0)),
            float(cost.get("total_cost", 0.0)),
            str(cost.get("currency", "USD")),
            float(cost.get("input_price_per_1m", 0.0)),
            float(cost.get("output_price_per_1m", 0.0)),
            str(cost.get("pricing_source", "")),
            json.dumps(detail or {}, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def insert_review_action(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    extraction_id: int | None,
    action: str,
    reviewer: str | None,
    override_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO review_actions (
            email_id, extraction_id, action, reviewer, override_payload_json, note
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            email_id,
            extraction_id,
            action,
            reviewer,
            json.dumps(override_payload or {}, sort_keys=True),
            note,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_review_actions(conn: sqlite3.Connection, email_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM review_actions
            WHERE email_id = ?
            ORDER BY id DESC
            """,
            (email_id,),
        )
    )


def list_review_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            WITH latest_extractions AS (
                SELECT email_id, MAX(id) AS id
                FROM structured_extraction
                GROUP BY email_id
            ),
            latest_reviews AS (
                SELECT email_id, MAX(id) AS id
                FROM review_actions
                GROUP BY email_id
            )
            SELECT
                e.id AS email_id,
                e.sender,
                e.received_at,
                e.subject,
                e.status AS email_status,
                s.id AS extraction_id,
                s.document_type,
                s.counterparty,
                s.amount,
                s.currency,
                s.verification_status,
                r.action AS review_action,
                r.note AS review_note
            FROM emails e
            LEFT JOIN latest_extractions le ON le.email_id = e.id
            LEFT JOIN structured_extraction s ON s.id = le.id
            LEFT JOIN latest_reviews lr ON lr.email_id = e.id
            LEFT JOIN review_actions r ON r.id = lr.id
            WHERE
                s.verification_status = 'needs_review'
                OR e.status IN ('new', 'failed')
                OR r.action = 'override'
            ORDER BY e.received_at DESC, e.id DESC
            """
        )
    )


def latest_email_cost(conn: sqlite3.Connection, email_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM email_processing_costs
        WHERE email_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (email_id,),
    ).fetchone()


def list_latest_email_costs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            WITH latest_costs AS (
                SELECT email_id, MAX(id) AS id
                FROM email_processing_costs
                GROUP BY email_id
            ),
            latest_extractions AS (
                SELECT email_id, MAX(id) AS id
                FROM structured_extraction
                GROUP BY email_id
            )
            SELECT
                c.id AS cost_id,
                c.email_id,
                e.subject,
                e.sender,
                e.status AS email_status,
                s.verification_status,
                s.document_type,
                s.amount AS extracted_amount,
                s.currency AS extracted_currency,
                c.model,
                c.input_tokens,
                c.output_tokens,
                c.total_tokens,
                c.model_calls,
                c.input_cost,
                c.output_cost,
                c.total_cost,
                c.currency,
                c.input_price_per_1m,
                c.output_price_per_1m,
                c.pricing_source,
                c.created_at
            FROM latest_costs lc
            JOIN email_processing_costs c ON c.id = lc.id
            JOIN emails e ON e.id = c.email_id
            LEFT JOIN latest_extractions le ON le.email_id = c.email_id
            LEFT JOIN structured_extraction s ON s.id = le.id
            ORDER BY c.created_at DESC, c.id DESC
            """
        )
    )


def summarize_latest_email_costs(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        WITH latest_costs AS (
            SELECT email_id, MAX(id) AS id
            FROM email_processing_costs
            GROUP BY email_id
        )
        SELECT
            COUNT(*) AS email_count,
            COALESCE(SUM(c.input_tokens), 0) AS input_tokens,
            COALESCE(SUM(c.output_tokens), 0) AS output_tokens,
            COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
            COALESCE(SUM(c.model_calls), 0) AS model_calls,
            COALESCE(SUM(c.input_cost), 0.0) AS input_cost,
            COALESCE(SUM(c.output_cost), 0.0) AS output_cost,
            COALESCE(SUM(c.total_cost), 0.0) AS total_cost,
            COALESCE(MAX(c.currency), 'USD') AS currency
        FROM latest_costs lc
        JOIN email_processing_costs c ON c.id = lc.id
        """
    ).fetchone()
    return dict(row) if row else {}


def list_email_extraction_traces(
    conn: sqlite3.Connection, email_id: int, *, limit: int | None = 25
) -> list[sqlite3.Row]:
    if limit is None:
        return list(
            conn.execute(
                """
                SELECT * FROM email_extraction_traces
                WHERE email_id = ?
                ORDER BY id DESC
                """,
                (email_id,),
            )
        )
    return list(
        conn.execute(
            """
            SELECT * FROM email_extraction_traces
            WHERE email_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (email_id, limit),
        )
    )


def mark_email_status(conn: sqlite3.Connection, email_id: int, status: str) -> None:
    processed_at = "CURRENT_TIMESTAMP" if status in {"processed", "failed"} else "NULL"
    conn.execute(
        f"UPDATE emails SET status = ?, processed_at = {processed_at} WHERE id = ?",
        (status, email_id),
    )
    conn.commit()


def claim_new_emails(conn: sqlite3.Connection, limit: int = 1) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM emails WHERE status = 'new' ORDER BY received_at, id LIMIT ?",
        (limit,),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE emails SET status = 'processing' WHERE id IN ({placeholders})", ids)
        conn.commit()
    return ids


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
