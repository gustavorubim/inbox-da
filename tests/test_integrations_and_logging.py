from pathlib import Path
from typing import Any

import pytest

from inbox_da import attachments, model_provider
from inbox_da.agent import DeepAgentExtractor, DeepAgentVerifier
from inbox_da.config import Settings, load_settings
from inbox_da.costing import collect_token_usage, estimate_cost
from inbox_da.logging_utils import progress_step, show_header


class FakeModel:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def invoke(self, content: Any) -> Any:
        self.messages.append(content)
        return type("Response", (), {"content": '{"status":"verified","reason":"ok"}'})()


class FakeAgent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.invocations: list[Any] = []

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append(request)
        return {"structured_response": self.payload}


def extraction_payload() -> dict[str, Any]:
    return {
        "document_type": "invoice",
        "counterparty": "Northstar Supplies",
        "account_or_customer": "Operations-4182",
        "currency": "USD",
        "amount": 10.0,
        "reference_number": "INV-0001",
        "financial_category": "accounts_payable",
        "line_items": [{"description": "Services", "amount": 10.0, "currency": "USD"}],
        "evidence": [{"source": "email.body", "quote": "USD 10.00"}],
        "confidence": 0.9,
        "verification_status": "candidate",
    }


def test_gemini_vision_parser_encodes_file(tmp_path: Path) -> None:
    path = tmp_path / "receipt.png"
    path.write_bytes(b"abc")
    model = FakeModel()
    parser = attachments.GeminiVisionParser(model)
    result = parser.parse_image(path, mime_type="image/png")
    assert result["mime_type"] == "image/png"
    assert "verified" in result["model_response"]
    assert "base64" in str(model.messages[0])
    assert result["token_usage"]["total_tokens"] == 0


def test_parse_unsupported_attachment_type(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"zip")
    with pytest.raises(ValueError, match="Unsupported"):
        attachments.parse_attachment(path, mime_type="application/zip", settings=Settings())


def test_deepagent_extractor_and_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    def fake_build_deep_agent(
        *, model: Any, tools: list[Any], system_prompt: str, settings: Settings
    ) -> FakeAgent:
        created.append(
            {
                "model": model,
                "tools": tools,
                "system_prompt": system_prompt,
                "settings": settings,
            }
        )
        payload = (
            extraction_payload()
            if "extraction pass" in system_prompt
            else {
                "status": "verified",
                "reason": "ok",
                "conflicts": [],
            }
        )
        return FakeAgent(payload)

    monkeypatch.setattr("inbox_da.agent.build_deep_agent", fake_build_deep_agent)
    settings = Settings()
    model = object()
    context = {"email": {"body": "USD 10.00"}, "attachments": []}
    run = DeepAgentExtractor(settings, model).extract(context, pass_name="A")
    raw = run.payload
    assert raw["amount"] == 10.0
    from inbox_da.models import StructuredExtraction

    extraction = StructuredExtraction.model_validate(raw)
    result = DeepAgentVerifier(settings, model).verify(context, extraction, extraction).result
    assert result.status == "verified"
    assert len(created) == 2
    assert created[0]["settings"].subagents_enabled is True


def test_model_factory_uses_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")

    class FakeChat:
        def __init__(self, *, model: str, temperature: float) -> None:
            self.model = model
            self.temperature = temperature

    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", FakeChat)
    model = model_provider.build_gemini_model(Settings(temperature=0.2))
    assert model.model == "gemini-3-flash-preview"
    assert model.temperature == 0.2


def test_build_deep_agent_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create_deep_agent(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr("deepagents.create_deep_agent", fake_create_deep_agent)
    result = model_provider.build_deep_agent(model="m", tools=[], system_prompt="s")
    assert result["model"] == "m"
    assert result["tools"] == []
    assert result["system_prompt"] == "s"
    assert [item["name"] for item in captured["subagents"][:4]] == [
        "attachment-reader",
        "financial-field-extractor",
        "evidence-verifier",
        "review-agent",
    ]
    assert captured["skills"] == ["/skills/project/"]
    assert captured["memory"] == ["/memories/AGENTS.md"]


def test_async_subagent_config_is_included() -> None:
    settings = Settings(
        async_subagents=(
            {
                "name": "async-attachment-reader",
                "description": "Remote background attachment reader.",
                "graph_id": "attachment-reader",
                "url": "https://example.test",
            },
        )
    )
    subagents = model_provider.build_subagents(settings, model="m")
    assert subagents[-1]["name"] == "async-attachment-reader"
    assert model_provider.agent_runtime_summary(settings)["async_subagents"] == [
        "async-attachment-reader"
    ]


def test_collect_token_usage_and_estimate_cost() -> None:
    message = type(
        "Message",
        (),
        {"usage_metadata": {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}},
    )()
    usage = collect_token_usage({"messages": [message]})
    cost = estimate_cost(usage)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 100
    assert usage.model_calls == 1
    assert cost.total_cost == 0.0008


def test_config_defaults_and_invalid_yaml(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.yaml").model_name == "gemini-3-flash-preview"
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- mapping\n")
    with pytest.raises(ValueError, match="mapping"):
        load_settings(bad)


def test_rich_helpers_run() -> None:
    show_header("title", "subtitle")
    with progress_step("step"):
        pass
