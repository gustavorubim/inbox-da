"""Command line interface for inbox-da."""

from __future__ import annotations

import csv
import os
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from inbox_da import db
from inbox_da.agent import process_email
from inbox_da.config import load_settings
from inbox_da.logging_utils import configure_logging, console, print_key_values, show_header
from inbox_da.model_provider import agent_runtime_summary, validate_model_config
from inbox_da.synthetic import seed_database

app = typer.Typer(help="DeepAgent financial inbox monitor.")


ConfigOpt = Annotated[Path, typer.Option("--config", help="Path to YAML config.")]

COST_REPORT_COLUMNS = [
    "email_id",
    "subject",
    "email_status",
    "verification_status",
    "document_type",
    "extracted_amount",
    "extracted_currency",
    "model",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "model_calls",
    "input_cost",
    "output_cost",
    "total_cost",
    "currency",
    "pricing_source",
    "created_at",
]

TRACE_EXPORT_COLUMNS = [
    "id",
    "email_id",
    "created_at",
    "actor",
    "event_name",
    "status",
    "message",
    "detail_json",
]


def _write_csv(output: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@app.callback()
def main(config: ConfigOpt = Path("configs/default.yaml")) -> None:
    del config


@app.command("init-db")
def init_db(config: ConfigOpt = Path("configs/default.yaml")) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    show_header("inbox-da", "Initializing SQLite database")
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
    print_key_values("Database", {"path": settings.database_path, "status": "ready"})


@app.command("seed")
def seed(
    count: Annotated[int, typer.Option("--count", min=1)] = 50,
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
        ids = seed_database(conn, count=count, attachments_dir=settings.attachments_dir)
    print_key_values(
        "Seed complete", {"emails": len(ids), "attachments_dir": settings.attachments_dir}
    )


@app.command("ingest-one")
def ingest_one(config: ConfigOpt = Path("configs/default.yaml")) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
        ids = seed_database(conn, count=1, attachments_dir=settings.attachments_dir)
        extraction_id = process_email(
            conn, email_id=ids[0], settings=settings, require_credentials=False
        )
    print_key_values("Ingested", {"email_id": ids[0], "extraction_id": extraction_id})


@app.command("process")
def process(
    email_id: Annotated[int, typer.Option("--email-id")],
    config: ConfigOpt = Path("configs/default.yaml"),
    offline_demo: Annotated[
        bool,
        typer.Option(
            "--offline-demo", help="Use deterministic local extraction instead of Gemini."
        ),
    ] = False,
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
        extraction_id = process_email(
            conn,
            email_id=email_id,
            settings=settings,
            require_credentials=not offline_demo,
        )
    print_key_values("Processed", {"email_id": email_id, "extraction_id": extraction_id})


@app.command("watch")
def watch(
    interval: Annotated[int | None, typer.Option("--interval")] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Process one polling cycle and exit.")
    ] = False,
    config: ConfigOpt = Path("configs/default.yaml"),
    offline_demo: Annotated[
        bool,
        typer.Option(
            "--offline-demo", help="Use deterministic local extraction instead of Gemini."
        ),
    ] = False,
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    validate_model_config(settings, require_credentials=not offline_demo)
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
        poll_interval = interval or settings.watch_interval_seconds
        while True:
            for email_id in db.claim_new_emails(conn, limit=1):
                try:
                    process_email(
                        conn,
                        email_id=email_id,
                        settings=settings,
                        require_credentials=not offline_demo,
                    )
                except Exception as exc:
                    db.insert_trace(
                        conn,
                        email_id=email_id,
                        task_name="watch",
                        step_name="process_email",
                        status="failed",
                        detail={"email_id": email_id},
                        error=str(exc),
                    )
                    db.insert_email_extraction_trace(
                        conn,
                        email_id=email_id,
                        actor="watch",
                        event_name="process_failed",
                        status="failed",
                        message=str(exc),
                    )
                    db.mark_email_status(conn, email_id, "failed")
                    console.log(f"[red]email {email_id} failed:[/red] {exc}")
            if once:
                return
            time.sleep(poll_interval)


@app.command("inspect")
def inspect_email(
    email_id: Annotated[int, typer.Option("--email-id")],
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        email = dict(db.get_email(conn, email_id))
        attachments = db.rows_to_dicts(db.list_attachments(conn, email_id))
        latest_cost = db.latest_email_cost(conn, email_id)
        trace_count = conn.execute(
            "SELECT COUNT(*) AS n FROM email_extraction_traces WHERE email_id = ?",
            (email_id,),
        ).fetchone()["n"]
    print_key_values("Email", email)
    print_key_values(
        "Attachments", {"count": len(attachments), "files": [a["filename"] for a in attachments]}
    )
    if latest_cost:
        print_key_values(
            "Latest Cost Estimate",
            {
                "total_cost": f"{latest_cost['total_cost']:.8f} {latest_cost['currency']}",
                "input_tokens": latest_cost["input_tokens"],
                "output_tokens": latest_cost["output_tokens"],
                "model_calls": latest_cost["model_calls"],
            },
        )
    print_key_values("Trace", {"email_extraction_trace_rows": trace_count})


@app.command("export")
def export(
    format: Annotated[str, typer.Option("--format")] = "csv",
    output: Annotated[Path, typer.Option("--output")] = Path("data/structured_extraction.csv"),
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    if format != "csv":
        raise typer.BadParameter("Only csv export is supported in v1")
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        rows = conn.execute("SELECT * FROM structured_extraction ORDER BY id").fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.writer(handle)
        columns = rows[0].keys() if rows else ["id"]
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[column] for column in columns])
    print_key_values("Export", {"path": output, "rows": len(rows)})


@app.command("agent-info")
def agent_info(config: ConfigOpt = Path("configs/default.yaml")) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    summary = agent_runtime_summary(settings)
    print_key_values(
        "Agent Runtime",
        {
            "sync_subagents": summary["sync_subagents"],
            "async_subagents": summary["async_subagents"],
            "skill_paths": summary["skill_paths"],
            "memory_paths": summary["memory_paths"],
        },
    )


@app.command("trace")
def trace_email(
    email_id: Annotated[int, typer.Option("--email-id")],
    limit: Annotated[int, typer.Option("--limit", min=1)] = 25,
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        rows = db.list_email_extraction_traces(conn, email_id, limit=limit)
        latest_cost = db.latest_email_cost(conn, email_id)

    if latest_cost:
        print_key_values(
            "Latest Cost Estimate",
            {
                "total_cost": f"{latest_cost['total_cost']:.8f} {latest_cost['currency']}",
                "tokens": latest_cost["total_tokens"],
                "model_calls": latest_cost["model_calls"],
                "pricing_source": latest_cost["pricing_source"],
            },
        )

    table = Table(title=f"Email {email_id} Extraction Trace")
    table.add_column("Time")
    table.add_column("Actor")
    table.add_column("Event")
    table.add_column("Status")
    table.add_column("Message")
    for row in reversed(rows):
        table.add_row(
            row["created_at"],
            row["actor"],
            row["event_name"],
            row["status"],
            row["message"],
        )
    console.print(table)


@app.command("export-trace")
def export_trace(
    email_id: Annotated[int, typer.Option("--email-id")],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="CSV path. Defaults to data/email_<id>_trace.csv."),
    ] = None,
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        rows = db.rows_to_dicts(db.list_email_extraction_traces(conn, email_id, limit=None))

    path = output or Path(f"data/email_{email_id}_trace.csv")
    chronological_rows = list(reversed(rows))
    _write_csv(path, chronological_rows, TRACE_EXPORT_COLUMNS)
    print_key_values("Trace Export", {"email_id": email_id, "path": path, "rows": len(rows)})


@app.command("cost-report")
def cost_report(
    format: Annotated[str, typer.Option("--format")] = "table",
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional CSV output path for the report."),
    ] = None,
    config: ConfigOpt = Path("configs/default.yaml"),
) -> None:
    if format not in {"table", "csv"}:
        raise typer.BadParameter("Only table and csv cost reports are supported")
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        rows = db.rows_to_dicts(db.list_latest_email_costs(conn))
        summary = db.summarize_latest_email_costs(conn)

    csv_path = output or (Path("data/cost_report.csv") if format == "csv" else None)
    if csv_path:
        _write_csv(csv_path, rows, COST_REPORT_COLUMNS)

    if format == "table":
        print_key_values(
            "Cost Report Summary",
            {
                "emails": summary.get("email_count", 0),
                "total_cost": f"{summary.get('total_cost', 0.0):.8f} "
                f"{summary.get('currency', 'USD')}",
                "input_tokens": summary.get("input_tokens", 0),
                "output_tokens": summary.get("output_tokens", 0),
                "model_calls": summary.get("model_calls", 0),
            },
        )
        table = Table(title="Latest Email Processing Costs")
        table.add_column("Email")
        table.add_column("Status")
        table.add_column("Verification")
        table.add_column("Tokens", justify="right")
        table.add_column("Calls", justify="right")
        table.add_column("Cost", justify="right")
        for row in rows:
            table.add_row(
                str(row["email_id"]),
                str(row["email_status"]),
                str(row["verification_status"] or ""),
                str(row["total_tokens"]),
                str(row["model_calls"]),
                f"{row['total_cost']:.8f} {row['currency']}",
            )
        console.print(table)

    if csv_path:
        print_key_values("Cost Report CSV", {"path": csv_path, "rows": len(rows)})


@app.command("web")
def web(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on code changes.")] = False,
    config: ConfigOpt = Path("configs/default.yaml"),
    offline_demo: Annotated[
        bool,
        typer.Option(
            "--offline-demo/--live-agent",
            help="Use deterministic local processing from web buttons instead of Gemini.",
        ),
    ] = True,
) -> None:
    settings = load_settings(config)
    configure_logging(settings.log_level)
    with closing(db.connect(settings.database_path)) as conn:
        db.init_db(conn)
    import uvicorn

    print_key_values(
        "Web Console",
        {
            "url": f"http://{host}:{port}",
            "database": settings.database_path,
            "button_processing": "offline-demo" if offline_demo else "live-agent",
        },
    )
    from inbox_da.web import create_app

    if reload:
        os.environ["INBOX_DA_CONFIG"] = str(config)
        os.environ["INBOX_DA_WEB_OFFLINE_DEMO"] = "1" if offline_demo else "0"
        uvicorn.run(
            "inbox_da.web:create_app_from_env",
            host=host,
            port=port,
            reload=True,
            factory=True,
            log_level=settings.log_level.lower(),
        )
    else:
        uvicorn.run(
            create_app(settings, process_offline_demo=offline_demo),
            host=host,
            port=port,
            log_level=settings.log_level.lower(),
        )
