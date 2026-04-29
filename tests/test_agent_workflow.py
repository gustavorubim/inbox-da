import sqlite3
from pathlib import Path
from typing import Any

import pytest

from inbox_da import db
from inbox_da.agent import (
    AgentRun,
    HeuristicExtractor,
    RuleVerifier,
    _coerce_agent_json,
    normalize_extraction_payload,
    normalize_verification_payload,
    process_email,
)
from inbox_da.config import Settings
from inbox_da.costing import TokenUsage
from inbox_da.models import StructuredExtraction, VerificationResult


class DisagreeingExtractor:
    def extract(self, context: dict[str, Any], *, pass_name: str) -> dict[str, Any]:
        amount = 10.0 if pass_name == "A" else 11.0
        return {
            "document_type": "invoice",
            "counterparty": "Northstar Supplies",
            "account_or_customer": "Operations-4182",
            "currency": "USD",
            "amount": amount,
            "reference_number": "INV-0001",
            "financial_category": "accounts_payable",
            "line_items": [{"description": "Services", "amount": amount, "currency": "USD"}],
            "evidence": [{"source": "email.body", "quote": context["email"]["body"][:40]}],
            "confidence": 0.9,
            "verification_status": "candidate",
        }


class NeedsReviewVerifier:
    def verify(
        self,
        context: dict[str, Any],
        left: StructuredExtraction,
        right: StructuredExtraction,
    ) -> VerificationResult:
        del context, left, right
        return VerificationResult(status="needs_review", reason="Mismatch", conflicts=["amount"])


@pytest.fixture
def inbox_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "workflow.sqlite")
    db.init_db(conn)
    email_id = db.insert_email(
        conn,
        message_id="<oracle-1@example.test>",
        sender="billing@northstar.example",
        received_at="2026-04-28T10:00:00Z",
        subject="Invoice INV-0001 from Northstar Supplies",
        body=(
            "Please review INV-0001. The invoice total is USD 1,037.25. "
            "Counterparty: Northstar Supplies. Due date: 2026-05-15. "
            "Account: Operations-4182."
        ),
    )
    attachment_dir = tmp_path / "attachments" / str(email_id)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    attachment_path = attachment_dir / "invoice.txt"
    attachment_path.write_text("INV-0001\nAmount Due: USD 1037.25\n")
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=attachment_path.name,
        mime_type="text/plain",
        storage_path=str(attachment_path),
    )
    settings = Settings(
        database_path=tmp_path / "workflow.sqlite", attachments_dir=tmp_path / "attachments"
    )
    try:
        yield conn, email_id, settings
    finally:
        conn.close()


def test_process_email_verified_path(inbox_conn: tuple[sqlite3.Connection, int, Settings]) -> None:
    conn, email_id, settings = inbox_conn
    extraction_id = process_email(
        conn,
        email_id=email_id,
        settings=settings,
        extractor=HeuristicExtractor(),
        verifier=RuleVerifier(),
        require_credentials=False,
    )
    row = conn.execute(
        "SELECT * FROM structured_extraction WHERE id = ?", (extraction_id,)
    ).fetchone()
    assert row["verification_status"] == "verified"
    assert row["amount"] > 0
    assert conn.execute("SELECT COUNT(*) AS n FROM extraction_runs").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM task_traces").fetchone()["n"] >= 3
    assert conn.execute("SELECT COUNT(*) AS n FROM email_extraction_traces").fetchone()["n"] >= 8
    cost = conn.execute(
        "SELECT * FROM email_processing_costs WHERE email_id = ?", (email_id,)
    ).fetchone()
    assert cost["total_cost"] == 0
    assert cost["model_calls"] == 0


def test_process_email_needs_review_path(
    inbox_conn: tuple[sqlite3.Connection, int, Settings],
) -> None:
    conn, email_id, settings = inbox_conn
    extraction_id = process_email(
        conn,
        email_id=email_id,
        settings=settings,
        extractor=DisagreeingExtractor(),
        verifier=NeedsReviewVerifier(),
        require_credentials=False,
    )
    row = conn.execute(
        "SELECT verification_status FROM structured_extraction WHERE id = ?", (extraction_id,)
    ).fetchone()
    assert row["verification_status"] == "needs_review"


