import asyncio
import logging

from openai import AsyncOpenAI
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles import ModelProfile
from pytest import LogCaptureFixture

from config import AIQAModelAuthMode, AIQAModelSettings
from models import AIQAModelAnswer, AIQARouterDecision
from services.ai_qa_service import (
    AIQAService,
    _map_response_usage,
    build_ai_qa_service,
)


def model_settings(
    *,
    auth_mode: AIQAModelAuthMode,
    endpoint_url: str,
    api_key: str,
) -> AIQAModelSettings:
    return AIQAModelSettings(
        profile_name="TEST",
        endpoint_url=endpoint_url,
        asset_name="test-deployment",
        model_name="gpt-test",
        auth_mode=auth_mode,
        api_key=api_key,
        context_window_tokens=2048,
        max_output_tokens=1024,
        timeout_seconds=15,
    )


def test_ai_qa_service_returns_single_model_answer() -> None:
    settings = model_settings(
        auth_mode=AIQAModelAuthMode.API_KEY,
        endpoint_url="https://api.openai.com/v1/",
        api_key="not-a-real-key",
    )
    client = AsyncOpenAI(
        base_url=settings.endpoint_url,
        api_key="not-a-real-key",
    )
    test_model = TestModel(
        custom_output_text='{"answer":"Tokyo","confidence":0.99}',
        profile=ModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        ),
    )
    agent: Agent[None, AIQAModelAnswer] = Agent(
        test_model,
        output_type=NativeOutput(AIQAModelAnswer),
    )
    service = AIQAService(
        agent,
        client=client,
        credential=None,
        settings=settings,
    )

    async def run() -> None:
        result = await service.ask("What is the capital of Japan?")
        assert result.answer == "Tokyo"
        assert result.confidence == 0.99
        assert result.usage.requests == 1
        assert result.usage.total_tokens > 0
        await service.close()

    asyncio.run(run())


def test_build_ai_qa_service_uses_native_openai_api_key() -> None:
    settings = model_settings(
        auth_mode=AIQAModelAuthMode.API_KEY,
        endpoint_url="https://api.openai.com/v1/",
        api_key="not-a-real-key",
    )

    service = build_ai_qa_service(settings)

    assert service.profile_name == "TEST"
    assert service.auth_mode is AIQAModelAuthMode.API_KEY
    assert not callable(service._client.api_key)
    asyncio.run(service.close())


def test_build_ai_qa_service_uses_foundry_managed_identity() -> None:
    settings = model_settings(
        auth_mode=AIQAModelAuthMode.MANAGED_IDENTITY,
        endpoint_url="https://example.services.ai.azure.com/openai/v1/",
        api_key="",
    )

    service = build_ai_qa_service(settings)

    assert service.profile_name == "TEST"
    assert service.auth_mode is AIQAModelAuthMode.MANAGED_IDENTITY
    assert callable(service._client._api_key_provider)
    asyncio.run(service.close())


def test_map_response_usage_preserves_provider_token_counts() -> None:
    usage = _map_response_usage(
        ResponseUsage(
            input_tokens=11,
            input_tokens_details=InputTokensDetails(
                cache_write_tokens=2,
                cached_tokens=3,
            ),
            output_tokens=5,
            output_tokens_details=OutputTokensDetails(reasoning_tokens=4),
            total_tokens=16,
        )
    )

    assert usage.input_tokens == 11
    assert usage.output_tokens == 5
    assert usage.total_tokens == 16
    assert usage.cache_write_tokens == 2
    assert usage.cache_read_tokens == 3
    assert usage.details == {"reasoning_tokens": 4}


def test_structured_model_call_logs_context_only_in_debug(
    caplog: LogCaptureFixture,
) -> None:
    settings = model_settings(
        auth_mode=AIQAModelAuthMode.API_KEY,
        endpoint_url="https://api.openai.com/v1/",
        api_key="not-a-real-key",
    )
    client = AsyncOpenAI(base_url=settings.endpoint_url, api_key="not-a-real-key")
    test_model = TestModel(
        custom_output_text=(
            '{"outcome":"answered","skill":"general","answer":"Tokyo",'
            '"confidence":0.99,"data_request":null,"clarification":null}'
        ),
        profile=ModelProfile(
            supports_json_schema_output=True,
            default_structured_output_mode="native",
        ),
    )
    agent: Agent[None, AIQAModelAnswer] = Agent(
        test_model,
        output_type=NativeOutput(AIQAModelAnswer),
    )
    service = AIQAService(
        agent,
        client=client,
        credential=None,
        settings=settings,
    )

    async def run() -> None:
        await service.run_structured(
            AIQARouterDecision,
            instructions="private routing instructions",
            context="question and context",
            timeout_seconds=5,
            debug=False,
            request_label="router-v1",
        )
        assert "private routing instructions" not in caplog.text
        caplog.clear()
        await service.run_structured(
            AIQARouterDecision,
            instructions="debug routing instructions",
            context="debug question and context",
            timeout_seconds=5,
            debug=True,
            request_label="router-v1",
        )
        assert "debug routing instructions" in caplog.text
        assert "debug question and context" in caplog.text
        assert '"answer":"Tokyo"' in caplog.text
        await service.close()

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
