import asyncio
import json
from types import SimpleNamespace

import azure.functions as func
import pytest

import blueprints.ai_qa_diagnostic as diagnostic
from config import AIQAModelAuthMode, AIQAModelSettings
from models import AIQAResult, AIQAUsage


class FakeAIQAService:
    def __init__(self, answer: str = "Tokyo", *, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.questions: list[str] = []

    async def ask(self, question: str, *, debug: bool = False) -> AIQAResult:
        self.questions.append(question)
        if self.fail:
            raise RuntimeError("provider included a sensitive diagnostic")
        return AIQAResult(
            answer=self.answer,
            confidence=0.99,
            usage=AIQAUsage(
                requests=1,
                input_tokens=20,
                output_tokens=5,
                total_tokens=25,
            ),
        )


def request(body: object) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="http://localhost:7071/api/diag/ai-qa",
        headers={"Content-Type": "application/json"},
        params={},
        route_params={},
        body=json.dumps(body).encode(),
    )


def settings() -> AIQAModelSettings:
    return AIQAModelSettings(
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


def install_service(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeAIQAService,
) -> None:
    model_settings = settings()
    monkeypatch.setattr(
        diagnostic,
        "ServiceSettings",
        lambda: SimpleNamespace(ai_qa_model=model_settings, debug_mode=False),
    )
    monkeypatch.setattr(diagnostic, "_build_service", lambda _: service)


def test_ai_qa_diagnostic_returns_model_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAIQAService()
    install_service(monkeypatch, service)

    response = asyncio.run(
        diagnostic.diagnose_ai_qa(
            request({"question": "  What is the capital of Japan?  "})
        )
    )
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert payload["question"] == "What is the capital of Japan?"
    assert payload["answer"] == "Tokyo"
    assert payload["confidence"] == 0.99
    assert payload["usage"] == {
        "requests": 1,
        "input_tokens": 20,
        "output_tokens": 5,
        "total_tokens": 25,
    }
    assert payload["execution_seconds"] >= 0
    assert payload["model_host"] == "azure"
    assert payload["model_profile"] == "AZURE_GPT"
    assert payload["model_name"] == "gpt-test"
    assert service.questions == ["What is the capital of Japan?"]
    assert payload["utc_now"].endswith("Z")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "hello", "unexpected": True},
        {"question": "x" * 4001},
    ],
)
def test_ai_qa_diagnostic_rejects_invalid_request(body: object) -> None:
    response = asyncio.run(diagnostic.diagnose_ai_qa(request(body)))
    payload = json.loads(response.get_body())

    assert response.status_code == 400
    assert payload["error"].startswith("Invalid request:")


def test_ai_qa_diagnostic_returns_safe_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_service(monkeypatch, FakeAIQAService(fail=True))

    response = asyncio.run(diagnostic.diagnose_ai_qa(request({"question": "Hello"})))
    payload = json.loads(response.get_body())

    assert response.status_code == 503
    assert payload["error"] == "AI Q&A model is temporarily unavailable"
    assert "sensitive" not in response.get_body().decode()


def test_ai_qa_diagnostic_returns_safe_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_settings() -> None:
        raise ValueError("configuration included a sensitive value")

    monkeypatch.setattr(diagnostic, "ServiceSettings", invalid_settings)

    response = asyncio.run(diagnostic.diagnose_ai_qa(request({"question": "Hello"})))
    payload = json.loads(response.get_body())

    assert response.status_code == 500
    assert payload["error"] == "AI Q&A model is not configured"
    assert "sensitive" not in response.get_body().decode()
