"""Configuration loading for the inbox monitor."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from inbox_da.costing import (
    GEMINI_FLASH_PREVIEW_INPUT_PER_1M_USD,
    GEMINI_FLASH_PREVIEW_MODEL,
    GEMINI_FLASH_PREVIEW_OUTPUT_PER_1M_USD,
    GEMINI_FLASH_PRICING_SOURCE,
)


@dataclass(frozen=True)
class OracleEmailColumns:
    message_id: str = "MESSAGE_ID"
    sender: str = "SENDER"
    received_at: str = "RECEIVED_AT"
    subject: str = "SUBJECT"
    body: str = "BODY"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OracleEmailColumns:
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("Config key 'oracle.email_columns' must be a mapping")
        return cls(
            message_id=str(data.get("message_id", cls.message_id)),
            sender=str(data.get("sender", cls.sender)),
            received_at=str(data.get("received_at", cls.received_at)),
            subject=str(data.get("subject", cls.subject)),
            body=str(data.get("body", cls.body)),
        )


@dataclass(frozen=True)
class OracleAttachmentColumns:
    message_id: str = "MESSAGE_ID"
    filename: str = "FILENAME"
    mime_type: str = "MIME_TYPE"
    content: str = "CONTENT_BLOB"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OracleAttachmentColumns:
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("Config key 'oracle.attachment_columns' must be a mapping")
        return cls(
            message_id=str(data.get("message_id", cls.message_id)),
            filename=str(data.get("filename", cls.filename)),
            mime_type=str(data.get("mime_type", cls.mime_type)),
            content=str(data.get("content", cls.content)),
        )


@dataclass(frozen=True)
class OracleInboxSettings:
    enabled: bool = False
    host: str = ""
    port: int = 1521
    service_name: str = ""
    username: str = ""
    password: str = ""
    password_env: str = "ORACLE_DB_PASSWORD"
    email_table: str = "EMAILS"
    email_columns: OracleEmailColumns = field(default_factory=OracleEmailColumns)
    attachment_table: str | None = None
    attachment_columns: OracleAttachmentColumns = field(default_factory=OracleAttachmentColumns)
    fetch_limit: int = 50

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OracleInboxSettings:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("Config key 'oracle' must be a mapping")

        password_env = str(data.get("password_env", cls.password_env))
        password = str(data.get("password") or os.environ.get(password_env, ""))
        service_name = data.get("service_name", data.get("servicename", cls.service_name))
        attachment_table = data.get("attachment_table")
        return cls(
            enabled=bool(data.get("enabled", cls.enabled)),
            host=str(data.get("host") or os.environ.get("ORACLE_DB_HOST", "")),
            port=int(data.get("port") or os.environ.get("ORACLE_DB_PORT", cls.port)),
            service_name=str(service_name or os.environ.get("ORACLE_DB_SERVICE_NAME", "")),
            username=str(data.get("username") or os.environ.get("ORACLE_DB_USERNAME", "")),
            password=password,
            password_env=password_env,
            email_table=str(data.get("email_table", cls.email_table)),
            email_columns=OracleEmailColumns.from_dict(data.get("email_columns")),
            attachment_table=str(attachment_table) if attachment_table else None,
            attachment_columns=OracleAttachmentColumns.from_dict(data.get("attachment_columns")),
            fetch_limit=int(data.get("fetch_limit", cls.fetch_limit)),
        )

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "service_name": self.service_name,
            "username": self.username,
            "password_env": self.password_env,
            "email_table": self.email_table,
            "attachment_table": self.attachment_table or "",
            "fetch_limit": self.fetch_limit,
        }


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/inbox_da.sqlite")
    attachments_dir: Path = Path("data/attachments")
    model_name: str = GEMINI_FLASH_PREVIEW_MODEL
    temperature: float = 0.0
    watch_interval_seconds: int = 5
    attachment_text_min_chars: int = 80
    log_level: str = "INFO"
    trace_enabled: bool = True
    verification_mode: str = "mark_review"
    subagents_enabled: bool = True
    skill_paths: tuple[str, ...] = ("/skills/project/",)
    memory_paths: tuple[str, ...] = ("/memories/AGENTS.md",)
    async_subagents: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    cost_input_per_1m: float = GEMINI_FLASH_PREVIEW_INPUT_PER_1M_USD
    cost_output_per_1m: float = GEMINI_FLASH_PREVIEW_OUTPUT_PER_1M_USD
    cost_currency: str = "USD"
    cost_pricing_source: str = GEMINI_FLASH_PRICING_SOURCE
    oracle: OracleInboxSettings = field(default_factory=OracleInboxSettings)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        agent = data.get("agent", {})
        if agent is None:
            agent = {}
        if not isinstance(agent, dict):
            raise ValueError("Config key 'agent' must be a mapping")
        cost = data.get("cost_estimation", {})
        if cost is None:
            cost = {}
        if not isinstance(cost, dict):
            raise ValueError("Config key 'cost_estimation' must be a mapping")
        return cls(
            database_path=Path(data.get("database_path", cls.database_path)),
            attachments_dir=Path(data.get("attachments_dir", cls.attachments_dir)),
            model_name=str(data.get("model_name", cls.model_name)),
            temperature=float(data.get("temperature", cls.temperature)),
            watch_interval_seconds=int(
                data.get("watch_interval_seconds", cls.watch_interval_seconds)
            ),
            attachment_text_min_chars=int(
                data.get("attachment_text_min_chars", cls.attachment_text_min_chars)
            ),
            log_level=str(data.get("log_level", cls.log_level)),
            trace_enabled=bool(data.get("trace_enabled", cls.trace_enabled)),
            verification_mode=str(data.get("verification_mode", cls.verification_mode)),
            subagents_enabled=bool(agent.get("subagents_enabled", cls.subagents_enabled)),
            skill_paths=_tuple_of_strings(agent.get("skill_paths", cls.skill_paths)),
            memory_paths=_tuple_of_strings(agent.get("memory_paths", cls.memory_paths)),
            async_subagents=_tuple_of_dicts(agent.get("async_subagents", ())),
            cost_input_per_1m=float(cost.get("input_per_1m", cls.cost_input_per_1m)),
            cost_output_per_1m=float(cost.get("output_per_1m", cls.cost_output_per_1m)),
            cost_currency=str(cost.get("currency", cls.cost_currency)),
            cost_pricing_source=str(cost.get("pricing_source", cls.cost_pricing_source)),
            oracle=OracleInboxSettings.from_dict(data.get("oracle")),
        )


def load_settings(config_path: Path | str = Path("configs/default.yaml")) -> Settings:
    load_env_file()
    path = Path(config_path)
    if not path.exists():
        return Settings()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return Settings.from_dict(data)


def load_env_file(env_path: Path | str = Path(".env")) -> None:
    """Load simple KEY=VALUE lines from .env without overriding shell environment."""
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _tuple_of_dicts(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    return tuple(dict(item) for item in value)
