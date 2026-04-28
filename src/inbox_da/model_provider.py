"""Google Gemini and DeepAgents model integration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.permissions import FilesystemPermission

from inbox_da.config import Settings


def validate_model_config(settings: Settings, *, require_credentials: bool = True) -> None:
    if settings.model_name != "gemini-3-flash-preview":
        raise ValueError("This project requires model_name='gemini-3-flash-preview'")
    if require_credentials and not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError("GOOGLE_API_KEY must be set before running agent commands")


def build_gemini_model(settings: Settings) -> Any:
    validate_model_config(settings, require_credentials=True)
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=settings.model_name, temperature=settings.temperature)


SYNC_SUBAGENT_NAMES = (
    "attachment-reader",
    "financial-field-extractor",
    "evidence-verifier",
    "review-agent",
)


def build_deep_agent(
    *,
    model: Any,
    tools: list[Any],
    system_prompt: str,
    settings: Settings | None = None,
) -> Any:
    from deepagents import create_deep_agent

    settings = settings or Settings()
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        subagents=build_subagents(settings, model=model),
        skills=list(settings.skill_paths) or None,
        memory=list(settings.memory_paths) or None,
        backend=build_agent_backend(),
        permissions=build_agent_permissions(),
    )


def build_subagents(settings: Settings, *, model: Any) -> list[dict[str, Any]]:
    subagents: list[dict[str, Any]] = []
    if settings.subagents_enabled:
        subagents.extend(_sync_subagents(model, settings.skill_paths))
    subagents.extend(dict(spec) for spec in settings.async_subagents)
    return subagents


def build_agent_backend() -> FilesystemBackend:
    return FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True)


def build_agent_permissions() -> list[FilesystemPermission]:
    return [
        FilesystemPermission(operations=["read"], paths=["/.env", "/**/.env"], mode="deny"),
        FilesystemPermission(operations=["read"], paths=["/skills/**", "/memories/**"]),
        FilesystemPermission(operations=["read"], paths=["/**"], mode="deny"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]


def agent_runtime_summary(settings: Settings) -> dict[str, Any]:
    return {
        "sync_subagents": list(SYNC_SUBAGENT_NAMES) if settings.subagents_enabled else [],
        "async_subagents": [
            spec.get("name", spec.get("graph_id", "unnamed")) for spec in settings.async_subagents
        ],
        "skill_paths": list(settings.skill_paths),
        "memory_paths": list(settings.memory_paths),
    }


def _sync_subagents(model: Any, skill_paths: tuple[str, ...]) -> list[dict[str, Any]]:
    common = {"model": model, "tools": [], "skills": list(skill_paths)}
    return [
        {
            **common,
            "name": "attachment-reader",
            "description": (
                "Summarizes financial evidence from email bodies, parsed attachments, "
                "Gemini vision summaries, PDFs, spreadsheets, and images."
            ),
            "system_prompt": (
                "You are the attachment-reader subagent. Identify source evidence in email "
                "bodies and attachments. Return concise JSON-compatible notes with filenames, "
                "attachment ids, key amounts, dates, counterparties, and direct quotes."
            ),
        },
        {
            **common,
            "name": "financial-field-extractor",
            "description": (
                "Extracts normalized financial fields such as document type, counterparty, "
                "amount, currency, due date, reference number, line items, and evidence."
            ),
            "system_prompt": (
                "You are the financial-field-extractor subagent. Extract exactly the target "
                "structured_extraction fields. Prefer explicit evidence and mark uncertain "
                "fields in your notes instead of inventing values."
            ),
        },
        {
            **common,
            "name": "evidence-verifier",
            "description": (
                "Checks candidate extraction fields against source email and attachment "
                "evidence and reports agreement, conflicts, and weak evidence."
            ),
            "system_prompt": (
                "You are the evidence-verifier subagent. Compare candidate fields to source "
                "evidence. Return whether candidates match, which fields conflict, and the "
                "short evidence quote that supports each important field."
            ),
        },
        {
            **common,
            "name": "review-agent",
            "description": (
                "Prepares conflict review notes when extractors disagree or evidence is weak."
            ),
            "system_prompt": (
                "You are the review-agent subagent. Explain why an extraction should be "
                "verified or marked needs_review. Be conservative when amounts, references, "
                "or dates disagree."
            ),
        },
    ]
