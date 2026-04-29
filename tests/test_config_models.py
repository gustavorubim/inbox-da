import os
from pathlib import Path

import pytest
import yaml

from inbox_da.config import Settings, load_env_file, load_settings
from inbox_da.model_provider import validate_model_config
from inbox_da.models import Evidence, StructuredExtraction, extractions_match


def test_load_settings_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "database_path": "x.sqlite",
                "model_name": "gemini-3-flash-preview",
                "oracle": {
                    "enabled": True,
                    "host": "db.example.test",
                    "port": 1521,
                    "service_name": "finance",
                    "username": "inbox_reader",
                    "password_env": "TEST_ORACLE_PASSWORD",
                    "email_table": "FINANCE.EMAIL_INBOX",
                    "email_columns": {"message_id": "EMAIL_GUID", "body": "BODY_CLOB"},
                    "attachment_table": "FINANCE.EMAIL_ATTACHMENTS",
                },
                "agent": {
                    "subagents_enabled": False,
                    "skill_paths": ["/skills/custom/"],
                    "memory_paths": ["/memories/custom.md"],
                    "async_subagents": [
                        {
                            "name": "async-reviewer",
                            "description": "Remote reviewer",
                            "graph_id": "reviewer",
                        }
                    ],
                },
            }
        )
    )
    monkeypatch.setenv("TEST_ORACLE_PASSWORD", "secret")
    settings = load_settings(path)
    assert settings.database_path == Path("x.sqlite")
    assert settings.model_name == "gemini-3-flash-preview"
    assert settings.oracle.enabled is True
    assert settings.oracle.host == "db.example.test"
    assert settings.oracle.service_name == "finance"
    assert settings.oracle.password == "secret"
    assert settings.oracle.email_table == "FINANCE.EMAIL_INBOX"
    assert settings.oracle.email_columns.message_id == "EMAIL_GUID"
    assert settings.oracle.email_columns.body == "BODY_CLOB"
    assert settings.oracle.attachment_table == "FINANCE.EMAIL_ATTACHMENTS"
    assert settings.subagents_enabled is False
    assert settings.skill_paths == ("/skills/custom/",)
    assert settings.memory_paths == ("/memories/custom.md",)
    assert settings.async_subagents[0]["name"] == "async-reviewer"


def test_load_env_file_sets_key_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("GOOGLE_API_KEY='from-file'\nOTHER=value\n")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    load_env_file(env_path)
    assert os.environ["GOOGLE_API_KEY"] == "from-file"
    monkeypatch.setenv("GOOGLE_API_KEY", "from-shell")
    load_env_file(env_path)
    assert os.environ["GOOGLE_API_KEY"] == "from-shell"


def test_invalid_model_rejected() -> None:
    settings = Settings(model_name="other-model")
    with pytest.raises(ValueError, match="gemini-3-flash-preview"):
        validate_model_config(settings, require_credentials=False)


def test_missing_api_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        validate_model_config(Settings(), require_credentials=True)


def test_structured_extraction_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        StructuredExtraction(
            document_type="invoice",
            counterparty="Vendor",
            currency="USD",
            amount=10.0,
            financial_category="ap",
            confidence=0.9,
        )


def test_extractions_match() -> None:
    base = StructuredExtraction(
        document_type="invoice",
        counterparty="Vendor",
        currency="USD",
        amount=10.0,
        reference_number="INV-1",
        financial_category="ap",
        confidence=0.9,
        evidence=[Evidence(source="email.body", quote="USD 10.00")],
    )
    assert extractions_match(base, base.model_copy())
    assert not extractions_match(base, base.model_copy(update={"amount": 11.0}))
