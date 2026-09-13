"""Function-protected diagnostic endpoint for configured AI model access."""

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from time import perf_counter

import azure.functions as func
from pydantic import ValidationError

from config import AIQAModelSettings, ServiceSettings
from models import (
    AI_QA_MAX_QUESTION_CHARACTERS,
    AIQADiagnosticRequest,
    AIQADiagnosticResponse,
)
from services import AIQAService, build_ai_qa_service

blueprint = func.Blueprint()  # type: ignore[no-untyped-call]


@lru_cache(maxsize=2)
def _build_service(settings: AIQAModelSettings) -> AIQAService:
    """Reuse the selected model client across warm Function App invocations."""
    return build_ai_qa_service(settings)


@blueprint.route(
    route="diag/ai-qa",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
async def diagnose_ai_qa(req: func.HttpRequest) -> func.HttpResponse:
    """Ask one generic question to verify configured model connectivity."""
    started_at = perf_counter()
    now = datetime.now(UTC)
    try:
        request = _parse_request(req)
    except ValueError as exc:
        return _error_response(f"Invalid request: {exc}", 400, now)

    try:
        settings = ServiceSettings()
        model_settings = settings.ai_qa_model
    except ValueError:
        logging.exception("Invalid AI Q&A model configuration")
        return _error_response("AI Q&A model is not configured", 500, now)

    try:
        service = _build_service(model_settings)
        result = await service.ask(request.question, debug=settings.debug_mode)
    except Exception:
        logging.exception(
            "AI Q&A diagnostic request failed: profile=%s model=%s auth=%s",
            model_settings.profile_name,
            model_settings.model_name,
            model_settings.auth_mode,
        )
        return _error_response("AI Q&A model is temporarily unavailable", 503, now)

    logging.info(
        "AI Q&A diagnostic request succeeded: profile=%s model=%s auth=%s",
        model_settings.profile_name,
        model_settings.model_name,
        model_settings.auth_mode,
    )
    response = AIQADiagnosticResponse(
        question=request.question,
        answer=result.answer,
        confidence=result.confidence,
        usage=result.usage,
        execution_seconds=round(perf_counter() - started_at, 6),
        model_host=model_settings.model_host,
        model_profile=model_settings.profile_name,
        model_name=model_settings.model_name,
        utc_now=now,
    )
    return func.HttpResponse(
        body=response.model_dump_json(),
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
        body=json.dumps({"error": message, "utc_now": now.isoformat()}),
        status_code=status_code,
        mimetype="application/json",
    )
