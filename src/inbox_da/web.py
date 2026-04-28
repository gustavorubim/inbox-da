"""FastAPI monitoring console for inbox-da."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from inbox_da import db
from inbox_da.agent import process_email
from inbox_da.config import Settings, load_settings

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(
    settings: Settings | None = None,
    *,
    process_offline_demo: bool = True,
) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with closing(db.connect(resolved_settings.database_path)) as conn:
            db.init_db(conn)
        yield

    app = FastAPI(title="inbox-da monitor", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.process_offline_demo = process_offline_demo
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request) -> HTMLResponse:
        with closing(db.connect(app.state.settings.database_path)) as conn:
            rows = db.rows_to_dicts(db.list_email_dashboard_rows(conn))
            summary = _dashboard_summary(conn, rows)
        return templates.TemplateResponse(
            request,
            "inbox.html",
            {"rows": rows, "summary": summary, "settings": app.state.settings},
        )

    @app.get("/emails/{email_id}", response_class=HTMLResponse)
    def email_detail(request: Request, email_id: int) -> HTMLResponse:
        notice = request.query_params.get("notice", "")
        with closing(db.connect(app.state.settings.database_path)) as conn:
            try:
                email = dict(db.get_email(conn, email_id))
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            attachments = [
                _decode_json_columns(row)
                for row in db.rows_to_dicts(db.list_attachments(conn, email_id))
            ]
            extraction = _decode_json_columns(
                dict(db.latest_structured_extraction(conn, email_id) or {})
            )
            cost = dict(db.latest_email_cost(conn, email_id) or {})
            trace_rows = db.rows_to_dicts(db.list_email_extraction_traces(conn, email_id, limit=25))
            reviews = [
                _decode_json_columns(row)
                for row in db.rows_to_dicts(db.list_review_actions(conn, email_id))
            ]
            candidate_comparison = _candidate_comparison(
                db.rows_to_dicts(db.list_latest_extraction_runs(conn, email_id)),
                extraction,
            )
        return templates.TemplateResponse(
            request,
            "email_detail.html",
            {
                "email": email,
                "attachments": attachments,
                "extraction": extraction,
                "cost": cost,
                "trace_rows": list(reversed(trace_rows)),
                "reviews": reviews,
                "candidate_comparison": candidate_comparison,
                "notice": _notice_text(notice),
                "process_offline_demo": app.state.process_offline_demo,
            },
        )

    @app.post("/emails/{email_id}/process")
    def process_email_route(email_id: int) -> RedirectResponse:
        _process_email_from_web(
            email_id,
            settings=app.state.settings,
            require_credentials=not app.state.process_offline_demo,
        )
        return RedirectResponse(f"/emails/{email_id}", status_code=303)

    @app.post("/emails/{email_id}/process-live")
    def process_email_live_route(email_id: int) -> RedirectResponse:
        _process_email_from_web(
            email_id,
            settings=app.state.settings,
            require_credentials=True,
        )
        return RedirectResponse(f"/emails/{email_id}", status_code=303)

    @app.post("/emails/{email_id}/process-offline")
    def process_email_offline_route(email_id: int) -> RedirectResponse:
        _process_email_from_web(
            email_id,
            settings=app.state.settings,
            require_credentials=False,
        )
        return RedirectResponse(f"/emails/{email_id}", status_code=303)

    @app.post("/review/{email_id}/approve")
    def approve_email(
        email_id: int,
        reviewer: str = Form(default="operator"),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        with closing(db.connect(app.state.settings.database_path)) as conn:
            extraction = db.latest_structured_extraction(conn, email_id)
            if extraction is None:
                raise HTTPException(
                    status_code=400, detail="No extraction is available to approve."
                )
            reviewed_payload = _reviewed_payload_from_row(extraction)
            reviewed_extraction_id = db.insert_structured_extraction(
                conn, email_id=email_id, extraction=reviewed_payload
            )
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=reviewed_extraction_id,
                action="approve",
                reviewer=reviewer,
                note=note,
            )
        return _email_redirect(email_id, "approved")

    @app.post("/review/{email_id}/select-candidate")
    def select_candidate(
        email_id: int,
        run_id: int = Form(...),
        reviewer: str = Form(default="operator"),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        with closing(db.connect(app.state.settings.database_path)) as conn:
            extraction = db.latest_structured_extraction(conn, email_id)
            run = db.get_extraction_run(conn, run_id)
            if int(run["email_id"]) != email_id:
                raise HTTPException(
                    status_code=400,
                    detail="Selected extraction run does not belong to this email.",
                )
            payload = json.loads(run["normalized_output_json"])
            reviewed_payload = _reviewed_payload_from_candidate(payload, extraction)
            reviewed_extraction_id = db.insert_structured_extraction(
                conn, email_id=email_id, extraction=reviewed_payload
            )
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=reviewed_extraction_id,
                selected_run_id=run_id,
                action="select_candidate",
                reviewer=reviewer,
                override_payload={
                    "selected_run_type": run["run_type"],
                    "selected_candidate": payload,
                },
                note=note,
            )
        return _email_redirect(email_id, "candidate_applied")

    @app.post("/review/{email_id}/select-fields")
    async def select_fields(
        request: Request,
        email_id: int,
        reviewer: str = Form(default="operator"),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        form = await request.form()
        selected_fields = {
            key.removeprefix("field_"): value
            for key, value in form.items()
            if key.startswith("field_") and value
        }
        with closing(db.connect(app.state.settings.database_path)) as conn:
            extraction = db.latest_structured_extraction(conn, email_id)
            if extraction is None:
                raise HTTPException(
                    status_code=400,
                    detail="No extraction is available for field selection.",
                )
            runs = db.rows_to_dicts(db.list_latest_extraction_runs(conn, email_id))
            comparison = _candidate_comparison(runs, dict(extraction or {}))
            override_payload = _field_selection_payload(comparison, selected_fields)
            selected = override_payload["selected_fields"]
            if selected:
                reviewed_payload = _reviewed_payload_from_row(extraction, updates=selected)
                reviewed_extraction_id = db.insert_structured_extraction(
                    conn, email_id=email_id, extraction=reviewed_payload
                )
            else:
                reviewed_extraction_id = int(extraction["id"])
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=reviewed_extraction_id,
                action="select_fields",
                reviewer=reviewer,
                override_payload=override_payload,
                note=note,
            )
        notice = "field_choices_applied" if selected else "no_field_choices"
        return _email_redirect(email_id, notice)

    @app.post("/review/{email_id}/override")
    def override_email(
        email_id: int,
        reviewer: str = Form(default="operator"),
        document_type: str = Form(default=""),
        counterparty: str = Form(default=""),
        amount: str = Form(default=""),
        currency: str = Form(default=""),
        reference_number: str = Form(default=""),
        due_date: str = Form(default=""),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        override_payload = {
            "document_type": document_type,
            "counterparty": counterparty,
            "amount": amount,
            "currency": currency,
            "reference_number": reference_number,
            "due_date": due_date,
        }
        override_payload = {key: value for key, value in override_payload.items() if value}
        with closing(db.connect(app.state.settings.database_path)) as conn:
            extraction = db.latest_structured_extraction(conn, email_id)
            if extraction is None:
                raise HTTPException(
                    status_code=400, detail="No extraction is available to override."
                )
            reviewed_extraction_id = int(extraction["id"])
            if override_payload:
                reviewed_payload = _reviewed_payload_from_row(extraction, updates=override_payload)
                reviewed_extraction_id = db.insert_structured_extraction(
                    conn, email_id=email_id, extraction=reviewed_payload
                )
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=reviewed_extraction_id,
                action="override",
                reviewer=reviewer,
                override_payload=override_payload,
                note=note,
            )
        notice = "override_applied" if override_payload else "no_override_fields"
        return _email_redirect(email_id, notice)

    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request) -> HTMLResponse:
        with closing(db.connect(app.state.settings.database_path)) as conn:
            rows = db.rows_to_dicts(db.list_review_queue(conn))
        return templates.TemplateResponse(request, "review.html", {"rows": rows})

    @app.get("/reports/costs", response_class=HTMLResponse)
    def cost_report(request: Request) -> HTMLResponse:
        with closing(db.connect(app.state.settings.database_path)) as conn:
            rows = db.rows_to_dicts(db.list_latest_email_costs(conn))
            summary = db.summarize_latest_email_costs(conn)
        return templates.TemplateResponse(
            request,
            "costs.html",
            {"rows": rows, "summary": summary},
        )

    return app


def _process_email_from_web(
    email_id: int,
    *,
    settings: Settings,
    require_credentials: bool,
) -> None:
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
        process_email(
            conn,
            email_id=email_id,
            settings=settings,
            require_credentials=require_credentials,
        )


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in (
        "line_items_json",
        "evidence_json",
        "parsed_payload_json",
        "vision_summary_json",
        "override_payload_json",
        "detail_json",
    ):
        if key in decoded and isinstance(decoded[key], str):
            try:
                decoded[key.removesuffix("_json")] = json.loads(decoded[key])
            except json.JSONDecodeError:
                decoded[key.removesuffix("_json")] = decoded[key]
    return decoded


NOTICE_MESSAGES = {
    "approved": "Review approved and applied to the final structured extraction.",
    "candidate_applied": "Candidate selection applied to the final structured extraction.",
    "field_choices_applied": "Field choices applied to the final structured extraction.",
    "no_field_choices": "No field choices were selected, so the final extraction was not changed.",
    "override_applied": "Manual override applied to the final structured extraction.",
    "no_override_fields": (
        "No override fields were entered, so the final extraction was not changed."
    ),
}


REVIEW_FIELDS = (
    "document_type",
    "counterparty",
    "account_or_customer",
    "period_start",
    "period_end",
    "currency",
    "amount",
    "due_date",
    "reference_number",
    "financial_category",
)


def _email_redirect(email_id: int, notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/emails/{email_id}?notice={notice}#structured-extraction",
        status_code=303,
    )


def _notice_text(notice: str) -> str:
    return NOTICE_MESSAGES.get(notice, "")


def _candidate_comparison(runs: list[dict[str, Any]], extraction: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for run in runs:
        normalized = _json_loads(run.get("normalized_output_json"), {})
        candidates.append(
            {
                "id": run["id"],
                "run_type": run["run_type"],
                "status": run["status"],
                "payload": normalized,
            }
        )
    candidates_by_type = {candidate["run_type"]: candidate for candidate in candidates}
    left = candidates_by_type.get("extractor_a", {}).get("payload", {})
    right = candidates_by_type.get("extractor_b", {}).get("payload", {})
    fields = []
    for field in REVIEW_FIELDS:
        left_value = left.get(field)
        right_value = right.get(field)
        final_value = extraction.get(field)
        fields.append(
            {
                "name": field,
                "left": left_value,
                "right": right_value,
                "final": final_value,
                "disagrees": _display_value(left_value) != _display_value(right_value),
            }
        )
    return {
        "candidates": candidates,
        "fields": fields,
        "has_candidates": bool(candidates),
        "has_disagreements": any(field["disagrees"] for field in fields),
    }


def _field_selection_payload(
    comparison: dict[str, Any], selected_fields: dict[str, Any]
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for field in comparison["fields"]:
        source = selected_fields.get(field["name"])
        if source in {"left", "right", "final"}:
            selected[field["name"]] = field[source]
    return {"selected_fields": selected, "field_sources": selected_fields}


def _reviewed_payload_from_row(
    extraction: Any,
    *,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decoded = _decode_json_columns(dict(extraction))
    payload = {
        "document_type": decoded["document_type"],
        "counterparty": decoded["counterparty"],
        "account_or_customer": decoded.get("account_or_customer"),
        "period_start": decoded.get("period_start"),
        "period_end": decoded.get("period_end"),
        "currency": decoded["currency"],
        "amount": _coerce_amount(decoded["amount"]),
        "due_date": decoded.get("due_date"),
        "reference_number": decoded.get("reference_number"),
        "financial_category": decoded["financial_category"],
        "line_items": decoded.get("line_items", []),
        "evidence": decoded.get("evidence", []),
        "confidence": _coerce_confidence(decoded.get("confidence")),
        "verification_status": "verified",
    }
    for field, value in (updates or {}).items():
        if field in REVIEW_FIELDS and value not in {None, ""}:
            payload[field] = _coerce_review_value(field, value)
    return _ensure_reviewed_payload(payload)


def _reviewed_payload_from_candidate(
    candidate: dict[str, Any],
    fallback_extraction: Any | None,
) -> dict[str, Any]:
    payload = (
        _reviewed_payload_from_row(fallback_extraction) if fallback_extraction is not None else {}
    )
    for field in REVIEW_FIELDS:
        value = candidate.get(field)
        if value not in {None, ""}:
            payload[field] = _coerce_review_value(field, value)
    for field in ("line_items", "evidence"):
        if candidate.get(field):
            payload[field] = candidate[field]
    payload["confidence"] = _coerce_confidence(candidate.get("confidence"))
    payload["verification_status"] = "verified"
    return _ensure_reviewed_payload(payload)


def _ensure_reviewed_payload(payload: dict[str, Any]) -> dict[str, Any]:
    missing = [
        field
        for field in (
            "document_type",
            "counterparty",
            "currency",
            "amount",
            "financial_category",
        )
        if payload.get(field) in {None, ""}
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Reviewed extraction is missing required fields: {', '.join(missing)}",
        )
    if not payload.get("evidence"):
        payload["evidence"] = [
            {
                "source": "review_action",
                "quote": "Reviewer selected this final extraction from candidate outputs.",
            }
        ]
    payload["amount"] = _coerce_amount(payload["amount"])
    payload["confidence"] = _coerce_confidence(payload.get("confidence"))
    payload["verification_status"] = "verified"
    return payload


def _coerce_review_value(field: str, value: Any) -> Any:
    if field == "amount":
        return _coerce_amount(value)
    return value


def _coerce_amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Amount must be numeric; got {value!r}.",
        ) from exc


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.7
    return max(0.0, min(1.0, confidence))


def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if value is not None else default


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return "" if value is None else str(value)


def _dashboard_summary(conn, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cost_summary = db.summarize_latest_email_costs(conn)
    return {
        "emails": len(rows),
        "new": sum(1 for row in rows if row["status"] == "new"),
        "processed": sum(1 for row in rows if row["status"] == "processed"),
        "needs_review": sum(
            1
            for row in rows
            if row.get("verification_status") == "needs_review"
            or row.get("review_action") == "override"
        ),
        "total_cost": cost_summary.get("total_cost", 0.0),
        "currency": cost_summary.get("currency", "USD"),
    }


def create_app_from_env() -> FastAPI:
    config_path = os.getenv("INBOX_DA_CONFIG", "configs/default.yaml")
    offline_demo = os.getenv("INBOX_DA_WEB_OFFLINE_DEMO", "1") != "0"
    return create_app(load_settings(Path(config_path)), process_offline_demo=offline_demo)


app = create_app_from_env()
