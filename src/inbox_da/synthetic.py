"""Synthetic financial inbox data generator."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from inbox_da import db


@dataclass(frozen=True)
class SyntheticAttachment:
    filename: str
    mime_type: str
    path: Path


EMAIL_TYPES = [
    ("invoice", "Invoice INV-{n:04d} from Northstar Supplies", "accounts@northstar.example"),
    ("bank_notice", "Bank notice for operating account {n:04d}", "alerts@metrobank.example"),
    ("payment_reminder", "Payment reminder REM-{n:04d}", "billing@atlaslogistics.example"),
    ("remittance", "Remittance advice RA-{n:04d}", "ap@solstice.example"),
    ("treasury", "Treasury cash update {n:04d}", "treasury@internal.example"),
    ("receipt", "Corporate card receipt RCPT-{n:04d}", "receipts@travel.example"),
    ("statement", "Monthly statement STMT-{n:04d}", "statements@custodian.example"),
]


def seed_database(conn, *, count: int, attachments_dir: Path) -> list[int]:
    attachments_dir.mkdir(parents=True, exist_ok=True)
    email_ids: list[int] = []
    start_index = _next_synthetic_index(conn)
    for index in range(start_index, start_index + count):
        email_type, subject_template, sender = EMAIL_TYPES[(index - 1) % len(EMAIL_TYPES)]
        amount = 1000 + index * 37.25
        reference = f"{email_type.upper()}-{index:04d}"
        body = _email_body(email_type, amount, reference)
        email_id = db.insert_email(
            conn,
            message_id=f"<synthetic-{index}@inbox-da.local>",
            sender=sender,
            received_at=(datetime.now(UTC) - timedelta(minutes=index)).isoformat(),
            subject=subject_template.format(n=index),
            body=body,
        )
        email_dir = attachments_dir / str(email_id)
        email_dir.mkdir(parents=True, exist_ok=True)
        for attachment in _create_attachments(email_dir, index, amount, reference):
            db.insert_attachment(
                conn,
                email_id=email_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                storage_path=str(attachment.path),
            )
        email_ids.append(email_id)
    return email_ids


def _next_synthetic_index(conn) -> int:
    row = conn.execute(
        """
        SELECT message_id
        FROM emails
        WHERE message_id LIKE '<synthetic-%@inbox-da.local>'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return 1
    value = str(row["message_id"])
    prefix = "<synthetic-"
    suffix = "@inbox-da.local>"
    if not value.startswith(prefix) or not value.endswith(suffix):
        return 1
    return int(value.removeprefix(prefix).removesuffix(suffix)) + 1


def _email_body(email_type: str, amount: float, reference: str) -> str:
    return (
        f"Please review {reference}. The {email_type.replace('_', ' ')} total is "
        f"USD {amount:,.2f}. Counterparty: Northstar Supplies. Due date: 2026-05-15. "
        "Account: Operations-4182."
    )


def _create_attachments(
    email_dir: Path, index: int, amount: float, reference: str
) -> list[SyntheticAttachment]:
    creators = [_create_txt, _create_csv, _create_xlsx, _create_pdf, _create_png]
    selected = creators[: 1 + (index % len(creators))]
    return [creator(email_dir, index, amount, reference) for creator in selected]


def _create_txt(email_dir: Path, index: int, amount: float, reference: str) -> SyntheticAttachment:
    path = email_dir / f"memo-{index}.txt"
    path.write_text(f"{reference}\nAmount: USD {amount:.2f}\nCounterparty: Northstar Supplies\n")
    return SyntheticAttachment(path.name, "text/plain", path)


def _create_csv(email_dir: Path, index: int, amount: float, reference: str) -> SyntheticAttachment:
    path = email_dir / f"line-items-{index}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["reference", "description", "amount", "currency"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "reference": reference,
                "description": "Financial services",
                "amount": f"{amount:.2f}",
                "currency": "USD",
            }
        )
    return SyntheticAttachment(path.name, "text/csv", path)


def _create_xlsx(email_dir: Path, index: int, amount: float, reference: str) -> SyntheticAttachment:
    path = email_dir / f"schedule-{index}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["reference", "period", "amount", "currency"])
    sheet.append([reference, "2026-04", amount, "USD"])
    workbook.save(path)
    return SyntheticAttachment(
        path.name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", path
    )


def _create_pdf(email_dir: Path, index: int, amount: float, reference: str) -> SyntheticAttachment:
    path = email_dir / f"invoice-{index}.pdf"
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawString(72, 720, f"Reference: {reference}")
    pdf.drawString(72, 700, "Counterparty: Northstar Supplies")
    pdf.drawString(72, 680, f"Amount Due: USD {amount:.2f}")
    pdf.drawString(72, 660, "Due Date: 2026-05-15")
    pdf.save()
    return SyntheticAttachment(path.name, "application/pdf", path)


def _create_png(email_dir: Path, index: int, amount: float, reference: str) -> SyntheticAttachment:
    path = email_dir / f"receipt-{index}.png"
    image = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 880, 400), outline="black", width=3)
    draw.text((60, 80), "Financial Receipt", fill="black")
    draw.text((60, 140), f"Reference: {reference}", fill="black")
    draw.text((60, 200), f"Amount: USD {amount:.2f}", fill="black")
    draw.text((60, 260), "Counterparty: Northstar Supplies", fill="black")
    image.save(path)
    return SyntheticAttachment(path.name, "image/png", path)
