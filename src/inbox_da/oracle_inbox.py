"""Read-only Oracle inbox ingestion."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from inbox_da import db
from inbox_da.config import OracleInboxSettings, Settings

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


class OracleConfigError(ValueError):
    """Raised when Oracle inbox configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class OracleEmailRecord:
    message_id: str
    sender: str
    received_at: str
    subject: str
    body: str


@dataclass(frozen=True)
class OracleAttachmentRecord:
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class OracleSyncResult:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    attachments_inserted: int = 0
    inserted_email_ids: tuple[int, ...] = field(default_factory=tuple)


class OracleInboxReader(Protocol):
    def fetch_emails(self, *, limit: int) -> Sequence[OracleEmailRecord]: ...

    def fetch_attachments(self, *, message_id: str) -> Sequence[OracleAttachmentRecord]: ...


class OracleInboxClient(AbstractContextManager["OracleInboxClient"]):
    """Small read-only adapter over the configured Oracle inbox tables."""

    def __init__(
        self,
        oracle: OracleInboxSettings,
        *,
        connection: Any | None = None,
    ) -> None:
        validate_oracle_settings(oracle)
        self.oracle = oracle
        self._connection = connection
        self._owns_connection = connection is None

    def __enter__(self) -> OracleInboxClient:
        if self._connection is None:
            self._connection = _connect_oracle(self.oracle)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._owns_connection and self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> Any:
        if self._connection is None:
            self._connection = _connect_oracle(self.oracle)
        return self._connection

    def fetch_emails(self, *, limit: int) -> Sequence[OracleEmailRecord]:
        email_columns = self.oracle.email_columns
        table = _identifier_path(self.oracle.email_table)
        received_column = _identifier_path(email_columns.received_at)
        message_column = _identifier_path(email_columns.message_id)
        sql = f"""
            SELECT *
            FROM (
                SELECT
                    {_identifier_path(email_columns.message_id)} AS message_id,
                    {_identifier_path(email_columns.sender)} AS sender,
                    {_identifier_path(email_columns.received_at)} AS received_at,
                    {_identifier_path(email_columns.subject)} AS subject,
                    {_identifier_path(email_columns.body)} AS body
                FROM {table}
                ORDER BY {received_column} DESC, {message_column} DESC
            )
            WHERE ROWNUM <= :limit
        """
        rows = self._execute(sql, {"limit": max(1, int(limit))})
        return tuple(
            OracleEmailRecord(
                message_id=_to_text(row["message_id"]),
                sender=_to_text(row["sender"]),
                received_at=_to_iso_text(row["received_at"]),
                subject=_to_text(row["subject"]),
                body=_to_text(row["body"]),
            )
            for row in rows
        )

    def fetch_attachments(self, *, message_id: str) -> Sequence[OracleAttachmentRecord]:
        if not self.oracle.attachment_table:
            return ()
        attachment_columns = self.oracle.attachment_columns
        table = _identifier_path(self.oracle.attachment_table)
        sql = f"""
            SELECT
                {_identifier_path(attachment_columns.filename)} AS filename,
                {_identifier_path(attachment_columns.mime_type)} AS mime_type,
                {_identifier_path(attachment_columns.content)} AS content
            FROM {table}
            WHERE {_identifier_path(attachment_columns.message_id)} = :message_id
            ORDER BY {_identifier_path(attachment_columns.filename)}
        """
        rows = self._execute(sql, {"message_id": message_id})
        return tuple(
            OracleAttachmentRecord(
                filename=_to_text(row["filename"]),
                mime_type=_to_text(row["mime_type"]) or "application/octet-stream",
                content=_to_bytes(row["content"]),
            )
            for row in rows
        )

    def _execute(self, sql: str, binds: dict[str, object]) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, binds)
            columns = [str(column[0]).lower() for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def sync_oracle_inbox(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    limit: int | None = None,
    client: OracleInboxReader | None = None,
) -> OracleSyncResult:
    """Copy unread Oracle inbox rows into the local auditable SQLite store."""
    effective_limit = limit or settings.oracle.fetch_limit
    db.insert_trace(
        conn,
        email_id=None,
        task_name="sync_oracle_inbox",
        step_name="start",
        status="ok",
        detail={"limit": effective_limit},
    )
    if client is None:
        with OracleInboxClient(settings.oracle) as oracle_client:
            return _sync_with_client(conn, settings, oracle_client, effective_limit)
    return _sync_with_client(conn, settings, client, effective_limit)


def validate_oracle_settings(oracle: OracleInboxSettings) -> None:
    missing = [
        name
        for name, value in {
            "host": oracle.host,
            "service_name": oracle.service_name,
            "username": oracle.username,
            "password": oracle.password,
            "email_table": oracle.email_table,
        }.items()
        if not value
    ]
    if missing:
        raise OracleConfigError(
            "Oracle inbox configuration is missing: " + ", ".join(sorted(missing))
        )
    if oracle.port <= 0:
        raise OracleConfigError("Oracle port must be a positive integer")
    _identifier_path(oracle.email_table)
    for value in vars(oracle.email_columns).values():
        _identifier_path(value)
    if oracle.attachment_table:
        _identifier_path(oracle.attachment_table)
        for value in vars(oracle.attachment_columns).values():
            _identifier_path(value)


def _sync_with_client(
    conn: sqlite3.Connection,
    settings: Settings,
    client: OracleInboxReader,
    limit: int,
) -> OracleSyncResult:
    inserted_ids: list[int] = []
    attachments_inserted = 0
    skipped = 0
    records = tuple(client.fetch_emails(limit=limit))
    for record in records:
        existing_id = db.get_email_id_by_message_id(conn, record.message_id)
        if existing_id is not None:
            skipped += 1
            continue

        email_id = db.insert_email(
            conn,
            message_id=record.message_id,
            sender=record.sender,
            received_at=record.received_at,
            subject=record.subject,
            body=record.body,
        )
        inserted_ids.append(email_id)
        attachments_inserted += _copy_attachments(conn, settings, client, email_id, record)
        db.insert_trace(
            conn,
            email_id=email_id,
            task_name="sync_oracle_inbox",
            step_name="insert_email",
            status="ok",
            detail={"message_id": record.message_id, "attachments_inserted": attachments_inserted},
        )

    result = OracleSyncResult(
        fetched=len(records),
        inserted=len(inserted_ids),
        skipped=skipped,
        attachments_inserted=attachments_inserted,
        inserted_email_ids=tuple(inserted_ids),
    )
    db.insert_trace(
        conn,
        email_id=None,
        task_name="sync_oracle_inbox",
        step_name="complete",
        status="ok",
        detail={
            "fetched": result.fetched,
            "inserted": result.inserted,
            "skipped": result.skipped,
            "attachments_inserted": result.attachments_inserted,
        },
    )
    return result


def _copy_attachments(
    conn: sqlite3.Connection,
    settings: Settings,
    client: OracleInboxReader,
    email_id: int,
    record: OracleEmailRecord,
) -> int:
    count = 0
    email_dir = settings.attachments_dir / str(email_id)
    for attachment in client.fetch_attachments(message_id=record.message_id):
        email_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(attachment.filename, fallback=f"attachment-{count + 1}.bin")
        path = _deduplicated_path(email_dir / filename)
        path.write_bytes(attachment.content)
        db.insert_attachment(
            conn,
            email_id=email_id,
            filename=path.name,
            mime_type=attachment.mime_type,
            storage_path=str(path),
        )
        count += 1
    return count


def _connect_oracle(oracle: OracleInboxSettings) -> Any:
    import oracledb

    dsn = oracledb.makedsn(oracle.host, oracle.port, service_name=oracle.service_name)
    return oracledb.connect(user=oracle.username, password=oracle.password, dsn=dsn)


def _identifier_path(value: str) -> str:
    parts = [part.strip() for part in str(value).split(".")]
    if not parts or any(not IDENTIFIER_PATTERN.fullmatch(part) for part in parts):
        raise OracleConfigError(f"Unsafe Oracle identifier: {value!r}")
    return ".".join(parts)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _to_iso_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _to_text(value)


def _to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _safe_filename(value: str, *, fallback: str) -> str:
    name = Path(value or fallback).name
    return name or fallback


def _deduplicated_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1
