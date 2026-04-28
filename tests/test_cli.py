from pathlib import Path

from typer.testing import CliRunner

from inbox_da.cli import app

runner = CliRunner()


def test_cli_init_seed_process_inspect_export(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    database = tmp_path / "db.sqlite"
    attachments = tmp_path / "attachments"
    config.write_text(
        "\n".join(
            [
                f"database_path: {database}",
                f"attachments_dir: {attachments}",
                "model_name: gemini-3-flash-preview",
                "attachment_text_min_chars: 5",
            ]
        )
    )
    assert runner.invoke(app, ["init-db", "--config", str(config)]).exit_code == 0
    assert runner.invoke(app, ["agent-info", "--config", str(config)]).exit_code == 0
    assert runner.invoke(app, ["seed", "--count", "2", "--config", str(config)]).exit_code == 0
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
    config.write_text(
        "\n".join(
            [
                f"database_path: {database}",
                f"attachments_dir: {attachments}",
                "model_name: gemini-3-flash-preview",
            ]
        )
    )
    runner.invoke(app, ["seed", "--count", "1", "--config", str(config)])
    result = runner.invoke(app, ["watch", "--once", "--offline-demo", "--config", str(config)])
    assert result.exit_code == 0, result.output
