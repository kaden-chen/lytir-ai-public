"""Provider-switchable, single-round PydanticAI model service."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseUsage
from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from pydantic_ai.profiles.openai import openai_model_profile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage, UsageLimits

from config import AIQAModelAuthMode, AIQAModelSettings
from models import AIQAModelAnswer, AIQAResult, AIQAUsage

AZURE_AI_TOKEN_SCOPE = "https://ai.azure.com/.default"
AI_QA_MODEL_INSTRUCTIONS = (
    "Answer the user's question directly and concisely. Provide a self-reported "
    "confidence from 0 to 1. Confidence is your assessment, not a calibrated "
    "probability."
)
StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)


class AIQAContextLimitError(ValueError):
    """Raised before a prompt that cannot fit the configured context budget."""


@dataclass(frozen=True)
class AIQAModelCallResult[ResultT: BaseModel]:
    """Typed output and usage from one independent model request."""

    output: ResultT
    usage: AIQAUsage


class _UsageAwareOpenAIResponsesModel(OpenAIResponsesModel):
    """Preserve usage for Azure deployment aliases unknown to price catalogs."""

    def _process_response(
        self,
        response: Response,
        model_settings: OpenAIResponsesModelSettings,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_response = super()._process_response(
            response,
            model_settings,
            model_request_parameters,
        )
        if response.usage is not None:
            model_response.usage = _map_response_usage(response.usage)
        return model_response


class AIQAService:
    """Run independent prompts through one configured PydanticAI agent."""

    def __init__(
        self,
        agent: Agent[None, AIQAModelAnswer],
        *,
        client: AsyncOpenAI,
        credential: DefaultAzureCredential | None,
        settings: AIQAModelSettings,
    ) -> None:
        self._agent = agent
        self._client = client
        self._credential = credential
        self.profile_name = settings.profile_name
        self.model_name = settings.model_name
        self.auth_mode = settings.auth_mode
        self.context_window_tokens = settings.context_window_tokens
        self.max_output_tokens = settings.max_output_tokens
        self.timeout_seconds = settings.timeout_seconds

    async def ask(self, question: str, *, debug: bool = False) -> AIQAResult:
        """Return one model answer without retaining conversation history."""
        if debug:
            logging.info(
                "AI Q&A debug model request: %s",
                json.dumps(
                    {
                        "label": "diagnostic",
                        "instructions": AI_QA_MODEL_INSTRUCTIONS,
                        "context": question,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        result = await self._agent.run(
            question,
            usage_limits=UsageLimits(
                request_limit=1,
                output_tokens_limit=self.max_output_tokens,
            ),
        )
        usage = result.usage
        response = AIQAResult(
            answer=result.output.answer,
            confidence=result.output.confidence,
            usage=_stable_usage(usage),
        )
        if debug:
            logging.info(
                "AI Q&A debug model response: %s",
                json.dumps(
                    {
                        "label": "diagnostic",
                        "response": response.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return response

    async def run_structured(
        self,
        output_type: type[StructuredOutputT],
        *,
        instructions: str,
        context: str,
        timeout_seconds: float,
        debug: bool,
        request_label: str,
    ) -> AIQAModelCallResult[StructuredOutputT]:
        """Run one stateless typed request with bounded time and token use."""
        estimated_input_tokens = _estimate_tokens(instructions, context)
        available_input_tokens = self.context_window_tokens - self.max_output_tokens
        if estimated_input_tokens > available_input_tokens:
            raise AIQAContextLimitError(
                "model instructions and context exceed the configured input budget"
            )

        if debug:
            logging.info(
                "AI Q&A debug model request: %s",
                json.dumps(
                    {
                        "label": request_label,
                        "instructions": instructions,
                        "context": context,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        agent: Agent[None, StructuredOutputT] = Agent(
            self._agent.model,
            output_type=NativeOutput(output_type),
            instructions=instructions,
            model_settings=OpenAIResponsesModelSettings(
                max_tokens=self.max_output_tokens,
                timeout=min(self.timeout_seconds, timeout_seconds),
                openai_store=False,
            ),
            retries=0,
        )
        async with asyncio.timeout(timeout_seconds):
            result = await agent.run(
                context,
                usage_limits=UsageLimits(
                    request_limit=1,
                    output_tokens_limit=self.max_output_tokens,
                ),
            )
        usage = _stable_usage(result.usage)
        output = result.output
        if debug:
            logging.info(
                "AI Q&A debug model response: %s",
                json.dumps(
                    {
                        "label": request_label,
                        "response": output.model_dump(mode="json"),
                        "usage": usage.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return AIQAModelCallResult(output=output, usage=usage)

    async def close(self) -> None:
        """Close owned SDK resources; primarily used by isolated tests."""
        await self._client.close()
        if self._credential is not None:
            self._credential.close()


def build_ai_qa_service(settings: AIQAModelSettings) -> AIQAService:
    """Build a model service for native OpenAI or compliant Azure identity."""
    credential: DefaultAzureCredential | None = None
    if settings.auth_mode is AIQAModelAuthMode.API_KEY:
        if not settings.api_key:  # Defensive; ServiceSettings enforces this.
            raise ValueError("Native OpenAI requires an API key")
        client = AsyncOpenAI(
            base_url=settings.endpoint_url,
            api_key=settings.api_key,
            max_retries=0,
        )
    else:
        if settings.api_key:  # Defensive compliance check.
            raise ValueError("Azure AI Foundry API keys are forbidden")
        credential = DefaultAzureCredential()
        sync_token_provider = get_bearer_token_provider(
            credential,
            AZURE_AI_TOKEN_SCOPE,
        )
        client = AsyncOpenAI(
            base_url=settings.endpoint_url,
            api_key=_async_token_provider(sync_token_provider),
            max_retries=0,
        )

    provider = OpenAIProvider(openai_client=client)
    model = _UsageAwareOpenAIResponsesModel(
        settings.asset_name,
        provider=provider,
        profile=openai_model_profile(settings.model_name),
    )
    agent: Agent[None, AIQAModelAnswer] = Agent(
        model,
        output_type=NativeOutput(AIQAModelAnswer),
        instructions=AI_QA_MODEL_INSTRUCTIONS,
        model_settings=OpenAIResponsesModelSettings(
            max_tokens=settings.max_output_tokens,
            timeout=settings.timeout_seconds,
            openai_store=False,
        ),
        retries=0,
    )
    return AIQAService(
        agent,
        client=client,
        credential=credential,
        settings=settings,
    )


def _async_token_provider(
    token_provider: Callable[[], str],
) -> Callable[[], Awaitable[str]]:
    async def get_token() -> str:
        return await asyncio.to_thread(token_provider)

    return get_token


class _UsageLike(Protocol):
    @property
    def requests(self) -> int: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def total_tokens(self) -> int: ...


def _stable_usage(usage: _UsageLike) -> AIQAUsage:
    return AIQAUsage(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
    )


def _estimate_tokens(*parts: str) -> int:
    """Use a conservative provider-neutral character estimate."""
    return (sum(len(part) for part in parts) + 2) // 3


def _map_response_usage(response_usage: ResponseUsage) -> RequestUsage:
    """Map the provider's Responses counters without model-name lookup."""
    input_details = response_usage.input_tokens_details
    output_details = response_usage.output_tokens_details
    return RequestUsage(
        input_tokens=response_usage.input_tokens,
        output_tokens=response_usage.output_tokens,
        cache_write_tokens=input_details.cache_write_tokens,
        cache_read_tokens=input_details.cached_tokens,
        details={"reasoning_tokens": output_details.reasoning_tokens},
    )
