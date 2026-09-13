import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import azure.functions as func
import pytest

import blueprints.ai_qa as ai_qa_blueprint
from config import AIQAModelAuthMode, AIQAModelSettings, AIQASettings
from models import (
    AIQADataStatus,
    AIQAKnowledgeSource,
    AIQAModelKnowledgeEvent,
    AIQAResponseOutcome,
    AIQASkillName,
    AIQAUsage,
)
from services.ai_qa import AIQAOrchestratorResult


class FakeOrchestrator:
    def __init__(self, result: AIQAOrchestratorResult) -> None:
        self.result = result
        self.questions: list[str] = []

    async def answer(
        self, question: str, *, now: datetime | None = None
    ) -> AIQAOrchestratorResult:
        self.questions.append(question)
        return self.result


def request(body: object) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="http://localhost:7071/api/ai/qa",
        headers={"Content-Type": "application/json"},
        params={},
        route_params={},
        body=json.dumps(body).encode(),
    )


def install_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    debug: bool,
    result: AIQAOrchestratorResult,
) -> FakeOrchestrator:
    model = AIQAModelSettings(
        profile_name="AZURE_GPT",
        endpoint_url="https://example.services.ai.azure.com/openai/v1/",
        asset_name="test-deployment",
        model_name="gpt-test",
        auth_mode=AIQAModelAuthMode.MANAGED_IDENTITY,
        api_key="",
        context_window_tokens=2048,
        max_output_tokens=1024,
        timeout_seconds=15,
    )
    qa = AIQASettings(
        earthquake_data_available_from_utc=datetime(2026, 9, 11, 17, tzinfo=UTC),
        max_model_requests=5,
        max_tool_calls=3,
        max_execution_seconds=210,
        debug=debug,
    )
    settings = SimpleNamespace(
        ai_qa_model=model,
        ai_qa=qa,
        cosmos_earthquake_db_connection_string="not-a-real-secret",
        cosmos_earthquake_db_name="earthquakes",
        cosmos_earthquake_data_container_name="data",
        earthquake_query_max_window_hours=720,
        earthquake_query_max_radius_km=1000,
    )
    fake = FakeOrchestrator(result)
    monkeypatch.setattr(ai_qa_blueprint, "ServiceSettings", lambda: settings)
    monkeypatch.setattr(
        ai_qa_blueprint,
        "_build_model_service",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        ai_qa_blueprint,
        "_build_data_services",
        lambda *_args: SimpleNamespace(availability=None, skills=None),
    )
    monkeypatch.setattr(ai_qa_blueprint, "AIQAOrchestrator", lambda *_args: fake)
    return fake


def result() -> AIQAOrchestratorResult:
    return AIQAOrchestratorResult(
        outcome=AIQAResponseOutcome.ANSWERED,
        skill=AIQASkillName.LYTIR_DATA,
        answer="**One** earthquake matched.",
        confidence=0.95,
        usage=AIQAUsage(
            requests=2,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
        ),
        data_context={"matched_records": 1},
    )


def test_ai_qa_endpoint_omits_debug_fields_in_normal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_endpoint(monkeypatch, debug=False, result=result())

    response = asyncio.run(
        ai_qa_blueprint.answer_ai_qa(request({"question": "Any earthquakes?"}))
    )
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["answer_format"] == "markdown"
    assert payload["model"] == {
        "host": "azure",
        "profile": "AZURE_GPT",
        "name": "gpt-test",
    }
    assert "skill" not in payload
    assert "usage" not in payload
    assert "data_context" not in payload
    assert fake.questions == ["Any earthquakes?"]


def test_ai_qa_endpoint_includes_debug_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_endpoint(monkeypatch, debug=True, result=result())

    response = asyncio.run(
        ai_qa_blueprint.answer_ai_qa(request({"question": "Any earthquakes?"}))
    )
    payload = json.loads(response.get_body())

    assert payload["skill"] == "lytir_data"
    assert payload["usage"]["requests"] == 2
    assert payload["data_context"] == {"matched_records": 1}


def test_ai_qa_endpoint_omits_confidence_for_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clarification = AIQAOrchestratorResult(
        outcome=AIQAResponseOutcome.CLARIFICATION_REQUIRED,
        skill=AIQASkillName.LYTIR_DATA,
        answer="Which Dallas do you mean?",
        confidence=None,
        usage=AIQAUsage(
            requests=1,
            input_tokens=50,
            output_tokens=10,
            total_tokens=60,
        ),
    )
    install_endpoint(monkeypatch, debug=False, result=clarification)

    response = asyncio.run(
        ai_qa_blueprint.answer_ai_qa(request({"question": "Near Dallas?"}))
    )
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["outcome"] == "clarification_required"
    assert "confidence" not in payload


def test_ai_qa_endpoint_includes_model_knowledge_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = AIQAOrchestratorResult(
        outcome=AIQAResponseOutcome.ANSWERED,
        skill=AIQASkillName.LYTIR_DATA,
        answer="Lytir cannot verify the requested historical period.",
        confidence=0.8,
        usage=AIQAUsage(
            requests=2,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
        ),
        data_status=AIQADataStatus.OUTSIDE_AVAILABLE_RANGE,
        knowledge_source=AIQAKnowledgeSource.MODEL_KNOWLEDGE,
        events=(
            AIQAModelKnowledgeEvent(
                event_type="earthquake",
                place="California",
            ),
        ),
    )
    install_endpoint(monkeypatch, debug=False, result=fallback)

    response = asyncio.run(
        ai_qa_blueprint.answer_ai_qa(
            request({"question": "Were there earthquakes in California in 2000?"})
        )
    )
    payload = json.loads(response.get_body())

    assert payload["data_status"] == "outside_available_range"
    assert payload["knowledge_source"] == "model_knowledge"
    assert payload["events"] == [
        {
            "event_type": "earthquake",
            "magnitude": None,
            "place": "California",
            "occurred_at_utc": None,
            "updated_at_utc": None,
            "longitude": None,
            "latitude": None,
            "depth_km": None,
        }
    ]


def test_ai_qa_endpoint_includes_partial_coverage_status_in_normal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = result()
    partial = AIQAOrchestratorResult(
        outcome=partial.outcome,
        skill=partial.skill,
        answer=partial.answer,
        confidence=partial.confidence,
        usage=partial.usage,
        data_context=partial.data_context,
        data_status=AIQADataStatus.PARTIAL_COVERAGE,
    )
    install_endpoint(monkeypatch, debug=False, result=partial)

    response = asyncio.run(
        ai_qa_blueprint.answer_ai_qa(request({"question": "This year?"}))
    )
    payload = json.loads(response.get_body())

    assert payload["data_status"] == "partial_coverage"
    assert "data_context" not in payload


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "hello", "extra": True}],
)
def test_ai_qa_endpoint_rejects_invalid_request(body: object) -> None:
    response = asyncio.run(ai_qa_blueprint.answer_ai_qa(request(body)))

    assert response.status_code == 400
    assert json.loads(response.get_body())["error"].startswith("Invalid request:")