def test_agent_run_usage_is_persisted(
    inbox_conn: tuple[sqlite3.Connection, int, Settings],
) -> None:
    class UsageExtractor:
        def extract(self, context: dict[str, Any], *, pass_name: str) -> AgentRun:
            del pass_name
            amount = 10.0
            return AgentRun(
                payload={
                    "document_type": "invoice",
                    "counterparty": "Northstar Supplies",
                    "currency": "USD",
                    "amount": amount,
                    "reference_number": "INV-0001",
                    "financial_category": "accounts_payable",
                    "line_items": [
                        {"description": "Services", "amount": amount, "currency": "USD"}
                    ],
                    "evidence": [{"source": "email.body", "quote": context["email"]["body"][:40]}],
                    "confidence": 0.9,
                    "verification_status": "candidate",
                },
                usage=TokenUsage(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                    model_calls=1,
                ),
            )

    conn, email_id, settings = inbox_conn
    process_email(
        conn,
        email_id=email_id,
        settings=settings,
        extractor=UsageExtractor(),
        verifier=RuleVerifier(),
        require_credentials=False,
    )
    cost = conn.execute(
        "SELECT * FROM email_processing_costs WHERE email_id = ?", (email_id,)
    ).fetchone()
    assert cost["input_tokens"] == 200
    assert cost["output_tokens"] == 40
    assert cost["total_cost"] > 0


def test_coerce_agent_json_from_markdown() -> None:
    assert _coerce_agent_json('```json\n{"status":"ok"}\n```') == {"status": "ok"}


def test_coerce_agent_json_from_message_object() -> None:
    message = type("Message", (), {"content": '{"status":"ok"}'})()
    assert _coerce_agent_json({"messages": [message]}) == {"status": "ok"}


def test_coerce_agent_json_from_content_blocks_and_python_repr() -> None:
    message = type("Message", (), {"content": [{"type": "text", "text": "{'status':'ok'}"}]})()
    assert _coerce_agent_json({"messages": [message]}) == {"status": "ok"}


def test_normalize_extraction_payload_from_wrapped_model_fields() -> None:
    raw = {
        "invoice_number": {"value": "INV-1", "evidence": "subject line"},
        "counterparty": {"value": "Vendor", "evidence": "body"},
        "currency": {"value": "USD", "evidence": "csv"},
        "total_amount": {"value": 12.5, "evidence": "invoice total"},
        "due_date": {"value": "2026-05-15", "evidence": "due date"},
        "line_items": [
            {
                "description": {"value": "Services", "evidence": "csv description"},
                "amount": {"value": 12.5, "evidence": "csv amount"},
                "currency": {"value": "USD", "evidence": "csv currency"},
            }
        ],
    }
    normalized = normalize_extraction_payload(raw)
    assert normalized["document_type"] == "invoice"
    assert normalized["reference_number"] == "INV-1"
    assert normalized["amount"] == 12.5
    assert normalized["line_items"][0]["description"] == "Services"
    assert normalized["evidence"]


def test_normalize_extraction_payload_coerces_model_string_numbers() -> None:
    raw = {
        "document_type": "invoice",
        "counterparty": "Northstar Supplies",
        "currency": "USD",
        "amount_due": "USD 1,037.25",
        "reference_number": "INV-0001",
        "financial_category": "accounts_payable",
        "confidence": "high",
        "line_items": [
            {
                "description": "Consulting",
                "amount": "$1,037.25",
                "currency": "USD",
            }
        ],
    }
    normalized = normalize_extraction_payload(raw)
    assert normalized["amount"] == 1037.25
    assert normalized["line_items"][0]["amount"] == 1037.25
    assert normalized["confidence"] == 0.85


def test_normalize_verification_payload_maps_model_statuses() -> None:
    assert normalize_verification_payload({"status": "match", "summary": "same"}) == {
        "status": "verified",
        "reason": "same",
        "conflicts": [],
    }
    assert normalize_verification_payload({"status": "mismatch", "issues": "amount"}) == {
        "status": "needs_review",
        "reason": "Verifier returned status: mismatch",
        "conflicts": ["amount"],
    }


def test_mark_and_claim_email(inbox_conn: tuple[sqlite3.Connection, int, Settings]) -> None:
    conn, email_id, _settings = inbox_conn
    claimed = db.claim_new_emails(conn)
    assert claimed == [email_id]
    assert db.get_email(conn, email_id)["status"] == "processing"
    db.mark_email_status(conn, email_id, "failed")
    assert db.get_email(conn, email_id)["status"] == "failed"
