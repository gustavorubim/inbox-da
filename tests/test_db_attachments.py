import csv
import sqlite3
from pathlib import Path

import fitz
import pytest
from langchain_core.messages import HumanMessage
from openpyxl import Workbook
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from inbox_da import db
from inbox_da.attachments import GeminiVisionParser, parse_attachment
from inbox_da.config import Settings


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def parse_image(self, path: Path, *, mime_type: str) -> dict[str, str]:
        self.calls.append((path, mime_type))
        return {"model_response": "Reference IMG-0001 Amount USD 44.00"}


class FakeGeminiModel:
    def __init__(self) -> None:
        self.invocations: list[object] = []

    def invoke(self, value: object) -> object:
        self.invocations.append(value)
        return type(
            "Response",
            (),
            {
                "content": "vision response",
                "usage_metadata": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )()


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.sqlite")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def insert_sample_email(conn: sqlite3.Connection) -> int:
    return db.insert_email(
        conn,
        message_id="<oracle-fixture@example.test>",
        sender="billing@northstar.example",
        received_at="2026-04-28T10:00:00Z",
        subject="Invoice INV-0001 from Northstar Supplies",
        body=(
            "Please review INV-0001. The invoice total is USD 1,037.25. "
            "Counterparty: Northstar Supplies. Due date: 2026-05-15. "
            "Account: Operations-4182."
        ),
    )


def create_core_attachments(
    conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path
) -> None:
    attachments_dir.mkdir(parents=True, exist_ok=True)
    _create_txt(conn, email_id=email_id, attachments_dir=attachments_dir)
    _create_csv(conn, email_id=email_id, attachments_dir=attachments_dir)
    _create_xlsx(conn, email_id=email_id, attachments_dir=attachments_dir)
    _create_pdf(conn, email_id=email_id, attachments_dir=attachments_dir)
    _create_png(conn, email_id=email_id, attachments_dir=attachments_dir)


def _create_txt(conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path) -> None:
    path = attachments_dir / "memo.txt"
    path.write_text("INV-0001\nAmount: USD 1037.25\nCounterparty: Northstar Supplies\n")
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=path.name,
        mime_type="text/plain",
        storage_path=str(path),
    )


def _create_csv(conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path) -> None:
    path = attachments_dir / "line-items.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["reference", "description", "amount", "currency"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "reference": "INV-0001",
                "description": "Financial services",
                "amount": "1037.25",
                "currency": "USD",
            }
        )
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=path.name,
        mime_type="text/csv",
        storage_path=str(path),
    )


def _create_xlsx(conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path) -> None:
    path = attachments_dir / "schedule.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["reference", "period", "amount", "currency"])
    sheet.append(["INV-0001", "2026-04", 1037.25, "USD"])
    workbook.save(path)
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=path.name,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        storage_path=str(path),
    )


def _create_pdf(conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path) -> None:
    path = attachments_dir / "invoice.pdf"
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawString(72, 720, "Reference: INV-0001")
    pdf.drawString(72, 700, "Counterparty: Northstar Supplies")
    pdf.drawString(72, 680, "Amount Due: USD 1037.25")
    pdf.drawString(72, 660, "Due Date: 2026-05-15")
    pdf.save()
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=path.name,
        mime_type="application/pdf",
        storage_path=str(path),
    )


def _create_png(conn: sqlite3.Connection, *, email_id: int, attachments_dir: Path) -> None:
    path = attachments_dir / "receipt.png"
    image = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 880, 400), outline="black", width=3)
    draw.text((60, 80), "Financial Receipt", fill="black")
    draw.text((60, 140), "Reference: INV-0001", fill="black")
    draw.text((60, 200), "Amount: USD 1037.25", fill="black")
    draw.text((60, 260), "Counterparty: Northstar Supplies", fill="black")
    image.save(path)
    db.insert_attachment(
        conn,
        email_id=email_id,
        filename=path.name,
        mime_type="image/png",
        storage_path=str(path),
    )


def test_schema_and_foreign_keys(conn: sqlite3.Connection) -> None:
    email_id = db.insert_email(
        conn,
        message_id="<a@test>",
        sender="sender@example.com",
        received_at="2026-04-28T10:00:00Z",
        subject="Subject",
        body="Body",
    )
    assert db.get_email_id_by_message_id(conn, "<a@test>") == email_id
    assert db.get_email_id_by_message_id(conn, "<missing@test>") is None
    attachment_id = db.insert_attachment(
        conn,
        email_id=email_id,
        filename="a.txt",
        mime_type="text/plain",
        storage_path="/tmp/a.txt",
    )
    assert attachment_id > 0
    db.insert_email_extraction_trace(
        conn,
        email_id=email_id,
        actor="tester",
        event_name="event",
        status="ok",
        message="message",
    )
    db.insert_email_processing_cost(
        conn,
        email_id=email_id,
        model="gemini-3-flash-preview",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "model_calls": 1},
        cost={
            "input_cost": 0.1,
            "output_cost": 0.2,
            "total_cost": 0.3,
            "currency": "USD",
            "input_price_per_1m": 0.5,
            "output_price_per_1m": 3.0,
            "pricing_source": "test",
        },
    )
    assert db.latest_email_cost(conn, email_id)["total_cost"] == 0.3
    report_rows = db.list_latest_email_costs(conn)
    assert report_rows[0]["email_id"] == email_id
    assert report_rows[0]["total_cost"] == 0.3
    summary = db.summarize_latest_email_costs(conn)
    assert summary["email_count"] == 1
    assert summary["total_tokens"] == 15
    assert summary["total_cost"] == 0.3
    assert len(db.list_email_extraction_traces(conn, email_id)) == 1
    assert len(db.list_email_extraction_traces(conn, email_id, limit=None)) == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_attachment(
            conn,
            email_id=999,
            filename="bad.txt",
            mime_type="text/plain",
            storage_path="/tmp/bad.txt",
        )


