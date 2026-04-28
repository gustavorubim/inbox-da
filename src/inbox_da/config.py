"""Configuration loading for the inbox monitor."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from inbox_da.costing import (
    GEMINI_3_FLASH_PREVIEW_INPUT_PER_1M_USD,
    GEMINI_3_FLASH_PREVIEW_OUTPUT_PER_1M_USD,
    GEMINI_3_FLASH_PRICING_SOURCE,
)


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/inbox_da.sqlite")
    attachments_dir: Path = Path("data/attachments")
    model_name: str = "gemini-3-flash-preview"
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
    cost_input_per_1m: float = GEMINI_3_FLASH_PREVIEW_INPUT_PER_1M_USD
    cost_output_per_1m: float = GEMINI_3_FLASH_PREVIEW_OUTPUT_PER_1M_USD
    cost_currency: str = "USD"
    cost_pricing_source: str = GEMINI_3_FLASH_PRICING_SOURCE

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
