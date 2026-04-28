import sqlite3
from pathlib import Path

import fitz
import pytest

from inbox_da import db
from inbox_da.attachments import parse_attachment
from inbox_da.config import Settings
from inbox_da.synthetic import seed_database


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def parse_image(self, path: Path, *, mime_type: str) -> dict[str, str]:
        self.calls.append((path, mime_type))
        return {"model_response": "Reference IMG-0001 Amount USD 44.00"}


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.sqlite")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_schema_and_foreign_keys(conn: sqlite3.Connection) -> None:
    email_id = db.insert_email(
        conn,
        message_id="<a@test>",
        sender="sender@example.com",
        received_at="2026-04-28T10:00:00Z",
        subject="Subject",
        body="Body",
    )
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


def test_seed_generates_core_attachment_types(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ids = seed_database(conn, count=8, attachments_dir=tmp_path / "attachments")
    assert len(ids) == 8
    rows = conn.execute("SELECT mime_type FROM attachments").fetchall()
    mime_types = {row["mime_type"] for row in rows}
    assert "image/png" in mime_types
    assert "application/pdf" in mime_types
    assert "text/csv" in mime_types
    assert "text/plain" in mime_types
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in mime_types


def test_seed_can_be_called_repeatedly(conn: sqlite3.Connection, tmp_path: Path) -> None:
    first = seed_database(conn, count=2, attachments_dir=tmp_path / "attachments")
    second = seed_database(conn, count=1, attachments_dir=tmp_path / "attachments")
    assert first == [1, 2]
    assert second == [3]
    rows = conn.execute("SELECT message_id FROM emails ORDER BY id").fetchall()
    assert [row["message_id"] for row in rows] == [
        "<synthetic-1@inbox-da.local>",
        "<synthetic-2@inbox-da.local>",
        "<synthetic-3@inbox-da.local>",
    ]


def test_parse_text_csv_xlsx_pdf_and_image(conn: sqlite3.Connection, tmp_path: Path) -> None:
    seed_database(conn, count=5, attachments_dir=tmp_path / "attachments")
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
