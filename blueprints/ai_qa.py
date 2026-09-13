"""Function-protected, skill-routed Lytir Q&A endpoint."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from time import perf_counter
from typing import cast

import azure.functions as func
from azure.core.exceptions import AzureError
from azure.cosmos import CosmosClient
from pydantic import ValidationError

from config import AIQAModelSettings, ServiceSettings
from models import AI_QA_MAX_QUESTION_CHARACTERS, AIQADiagnosticRequest
from services import (
    AIQAService,
    EarthquakeQueryService,
    EarthquakeQueryUnavailableError,
    build_ai_qa_service,
)
from services.ai_qa import AIQAOrchestrationUnavailableError, AIQAOrchestrator
from services.ai_qa.availability import EarthquakeAvailabilityService
from services.ai_qa.orchestrator import AIQAOrchestratorConfig
from services.ai_qa.skills import AIQASkillRegistry, EarthquakeDataSkill
from services.earthquake_query_service import CosmosContainer

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]


@dataclass(frozen=True)
class _DataServices:
    availability: EarthquakeAvailabilityService
    skills: AIQASkillRegistry


@lru_cache(maxsize=2)
def _build_model_service(model_settings: AIQAModelSettings) -> AIQAService:
    return build_ai_qa_service(model_settings)


@lru_cache(maxsize=2)
def _build_data_services(
    connection_string: str,
    database_name: str,
    container_name: str,
) -> _DataServices:
    """Reuse the Cosmos connection pool and availability cache."""
    client = CosmosClient.from_connection_string(connection_string)
    container = client.get_database_client(database_name).get_container_client(
        container_name
    )
    earthquake_service = EarthquakeQueryService(
        cast(CosmosContainer, container), client=client
    )
    return _DataServices(
        availability=EarthquakeAvailabilityService(earthquake_service),
        skills=AIQASkillRegistry(EarthquakeDataSkill(earthquake_service)),
    )


@blueprint.route(
    route="ai/qa",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
async def answer_ai_qa(req: func.HttpRequest) -> func.HttpResponse:
    """Answer one stateless question through bounded skill orchestration."""
    started_at = perf_counter()
    request_started_utc = datetime.now(UTC)
    try:
        request = _parse_request(req)
    except ValueError as exc:
        return _error_response(f"Invalid request: {exc}", 400, request_started_utc)

    try:
        settings = ServiceSettings()
        model_settings = settings.ai_qa_model
        qa_settings = settings.ai_qa
    except ValueError:
        logging.exception("Invalid Lytir Q&A configuration")
        return _error_response("Lytir Q&A is not configured", 500, request_started_utc)

    try:
        model_service = _build_model_service(model_settings)
    except Exception:
        logging.exception("Could not initialize the configured AI Q&A model")
        return _error_response(
            "Lytir Q&A is temporarily unavailable", 503, request_started_utc
        )
    try:
        data_services = _build_data_services(
            settings.cosmos_earthquake_db_connection_string,
            settings.cosmos_earthquake_db_name,
            settings.cosmos_earthquake_data_container_name,
        )
    except AzureError:
        logging.exception("Could not initialize the earthquake data service")
        data_services = _unavailable_data_services()
    except (TypeError, ValueError):
        logging.exception("Invalid earthquake data configuration for Lytir Q&A")
        return _error_response("Lytir Q&A is not configured", 500, request_started_utc)

    orchestrator = AIQAOrchestrator(
        model_service,
        data_services.availability,
        data_services.skills,
        AIQAOrchestratorConfig(
            available_from_utc=qa_settings.earthquake_data_available_from_utc,
            max_window_hours=settings.earthquake_query_max_window_hours,
            max_radius_km=settings.earthquake_query_max_radius_km,
            max_model_requests=qa_settings.max_model_requests,
            max_tool_calls=qa_settings.max_tool_calls,
            max_execution_seconds=qa_settings.max_execution_seconds,
            debug=qa_settings.debug,
        ),
    )
    try:
        result = await orchestrator.answer(request.question, now=request_started_utc)
    except AIQAOrchestrationUnavailableError:
        logging.exception("A required Lytir Q&A dependency was unavailable")
        return _error_response(
            "Lytir Q&A is temporarily unavailable", 503, request_started_utc
        )
    except TimeoutError:
        logging.exception("Lytir Q&A exceeded its execution deadline")
        return _error_response("Lytir Q&A timed out", 503, request_started_utc)
    except Exception:
        logging.exception("Unexpected Lytir Q&A failure")
        return _error_response("Lytir Q&A failed", 500, request_started_utc)

    payload: dict[str, object] = {
        "question": request.question,
        "outcome": result.outcome,
        "answer": result.answer,
        "answer_format": "markdown",
        "execution_seconds": round(perf_counter() - started_at, 6),
        "model": {
            "host": model_settings.model_host,
            "profile": model_settings.profile_name,
            "name": model_settings.model_name,
        },
        "utc_now": _format_utc(datetime.now(UTC)),
    }
    if result.confidence is not None:
        payload["confidence"] = result.confidence
    if result.data_status is not None:
        payload["data_status"] = result.data_status
    if result.knowledge_source is not None:
        payload["knowledge_source"] = result.knowledge_source
        payload["events"] = [event.model_dump(mode="json") for event in result.events]
    if qa_settings.debug:
        payload["skill"] = result.skill
        payload["usage"] = result.usage.model_dump(mode="json")
        if result.data_context is not None:
            payload["data_context"] = result.data_context
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=200,
        mimetype="application/json",
    )


def _parse_request(req: func.HttpRequest) -> AIQADiagnosticRequest:
    try:
        payload = req.get_json()
    except (TypeError, ValueError) as exc:
        raise ValueError("body must be valid JSON") from exc
    try:
        return AIQADiagnosticRequest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            "body must contain only a non-empty question of at most "
            f"{AI_QA_MAX_QUESTION_CHARACTERS} characters"
        ) from exc


def _error_response(message: str, status_code: int, now: datetime) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": message, "utc_now": _format_utc(now)}),
        status_code=status_code,
        mimetype="application/json",
    )


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _UnavailableEarthquakeService:
    """Keep non-data routes available when Cosmos initialization fails."""

    def latest_observed_at(self) -> datetime | None:
        raise EarthquakeQueryUnavailableError("earthquake data is unavailable")

    def query(self, _query: object) -> None:
        raise EarthquakeQueryUnavailableError("earthquake data is unavailable")


def _unavailable_data_services() -> _DataServices:
    unavailable = cast(EarthquakeQueryService, _UnavailableEarthquakeService())
    return _DataServices(
        availability=EarthquakeAvailabilityService(unavailable),
        skills=AIQASkillRegistry(EarthquakeDataSkill(unavailable)),
    )
