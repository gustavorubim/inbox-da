import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from inbox_da import db
from inbox_da.config import OracleInboxSettings, Settings
from inbox_da.oracle_inbox import (
    OracleAttachmentRecord,
    OracleConfigError,
    OracleEmailRecord,
    OracleInboxClient,
    sync_oracle_inbox,
    validate_oracle_settings,
)


class FakeOracleReader:
    def __init__(self) -> None:
        self.attachment_calls: list[str] = []

    def fetch_emails(self, *, limit: int):
        assert limit == 5
        return [
            OracleEmailRecord(
                message_id="<oracle-1@example.test>",
                sender="billing@northstar.example",
                received_at="2026-04-28T10:00:00Z",
                subject="Invoice INV-0001 from Northstar Supplies",
                body="Invoice INV-0001 total USD 1,037.25",
            ),
            OracleEmailRecord(
                message_id="<oracle-2@example.test>",
                sender="alerts@metrobank.example",
                received_at="2026-04-28T09:00:00Z",
                subject="Bank notice",
                body="Bank notice total USD 1,074.50",
            ),
        ]

    def fetch_attachments(self, *, message_id: str):
        self.attachment_calls.append(message_id)
        return [
            OracleAttachmentRecord(
                filename="../invoice.txt",
                mime_type="text/plain",
                content=f"{message_id} attachment".encode(),
            )
        ]


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "oracle.sqlite")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_sync_oracle_inbox_inserts_and_skips_duplicates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = Settings(
        database_path=tmp_path / "oracle.sqlite",
        attachments_dir=tmp_path / "attachments",
        oracle=OracleInboxSettings(enabled=True, fetch_limit=5),
    )
    reader = FakeOracleReader()

    first = sync_oracle_inbox(conn, settings=settings, client=reader)
    second = sync_oracle_inbox(conn, settings=settings, client=reader)

    assert first.fetched == 2
    assert first.inserted == 2
    assert first.skipped == 0
    assert first.attachments_inserted == 2
    assert second.inserted == 0
    assert second.skipped == 2
    assert (
        db.get_email(conn, first.inserted_email_ids[0])["message_id"] == "<oracle-1@example.test>"
    )
    attachments = db.list_attachments(conn, first.inserted_email_ids[0])
    assert attachments[0]["filename"] == "invoice.txt"
    assert Path(attachments[0]["storage_path"]).read_text() == "<oracle-1@example.test> attachment"
    traces = conn.execute(
        "SELECT COUNT(*) AS n FROM task_traces WHERE task_name = 'sync_oracle_inbox'"
    ).fetchone()
    assert traces["n"] >= 4


def test_validate_oracle_settings_rejects_missing_credentials_and_bad_identifiers() -> None:
    with pytest.raises(OracleConfigError, match="password"):
        validate_oracle_settings(
            OracleInboxSettings(
                host="db.example.test",
                service_name="finance",
                username="reader",
                password="",
            )
        )

    with pytest.raises(OracleConfigError, match="Unsafe Oracle identifier"):
        validate_oracle_settings(
            OracleInboxSettings(
                host="db.example.test",
                service_name="finance",
                username="reader",
                password="secret",
                email_table="EMAILS;DROP",
            )
        )


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.description: list[tuple[str]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        pass

    def execute(self, sql: str, binds: dict[str, Any]) -> None:
        self.connection.statements.append((sql, binds))
        if "CONTENT_BLOB" in sql:
            self.description = [("FILENAME",), ("MIME_TYPE",), ("CONTENT",)]
            self.connection.active_rows = [
                ("invoice.txt", "text/plain", FakeLob(b"attachment bytes"))
            ]
        else:
            self.description = [
                ("MESSAGE_ID",),
                ("SENDER",),
                ("RECEIVED_AT",),
                ("SUBJECT",),
                ("BODY",),
            ]
            self.connection.active_rows = [
                (
                    "<oracle-client@example.test>",
                    "sender@example.test",
                    datetime(2026, 4, 28, 10, 0),
                    "Subject",
                    FakeLob(b"Body text"),
                )
            ]

    def fetchall(self):
        return self.connection.active_rows


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.active_rows: list[tuple[Any, ...]] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeLob:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content


def test_oracle_inbox_client_reads_rows_with_configured_columns() -> None:
    connection = FakeConnection()
    settings = OracleInboxSettings(
        host="db.example.test",
        service_name="finance",
        username="reader",
        password="secret",
        email_table="FINANCE.EMAILS",
        attachment_table="FINANCE.ATTACHMENTS",
    )
    client = OracleInboxClient(settings, connection=connection)

    emails = client.fetch_emails(limit=1)
    attachments = client.fetch_attachments(message_id=emails[0].message_id)

    assert emails[0].received_at == "2026-04-28T10:00:00"
    assert emails[0].body == "Body text"
    assert attachments[0].content == b"attachment bytes"
    assert connection.statements[0][1] == {"limit": 1}
    assert "FINANCE.EMAILS" in connection.statements[0][0]
    assert connection.closed is False
