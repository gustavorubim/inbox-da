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
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=int(extraction["id"]) if extraction else None,
                action="approve",
                reviewer=reviewer,
                note=note,
            )
        return RedirectResponse(f"/emails/{email_id}", status_code=303)

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
            db.insert_review_action(
                conn,
                email_id=email_id,
                extraction_id=int(extraction["id"]) if extraction else None,
                action="override",
                reviewer=reviewer,
                override_payload=override_payload,
                note=note,
            )
        return RedirectResponse(f"/emails/{email_id}", status_code=303)

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
