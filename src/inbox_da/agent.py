"""DeepAgent extraction workflow with verification and tracing."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from ast import literal_eval
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from inbox_da import db
from inbox_da.attachments import (
    GeminiVisionParser,
    OfflineVisionParser,
    VisionParser,
    parse_attachment,
)
from inbox_da.config import Settings
from inbox_da.costing import TokenUsage, collect_token_usage, estimate_cost
from inbox_da.logging_utils import console
from inbox_da.model_provider import (
    agent_runtime_summary,
    build_deep_agent,
    build_gemini_model,
    validate_model_config,
)
from inbox_da.models import StructuredExtraction, VerificationResult, extractions_match

logger = logging.getLogger(__name__)


class Extractor(Protocol):
    def extract(self, context: dict[str, Any], *, pass_name: str) -> dict[str, Any] | AgentRun: ...


class Verifier(Protocol):
    def verify(
        self,
        context: dict[str, Any],
        left: StructuredExtraction,
        right: StructuredExtraction,
    ) -> VerificationResult | VerificationRun: ...


@dataclass(frozen=True)
class AgentRun:
    payload: dict[str, Any]
    usage: TokenUsage = field(default_factory=TokenUsage)
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationRun:
    result: VerificationResult
    usage: TokenUsage = field(default_factory=TokenUsage)
    trace: dict[str, Any] = field(default_factory=dict)


class EmailTrace:
    def __init__(self, conn: sqlite3.Connection, email_id: int) -> None:
        self.conn = conn
        self.email_id = email_id

    def event(
        self,
        *,
        actor: str,
        event_name: str,
        status: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        db.insert_email_extraction_trace(
            self.conn,
            email_id=self.email_id,
            actor=actor,
            event_name=event_name,
            status=status,
            message=message,
            detail=detail,
        )
        style = (
            "green"
            if status in {"ok", "verified", "processed"}
            else "yellow"
            if status == "needs_review"
            else "red"
        )
        console.log(
            f"[bold cyan]email {self.email_id}[/] "
            f"[bold]{actor}.{event_name}[/] [{style}]{status}[/] {message}"
        )


class DeepAgentExtractor:
    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        self.settings = settings
        self.model = model or build_gemini_model(settings)

    def extract(self, context: dict[str, Any], *, pass_name: str) -> dict[str, Any]:
        agent = build_deep_agent(
            model=self.model,
            tools=[],
            system_prompt=(
                "You are the supervisor for one structured financial extraction pass. "
                "Use the attachment-reader and financial-field-extractor subagents when "
                "they are available. Return only JSON matching the required schema, "
                "including evidence."
            ),
            settings=self.settings,
        )
        response = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Extraction pass {pass_name}. Extract structured data from:\n"
                            f"{json.dumps(context, default=str)}"
                        ),
                    }
                ]
            }
        )
        return AgentRun(
            payload=_coerce_agent_json(response),
            usage=collect_token_usage(response),
            trace={"pass_name": pass_name, "response_type": type(response).__name__},
        )


class DeepAgentVerifier:
    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        self.settings = settings
        self.model = model or build_gemini_model(settings)

    def verify(
        self,
        context: dict[str, Any],
        left: StructuredExtraction,
        right: StructuredExtraction,
    ) -> VerificationResult:
        agent = build_deep_agent(
            model=self.model,
            tools=[],
            system_prompt=(
                "You are the supervisor for verification. Use the evidence-verifier "
                "subagent and review-agent when they are available. Return JSON with "
                "status, reason, and conflicts."
            ),
            settings=self.settings,
        )
        response = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "context": context,
                                "left": left.model_dump(),
                                "right": right.model_dump(),
                            },
                            default=str,
                        ),
                    }
                ]
            }
        )
        data = _coerce_agent_json(response)
        return VerificationRun(
            result=VerificationResult.model_validate(normalize_verification_payload(data)),
            usage=collect_token_usage(response),
            trace={"response_type": type(response).__name__},
        )


class HeuristicExtractor:
    """Offline fallback used for demos when no API key is configured."""

    def extract(self, context: dict[str, Any], *, pass_name: str) -> dict[str, Any]:
        text = (
            context["email"]["body"]
            + "\n"
            + "\n".join(
                attachment.get("extracted_text") or "" for attachment in context["attachments"]
            )
        )
        amount_match = re.search(r"USD\s+([0-9,]+\.\d{2})", text)
        reference_match = re.search(r"([A-Z_]+-\d{4}|INV-\d{4}|REM-\d{4}|RA-\d{4})", text)
        amount = float(amount_match.group(1).replace(",", "")) if amount_match else 0.0
        reference = reference_match.group(1) if reference_match else None
        return {
            "document_type": "financial_notice",
            "counterparty": "Northstar Supplies",
            "account_or_customer": "Operations-4182",
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "currency": "USD",
            "amount": amount,
            "due_date": "2026-05-15",
            "reference_number": reference,
            "financial_category": "accounts_payable",
            "line_items": [
                {"description": "Financial services", "amount": amount, "currency": "USD"}
            ],
            "evidence": [
                {
                    "source": "email.body",
                    "quote": context["email"]["body"][:180],
                    "attachment_id": None,
                    "filename": None,
                }
            ],
            "confidence": 0.86 if pass_name == "A" else 0.85,
            "verification_status": "candidate",
        }


class RuleVerifier:
    def verify(
        self,
        context: dict[str, Any],
        left: StructuredExtraction,
        right: StructuredExtraction,
    ) -> VerificationResult:
        del context
        if extractions_match(left, right) and left.evidence and right.evidence:
            return VerificationResult(status="verified", reason="Candidates agree with evidence.")
        return VerificationResult(
            status="needs_review",
            reason="Extractor candidates disagree or evidence is incomplete.",
            conflicts=["candidate_mismatch"],
        )


def process_email(
    conn: sqlite3.Connection,
    *,
    email_id: int,
    settings: Settings,
    extractor: Extractor | None = None,
    verifier: Verifier | None = None,
    vision_parser: VisionParser | None = None,
    require_credentials: bool = True,
) -> int:
    validate_model_config(settings, require_credentials=require_credentials)
    email = dict(db.get_email(conn, email_id))
    trace = EmailTrace(conn, email_id)
    if require_credentials:
        model = build_gemini_model(settings)
        extractor = extractor or DeepAgentExtractor(settings, model)
        verifier = verifier or DeepAgentVerifier(settings, model)
        vision_parser = vision_parser or GeminiVisionParser(model)
    else:
        extractor = extractor or HeuristicExtractor()
        verifier = verifier or RuleVerifier()
        vision_parser = vision_parser or OfflineVisionParser()

    db.insert_trace(
        conn,
        email_id=email_id,
        task_name="process_email",
        step_name="start",
        status="ok",
        detail=agent_runtime_summary(settings),
    )
    trace.event(
        actor="pipeline",
        event_name="start",
        status="ok",
        message="Starting email extraction workflow.",
        detail=agent_runtime_summary(settings),
    )
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Processing email {email_id}", total=8)
        trace.event(
            actor="pipeline",
            event_name="load_email",
            status="ok",
            message=f"Loaded email subject: {email['subject']}",
            detail={"sender": email["sender"], "status": email["status"]},
        )
        progress.advance(task)
        attachments, attachment_usage = _parse_attachments(
            conn, email_id, settings, vision_parser, trace
        )
        progress.advance(task)
        context = {"email": email, "attachments": attachments}
        trace.event(
            actor="extractors",
            event_name="start_parallel",
            status="ok",
            message="Launching extractor passes A and B in parallel.",
            detail={"attachment_count": len(attachments)},
        )
        left_run, right_run = _run_extractors(extractor, context)
        progress.advance(task)
        left_raw = left_run.payload
        right_raw = right_run.payload
        trace.event(
            actor="extractor_a",
            event_name="complete",
            status="ok",
            message="Extractor pass A returned a candidate payload.",
            detail={"usage": left_run.usage.as_dict(), **left_run.trace},
        )
        trace.event(
            actor="extractor_b",
            event_name="complete",
            status="ok",
            message="Extractor pass B returned a candidate payload.",
            detail={"usage": right_run.usage.as_dict(), **right_run.trace},
        )
        progress.advance(task)
        left_normalized = normalize_extraction_payload(left_raw)
        right_normalized = normalize_extraction_payload(right_raw)
        left = StructuredExtraction.model_validate(left_normalized)
        right = StructuredExtraction.model_validate(right_normalized)
        trace.event(
            actor="normalizer",
            event_name="complete",
            status="ok",
            message="Normalized both extractor candidates into the target schema.",
            detail={
                "candidate_a": {
                    "amount": left.amount,
                    "currency": left.currency,
                    "reference_number": left.reference_number,
                },
                "candidate_b": {
                    "amount": right.amount,
                    "currency": right.currency,
                    "reference_number": right.reference_number,
                },
            },
        )
        _record_runs(conn, email_id, settings.model_name, left_raw, right_raw, left, right)
        progress.advance(task)
        verification_run = _as_verification_run(verifier.verify(context, left, right))
        verification = verification_run.result
        trace.event(
            actor="verifier",
            event_name="complete",
            status=verification.status,
            message=verification.reason,
            detail={
                "conflicts": verification.conflicts,
                "usage": verification_run.usage.as_dict(),
                **verification_run.trace,
            },
        )
        progress.advance(task)
        final = _choose_final(left, right, verification)
        extraction_id = db.insert_structured_extraction(
            conn, email_id=email_id, extraction=final.model_dump()
        )
        trace.event(
            actor="database",
            event_name="write_extraction",
            status=final.verification_status,
            message=f"Wrote structured extraction {extraction_id}.",
            detail={
                "extraction_id": extraction_id,
                "amount": final.amount,
                "currency": final.currency,
                "counterparty": final.counterparty,
            },
        )
        progress.advance(task)
        total_usage = attachment_usage + left_run.usage + right_run.usage + verification_run.usage
        cost = estimate_cost(
            total_usage,
            input_price_per_1m=settings.cost_input_per_1m,
            output_price_per_1m=settings.cost_output_per_1m,
            currency=settings.cost_currency,
            pricing_source=settings.cost_pricing_source,
        )
        cost_id = db.insert_email_processing_cost(
            conn,
            email_id=email_id,
            model=settings.model_name,
            usage=total_usage.as_dict(),
            cost=cost.as_dict(),
            detail={
                "pricing_note": "Estimated from available model response token metadata.",
                "attachment_usage": attachment_usage.as_dict(),
                "extractor_a_usage": left_run.usage.as_dict(),
                "extractor_b_usage": right_run.usage.as_dict(),
                "verifier_usage": verification_run.usage.as_dict(),
            },
        )
        trace.event(
            actor="cost",
            event_name="estimate",
            status="ok",
            message=(
                f"Estimated processing cost: {cost.total_cost:.6f} {cost.currency} "
                f"across {total_usage.model_calls} model call(s)."
            ),
            detail={"cost_id": cost_id, "usage": total_usage.as_dict(), **cost.as_dict()},
        )
        db.mark_email_status(conn, email_id, "processed")
        db.insert_trace(
            conn,
            email_id=email_id,
            task_name="process_email",
            step_name="complete",
            status=verification.status,
            detail={"extraction_id": extraction_id, "reason": verification.reason},
        )
        progress.advance(task)
        return extraction_id


def _parse_attachments(
    conn: sqlite3.Connection,
    email_id: int,
    settings: Settings,
    vision_parser: VisionParser | None,
    trace: EmailTrace,
) -> tuple[list[dict[str, Any]], TokenUsage]:
    parsed: list[dict[str, Any]] = []
    usage = TokenUsage()
    for row in db.list_attachments(conn, email_id):
        trace.event(
            actor="attachment_parser",
            event_name="start",
            status="ok",
            message=f"Parsing {row['filename']} ({row['mime_type']}).",
            detail={"attachment_id": row["id"]},
        )
        text, payload, vision = parse_attachment(
            Path(row["storage_path"]),
            mime_type=row["mime_type"],
            settings=settings,
            vision_parser=vision_parser,
        )
        if isinstance(vision, dict):
            usage += collect_token_usage(vision.get("token_usage", {}))
        db.update_attachment_parse(
            conn,
            attachment_id=int(row["id"]),
            extracted_text=text,
            parsed_payload=payload,
            vision_summary=vision,
        )
        item = dict(row)
        item.update(
            {
                "extracted_text": text,
                "parsed_payload_json": json.dumps(payload, sort_keys=True),
                "vision_summary_json": json.dumps(vision or {}, sort_keys=True),
            }
        )
        db.insert_trace(
            conn,
            email_id=email_id,
            task_name="process_email",
            step_name="parse_attachment",
            status="ok",
            detail={
                "attachment_id": row["id"],
                "filename": row["filename"],
                "kind": payload["kind"],
            },
        )
        trace.event(
            actor="attachment_parser",
            event_name="complete",
            status="ok",
            message=f"Parsed {row['filename']} as {payload['kind']}.",
            detail={
                "attachment_id": row["id"],
                "filename": row["filename"],
                "kind": payload["kind"],
                "text_chars": len(text),
                "vision": bool(vision),
            },
        )
        parsed.append(item)
    return parsed, usage


def _run_extractors(extractor: Extractor, context: dict[str, Any]) -> tuple[AgentRun, AgentRun]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(extractor.extract, context, pass_name="A")
        right = pool.submit(extractor.extract, context, pass_name="B")
        return _as_agent_run(left.result()), _as_agent_run(right.result())


def _as_agent_run(value: dict[str, Any] | AgentRun) -> AgentRun:
    if isinstance(value, AgentRun):
        return value
    return AgentRun(payload=value)


def _as_verification_run(value: VerificationResult | VerificationRun) -> VerificationRun:
    if isinstance(value, VerificationRun):
        return value
    return VerificationRun(result=value)


def _record_runs(
    conn: sqlite3.Connection,
    email_id: int,
    model_name: str,
    left_raw: dict[str, Any],
    right_raw: dict[str, Any],
    left: StructuredExtraction,
    right: StructuredExtraction,
) -> None:
    for label, raw, normalized in (
        ("extractor_a", left_raw, left),
        ("extractor_b", right_raw, right),
    ):
        db.insert_extraction_run(
            conn,
            email_id=email_id,
            run_type=label,
            model=model_name,
            raw_output=raw,
            normalized_output=normalized.model_dump(),
            status="ok",
        )


def _choose_final(
    left: StructuredExtraction, right: StructuredExtraction, verification: VerificationResult
) -> StructuredExtraction:
    chosen = left if left.confidence >= right.confidence else right
    status = (
        "verified"
        if verification.status == "verified" and extractions_match(left, right)
        else "needs_review"
    )
    return chosen.model_copy(update={"verification_status": status})


def normalize_extraction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = _collect_evidence(payload)
    line_items = [
        {
            "description": str(_unwrap_value(item.get("description", "Financial line item"))),
            "amount": _coerce_float(item.get("amount", payload.get("amount", 0.0))),
            "currency": str(_unwrap_value(item.get("currency", payload.get("currency", "USD")))),
        }
        for item in payload.get("line_items", [])
        if isinstance(item, dict)
    ]
    amount = _first_present(
        payload,
        "amount",
        "total_amount",
        "invoice_amount",
        "amount_due",
        "total",
    )
    if amount is None and line_items:
        amount = sum(item["amount"] for item in line_items)
    reference = _first_present(
        payload,
        "reference_number",
        "invoice_number",
        "document_number",
        "transaction_id",
    )
    normalized = {
        "document_type": _unwrap_value(
            payload.get("document_type")
            or payload.get("type")
            or ("invoice" if payload.get("invoice_number") else "financial_notice")
        ),
        "counterparty": _unwrap_value(
            payload.get("counterparty") or payload.get("vendor") or "Unknown"
        ),
        "account_or_customer": _unwrap_value(
            payload.get("account_or_customer") or payload.get("account") or payload.get("customer")
        ),
        "period_start": _unwrap_value(payload.get("period_start")),
        "period_end": _unwrap_value(payload.get("period_end")),
        "currency": _unwrap_value(payload.get("currency") or "USD"),
        "amount": _coerce_float(amount),
        "due_date": _unwrap_value(payload.get("due_date")),
        "reference_number": _unwrap_value(reference),
        "financial_category": _unwrap_value(
            payload.get("financial_category") or "accounts_payable"
        ),
        "line_items": line_items,
        "evidence": evidence
        or [{"source": "model_output", "quote": "Model returned structured financial fields."}],
        "confidence": _coerce_confidence(payload.get("confidence")),
        "verification_status": "candidate",
    }
    if not normalized["line_items"] and normalized["amount"]:
        normalized["line_items"] = [
            {
                "description": "Extracted financial amount",
                "amount": normalized["amount"],
                "currency": normalized["currency"],
            }
        ]
    return normalized


def normalize_verification_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(payload.get("status", "")).lower().strip()
    if raw_status in {"verified", "match", "matched", "pass", "passed", "ok", "agree"}:
        status = "verified"
    else:
        status = "needs_review"
    reason = payload.get("reason") or payload.get("summary") or payload.get("explanation")
    conflicts = payload.get("conflicts") or payload.get("issues") or []
    if isinstance(conflicts, str):
        conflicts = [conflicts]
    return {
        "status": status,
        "reason": str(reason or f"Verifier returned status: {raw_status or 'unknown'}"),
        "conflicts": [str(conflict) for conflict in conflicts],
    }


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _unwrap_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _coerce_float(value: Any, default: float = 0.0) -> float:
    unwrapped = _unwrap_value(value)
    if unwrapped is None:
        return default
    if isinstance(unwrapped, int | float):
        return float(unwrapped)
    text = str(unwrapped).strip()
    if not text:
        return default
    try:
        return float(text.replace(",", ""))
    except ValueError:
        match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text)
        if match:
            return float(match.group(0).replace(",", ""))
    return default


def _coerce_confidence(value: Any) -> float:
    unwrapped = _unwrap_value(value)
    if unwrapped is None:
        return 0.7
    if isinstance(unwrapped, str):
        label = unwrapped.strip().lower().replace("_", " ").replace("-", " ")
        label_scores = {
            "very high": 0.95,
            "high": 0.85,
            "medium": 0.7,
            "moderate": 0.7,
            "low": 0.4,
            "very low": 0.2,
            "unknown": 0.7,
        }
        if label in label_scores:
            return label_scores[label]
    score = _coerce_float(unwrapped, default=0.7)
    if 1.0 < score <= 100.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def _collect_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    raw_evidence = payload.get("evidence", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, dict):
                collected.append(
                    {
                        "source": str(item.get("source", "model_output")),
                        "quote": str(item.get("quote") or item.get("evidence") or item),
                        "attachment_id": item.get("attachment_id"),
                        "filename": item.get("filename"),
                    }
                )
    for key, value in payload.items():
        if isinstance(value, dict) and value.get("evidence"):
            collected.append(
                {
                    "source": str(value.get("source", key)),
                    "quote": str(value["evidence"]),
                    "attachment_id": value.get("attachment_id"),
                    "filename": value.get("filename"),
                }
            )
    if isinstance(payload.get("line_items"), list):
        for item in payload["line_items"]:
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if isinstance(value, dict) and value.get("evidence"):
                    collected.append(
                        {
                            "source": f"line_items.{key}",
                            "quote": str(value["evidence"]),
                            "attachment_id": value.get("attachment_id"),
                            "filename": value.get("filename"),
                        }
                    )
    return collected


def _coerce_agent_json(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        if "structured_response" in response:
            return dict(response["structured_response"])
        messages = response.get("messages")
        if messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                content = last_message.get("content", "")
            else:
                content = getattr(last_message, "content", "")
            return _json_from_text(_content_to_text(content))
        return response
    return _json_from_text(getattr(response, "content", str(response)))


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Model response did not contain JSON: {text[:200]}")
    candidate = stripped[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as err:
        parsed = literal_eval(candidate)
        if not isinstance(parsed, dict):
            raise ValueError(f"Model response JSON was not an object: {text[:200]}") from err
        return parsed
