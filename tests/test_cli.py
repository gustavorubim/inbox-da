from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from inbox_da import db
from inbox_da.cli import app
from inbox_da.oracle_inbox import OracleAttachmentRecord, OracleEmailRecord

runner = CliRunner()


class FakeOracleClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info) -> None:
        pass

    def fetch_emails(self, *, limit: int):
        del limit
        return [
            OracleEmailRecord(
                message_id="<oracle-cli-1@example.test>",
                sender="billing@northstar.example",
                received_at="2026-04-28T10:00:00Z",
                subject="Invoice INV-0001 from Northstar Supplies",
                body=(
                    "Please review INV-0001. The invoice total is USD 1,037.25. "
                    "Counterparty: Northstar Supplies. Due date: 2026-05-15."
                ),
            ),
            OracleEmailRecord(
                message_id="<oracle-cli-2@example.test>",
                sender="alerts@metrobank.example",
                received_at="2026-04-28T09:00:00Z",
                subject="Bank notice for operating account 0002",
                body="Please review BANK-0002. The total is USD 1,074.50.",
            ),
        ]

    def fetch_attachments(self, *, message_id: str):
        return [
            OracleAttachmentRecord(
                filename=f"{message_id.strip('<>').split('@')[0]}.txt",
                mime_type="text/plain",
                content=b"Attachment amount USD 1037.25",
            )
        ]


def write_config(
    config: Path, database: Path, attachments: Path, *, oracle_enabled: bool = True
) -> None:
    config.write_text(
        "\n".join(
            [
                f"database_path: {database}",
                f"attachments_dir: {attachments}",
                "model_name: gemini-3-flash-preview",
                "attachment_text_min_chars: 5",
                "oracle:",
                f"  enabled: {str(oracle_enabled).lower()}",
                "  host: db.example.test",
                "  port: 1521",
                "  service_name: finance",
                "  username: inbox_reader",
                "  password: test-password",
                "  email_table: EMAILS",
                "  attachment_table: ATTACHMENTS",
            ]
        )
    )


def insert_local_email(database: Path) -> None:
    with closing(db.connect(database)) as conn:
        db.init_db(conn)
        db.insert_email(
            conn,
            message_id="<local-watch@example.test>",
            sender="billing@northstar.example",
            received_at="2026-04-28T10:00:00Z",
            subject="Invoice INV-0001 from Northstar Supplies",
            body=(
                "Please review INV-0001. The invoice total is USD 1,037.25. "
                "Counterparty: Northstar Supplies. Due date: 2026-05-15."
            ),
        )


def test_cli_init_sync_process_inspect_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    database = tmp_path / "db.sqlite"
    attachments = tmp_path / "attachments"
    write_config(config, database, attachments)
    monkeypatch.setattr("inbox_da.oracle_inbox.OracleInboxClient", FakeOracleClient)
    assert runner.invoke(app, ["init-db", "--config", str(config)]).exit_code == 0
    assert runner.invoke(app, ["agent-info", "--config", str(config)]).exit_code == 0
    assert (
        runner.invoke(app, ["sync-oracle", "--limit", "2", "--config", str(config)]).exit_code == 0
    )
    result = runner.invoke(
        app,
        ["process", "--email-id", "1", "--offline-demo", "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert (
        runner.invoke(app, ["inspect", "--email-id", "1", "--config", str(config)]).exit_code == 0
    )
    assert runner.invoke(app, ["trace", "--email-id", "1", "--config", str(config)]).exit_code == 0
    trace_output = tmp_path / "trace.csv"
    result = runner.invoke(
        app,
        [
            "export-trace",
            "--email-id",
            "1",
            "--output",
            str(trace_output),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert trace_output.exists()
    assert "event_name" in trace_output.read_text()
    assert runner.invoke(app, ["cost-report", "--config", str(config)]).exit_code == 0
    cost_output = tmp_path / "cost_report.csv"
    result = runner.invoke(
        app,
        [
            "cost-report",
            "--format",
            "csv",
            "--output",
            str(cost_output),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert cost_output.exists()
    assert "total_cost" in cost_output.read_text()
    output = tmp_path / "out.csv"
    result = runner.invoke(
        app,
        ["export", "--format", "csv", "--output", str(output), "--config", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()


def test_cli_watch_once_offline(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    database = tmp_path / "db.sqlite"
    attachments = tmp_path / "attachments"
    write_config(config, database, attachments, oracle_enabled=False)
    insert_local_email(database)
    result = runner.invoke(app, ["watch", "--once", "--offline-demo", "--config", str(config)])
    assert result.exit_code == 0, result.output


def test_cli_watch_marks_failed_email_and_keeps_monitor_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    database = tmp_path / "db.sqlite"
    attachments = tmp_path / "attachments"
    write_config(config, database, attachments, oracle_enabled=False)
    insert_local_email(database)

    def fail_process(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("vision parser failed")

    monkeypatch.setattr("inbox_da.cli.process_email", fail_process)
    result = runner.invoke(app, ["watch", "--once", "--offline-demo", "--config", str(config)])
    assert result.exit_code == 0, result.output
    with closing(db.connect(database)) as conn:
        assert db.get_email(conn, 1)["status"] == "failed"
        trace = db.list_email_extraction_traces(conn, 1, limit=1)[0]
        assert trace["event_name"] == "process_failed"