def test_review_queue_hides_resolved_needs_review_items(conn: sqlite3.Connection) -> None:
    email_id = db.insert_email(
        conn,
        message_id="<review@test>",
        sender="sender@example.com",
        received_at="2026-04-28T10:00:00Z",
        subject="Review",
        body="Needs review",
        status="processed",
    )
    extraction_id = db.insert_structured_extraction(
        conn,
        email_id=email_id,
        extraction={
            "document_type": "invoice",
            "counterparty": "Acme",
            "currency": "USD",
            "amount": 100.0,
            "financial_category": "accounts_payable",
            "line_items": [],
            "evidence": [{"source": "email_body", "quote": "Amount USD 100"}],
            "confidence": 0.7,
            "verification_status": "needs_review",
        },
    )
    assert [row["email_id"] for row in db.list_review_queue(conn)] == [email_id]

    db.insert_review_action(
        conn,
        email_id=email_id,
        extraction_id=extraction_id,
        action="select_candidate",
        reviewer="tester",
    )
    assert db.list_review_queue(conn) == []

    db.insert_review_action(
        conn,
        email_id=email_id,
        extraction_id=extraction_id,
        action="override",
        reviewer="tester",
    )
    assert [row["email_id"] for row in db.list_review_queue(conn)] == [email_id]


def test_relative_database_connection_survives_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(workspace)
    connection = db.connect("data/test.sqlite")
    db.init_db(connection)
    monkeypatch.chdir(other)
    try:
        email_id = db.insert_email(
            connection,
            message_id="<cwd@test>",
            sender="sender@example.com",
            received_at="2026-04-28T10:00:00Z",
            subject="Subject",
            body="Body",
        )
        assert email_id == 1
        assert (workspace / "data" / "test.sqlite").exists()
    finally:
        connection.close()


def test_local_fixture_has_core_attachment_types(conn: sqlite3.Connection, tmp_path: Path) -> None:
    email_id = insert_sample_email(conn)
    create_core_attachments(conn, email_id=email_id, attachments_dir=tmp_path / "attachments")
    rows = conn.execute("SELECT mime_type FROM attachments").fetchall()
    mime_types = {row["mime_type"] for row in rows}
    assert "image/png" in mime_types
    assert "application/pdf" in mime_types
    assert "text/csv" in mime_types
    assert "text/plain" in mime_types
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in mime_types


def test_parse_text_csv_xlsx_pdf_and_image(conn: sqlite3.Connection, tmp_path: Path) -> None:
    email_id = insert_sample_email(conn)
    create_core_attachments(conn, email_id=email_id, attachments_dir=tmp_path / "attachments")
    settings = Settings(attachment_text_min_chars=5)
    fake = FakeVision()
    rows = conn.execute("SELECT * FROM attachments").fetchall()
    seen_kinds: set[str] = set()
    for row in rows:
        text, payload, vision = parse_attachment(
            Path(row["storage_path"]),
            mime_type=row["mime_type"],
            settings=settings,
            vision_parser=fake,
        )
        assert payload["kind"] in {"text", "csv", "xlsx", "pdf_text", "image_vision"}
        seen_kinds.add(payload["kind"])
        assert text
        if payload["kind"] == "image_vision":
            assert vision is not None
    assert {"text", "csv", "xlsx", "pdf_text", "image_vision"}.issubset(seen_kinds)
    assert fake.calls


def test_low_text_pdf_routes_to_vision(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()
    fake = FakeVision()
    text, payload, vision = parse_attachment(
        path,
        mime_type="application/pdf",
        settings=Settings(attachment_text_min_chars=100),
        vision_parser=fake,
    )
    assert text == ""
    assert payload["kind"] == "pdf_vision_fallback"
    assert vision == {"model_response": "Reference IMG-0001 Amount USD 44.00"}


def test_gemini_vision_parser_wraps_content_blocks_in_human_message(tmp_path: Path) -> None:
    path = tmp_path / "receipt.png"
    path.write_bytes(b"not-a-real-image-but-valid-bytes-for-base64")
    model = FakeGeminiModel()
    summary = GeminiVisionParser(model).parse_image(path, mime_type="image/png")
    assert summary["model_response"] == "vision response"
    assert summary["token_usage"]["model_calls"] == 1
    invocation = model.invocations[0]
    assert isinstance(invocation, list)
    assert isinstance(invocation[0], HumanMessage)
    assert invocation[0].content[0]["type"] == "text"
    assert invocation[0].content[1]["type"] == "image_url"
