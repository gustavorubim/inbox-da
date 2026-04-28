"""Pydantic models for structured extraction and verification."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Evidence(BaseModel):
    source: str
    quote: str
    attachment_id: int | None = None
    filename: str | None = None


class LineItem(BaseModel):
    description: str
    amount: float
    currency: str = "USD"


class StructuredExtraction(BaseModel):
    model_config = ConfigDict(validate_default=True)

    document_type: str
    counterparty: str
    account_or_customer: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    currency: str = "USD"
    amount: float
    due_date: str | None = None
    reference_number: str | None = None
    financial_category: str
    line_items: list[LineItem] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    verification_status: Literal["candidate", "verified", "needs_review"] = "candidate"

    @field_validator("evidence")
    @classmethod
    def require_evidence(cls, value: list[Evidence]) -> list[Evidence]:
        if not value:
            raise ValueError("At least one evidence item is required")
        return value


class VerificationResult(BaseModel):
    status: Literal["verified", "needs_review"]
    reason: str
    conflicts: list[str] = Field(default_factory=list)


def extractions_match(left: StructuredExtraction, right: StructuredExtraction) -> bool:
    return (
        left.document_type == right.document_type
        and left.counterparty == right.counterparty
        and left.currency == right.currency
        and abs(left.amount - right.amount) < 0.01
        and left.reference_number == right.reference_number
    )
