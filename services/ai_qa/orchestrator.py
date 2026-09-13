"""Bounded, skill-routed orchestration for one stateless Lytir Q&A request."""

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from models import (
    AIQADataStatus,
    AIQAEarthquakeDataRequest,
    AIQAGroundedDecision,
    AIQAGroundedOutcome,
    AIQAKnowledgeSource,
    AIQAModelKnowledgeEvent,
    AIQAOutOfRangeAnswer,
    AIQAResponseOutcome,
    AIQARouterDecision,
    AIQARouterOutcome,
    AIQASkillName,
    AIQAUsage,
    EarthquakeQuery,
)
from services.ai_qa.availability import EarthquakeAvailabilityService
from services.ai_qa.prompt_builders import (
    FINAL_INSTRUCTIONS,
    GROUNDED_INSTRUCTIONS,
    OUT_OF_RANGE_INSTRUCTIONS,
    ROUTER_INSTRUCTIONS,
    build_grounded_context,
    build_out_of_range_context,
    build_router_context,
)
from services.ai_qa.skills import AIQASkillRegistry, EarthquakeEvidence
from services.ai_qa_service import (
    AIQAContextLimitError,
    AIQAModelCallResult,
    AIQAService,
)
from services.earthquake_query_service import EarthquakeQueryUnavailableError

DEFAULT_DATA_WINDOW_HOURS = 168
MIN_MODEL_TIME_SECONDS = 0.1
AVAILABILITY_LOOKUP_TIMEOUT_SECONDS = 5.0


class AIQAOrchestrationUnavailableError(RuntimeError):
    """Raised when a required provider or earthquake operation is unavailable."""


@dataclass(frozen=True)
class AIQAOrchestratorConfig:
    """Validated application limits for one request."""

    available_from_utc: datetime
    max_window_hours: int
    max_radius_km: float
    max_model_requests: int
    max_tool_calls: int
    max_execution_seconds: int
    debug: bool


@dataclass(frozen=True)
class AIQAOrchestratorResult:
    """Provider-neutral response assembled by the Q&A endpoint."""

    outcome: AIQAResponseOutcome
    skill: AIQASkillName
    answer: str
    confidence: float | None
    usage: AIQAUsage
    data_context: dict[str, object] | None = None
    data_status: AIQADataStatus | None = None
    knowledge_source: AIQAKnowledgeSource | None = None
    events: tuple[AIQAModelKnowledgeEvent, ...] = ()


@dataclass
class _RunState:
    started_at: float
    usage: AIQAUsage = field(
        default_factory=lambda: AIQAUsage(
            requests=0,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )
    )
    model_requests: int = 0
    tool_calls: int = 0
    query_contexts: list[dict[str, object]] = field(default_factory=list)
    evidence_blocks: list[str] = field(default_factory=list)
    matched_records: int = 0
    partial_coverage: bool = False
    fingerprints: set[str] = field(default_factory=set)


class _ClarificationRequired(ValueError):
    pass


class _OutsideAvailableRange(ValueError):
    def __init__(self, start: datetime, end: datetime) -> None:
        self.start = start
        self.end = end


class AIQAOrchestrator:
    """Route, retrieve, and ground one question within fixed budgets."""

    def __init__(
        self,
        model_service: AIQAService,
        availability_service: EarthquakeAvailabilityService,
        skill_registry: AIQASkillRegistry,
        config: AIQAOrchestratorConfig,
    ) -> None:
        self._model = model_service
        self._availability = availability_service
        self._skills = skill_registry
        self._config = config

    async def answer(
        self, question: str, *, now: datetime | None = None
    ) -> AIQAOrchestratorResult:
        state = _RunState(started_at=perf_counter())
        effective_now = (now or datetime.now(UTC)).astimezone(UTC)

        availability_error = False
        availability_started = perf_counter()
        try:
            availability = await asyncio.wait_for(
                self._availability.get(),
                timeout=min(
                    self._remaining(state), AVAILABILITY_LOOKUP_TIMEOUT_SECONDS
                ),
            )
            latest = availability.latest_observed_at_utc
            cache_hit = availability.cache_hit
        except (EarthquakeQueryUnavailableError, TimeoutError):
            latest = None
            cache_hit = False
            availability_error = True
        availability_seconds = perf_counter() - availability_started
        availability_context = self._availability_context(
            effective_now, latest, available=not availability_error
        )

        router_context = build_router_context(
            question=question,
            availability=availability_context,
            max_window_hours=self._config.max_window_hours,
            max_radius_km=self._config.max_radius_km,
        )
        router = await self._model_call(
            AIQARouterDecision,
            instructions=ROUTER_INSTRUCTIONS,
            context=router_context,
            label="router-v1",
            state=state,
        )
        decision = router.output

        if decision.outcome is AIQARouterOutcome.ANSWERED:
            assert decision.answer is not None
            assert decision.confidence is not None
            result = AIQAOrchestratorResult(
                outcome=AIQAResponseOutcome.ANSWERED,
                skill=decision.skill,
                answer=decision.answer,
                confidence=decision.confidence,
                usage=state.usage,
            )
            self._log_completion(
                result, state, availability_seconds, cache_hit=cache_hit
            )
            return result

        if decision.outcome is AIQARouterOutcome.CLARIFICATION_REQUIRED:
            assert decision.clarification is not None
            result = self._clarification(
                decision.clarification,
                state,
                availability_context,
            )
            self._log_completion(
                result, state, availability_seconds, cache_hit=cache_hit
            )
            return result

        if availability_error:
            raise AIQAOrchestrationUnavailableError(
                "earthquake availability could not be loaded"
            )

        assert decision.data_request is not None
        next_request = decision.data_request
        stop_reason: str | None = None

        while True:
            if state.tool_calls >= self._config.max_tool_calls:
                stop_reason = "tool_call_budget_exhausted"
                return await self._finalize(
                    question,
                    availability_context,
                    state,
                    availability_seconds,
                    cache_hit,
                    stop_reason,
                )

            state.tool_calls += 1
            try:
                query, query_context = self._validate_data_request(
                    next_request,
                    now=effective_now,
                    latest=latest,
                )
            except _OutsideAvailableRange as outside_range:
                return await self._answer_outside_available_range(
                    question=question,
                    data_request=next_request,
                    requested_start=outside_range.start,
                    requested_end=outside_range.end,
                    availability=availability_context,
                    latest=latest,
                    state=state,
                    availability_seconds=availability_seconds,
                    cache_hit=cache_hit,
                )
            except _ClarificationRequired as exc:
                result = self._clarification(str(exc), state, availability_context)
                self._log_completion(
                    result, state, availability_seconds, cache_hit=cache_hit
                )
                return result

            if query_context.get("data_status") == AIQADataStatus.PARTIAL_COVERAGE:
                state.partial_coverage = True

            fingerprint = _query_fingerprint(query)
            if fingerprint in state.fingerprints:
                stop_reason = "duplicate_data_request"
                return await self._finalize(
                    question,
                    availability_context,
                    state,
                    availability_seconds,
                    cache_hit,
                    stop_reason,
                )
            state.fingerprints.add(fingerprint)

            query_started = perf_counter()
            try:
                evidence = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._skills.get_earthquake_data(
                            AIQASkillName.LYTIR_DATA
                        ).execute,
                        query,
                    ),
                    timeout=self._remaining(state),
                )
            except EarthquakeQueryUnavailableError as exc:
                raise AIQAOrchestrationUnavailableError(
                    "earthquake data could not be retrieved"
                ) from exc
            query_seconds = perf_counter() - query_started
            state.query_contexts.append(query_context)
            state.evidence_blocks.append(evidence.text)
            state.matched_records += evidence.record_count
            _log_evidence(evidence, query, query_seconds)

            final_request = (
                state.model_requests >= self._config.max_model_requests - 1
                or state.tool_calls >= self._config.max_tool_calls
            )
            instructions = GROUNDED_INSTRUCTIONS
            if final_request:
                instructions = f"{instructions}\n\n{FINAL_INSTRUCTIONS}"
            grounded_context = build_grounded_context(
                question=question,
                availability=availability_context,
                query_contexts=state.query_contexts,
                evidence_blocks=state.evidence_blocks,
                remaining_model_requests=(
                    self._config.max_model_requests - state.model_requests
                ),
                remaining_tool_calls=(self._config.max_tool_calls - state.tool_calls),
                stop_reason=("final_request_reserved" if final_request else None),
            )
            try:
                grounded = await self._model_call(
                    AIQAGroundedDecision,
                    instructions=instructions,
                    context=grounded_context,
                    label="grounded-decision-v1-final"
                    if final_request
                    else "grounded-decision-v1",
                    state=state,
                )
            except AIQAContextLimitError:
                return await self._finalize(
                    question,
                    availability_context,
                    state,
                    availability_seconds,
                    cache_hit,
                    "evidence_context_budget_exhausted",
                    omit_evidence=True,
                )

            grounded_decision = grounded.output
            if grounded_decision.outcome is AIQAGroundedOutcome.FINAL_ANSWER:
                assert grounded_decision.answer is not None
                assert grounded_decision.confidence is not None
                outcome = (
                    AIQAResponseOutcome.INCOMPLETE
                    if stop_reason
                    else AIQAResponseOutcome.ANSWERED
                )
                result = AIQAOrchestratorResult(
                    outcome=outcome,
                    skill=AIQASkillName.LYTIR_DATA,
                    answer=grounded_decision.answer,
                    confidence=grounded_decision.confidence,
                    usage=state.usage,
                    data_context=self._data_context(
                        state, latest, stop_reason=stop_reason
                    ),
                    data_status=(
                        AIQADataStatus.PARTIAL_COVERAGE
                        if state.partial_coverage
                        else None
                    ),
                )
                self._log_completion(
                    result, state, availability_seconds, cache_hit=cache_hit
                )
                return result
            if grounded_decision.outcome is AIQAGroundedOutcome.CLARIFICATION_REQUIRED:
                assert grounded_decision.clarification is not None
                result = self._clarification(
                    grounded_decision.clarification,
                    state,
                    availability_context,
                )
                self._log_completion(
                    result, state, availability_seconds, cache_hit=cache_hit
                )
                return result
            if final_request:
                result = self._incomplete_without_model(
                    state,
                    latest,
                    "model_requested_data_on_final_request",
                )
                self._log_completion(
                    result, state, availability_seconds, cache_hit=cache_hit
                )
                return result
            assert grounded_decision.data_request is not None
            next_request = grounded_decision.data_request

    async def _finalize(
        self,
        question: str,
        availability: dict[str, object],
        state: _RunState,
        availability_seconds: float,
        cache_hit: bool,
        stop_reason: str,
        *,
        omit_evidence: bool = False,
    ) -> AIQAOrchestratorResult:
        if state.model_requests >= self._config.max_model_requests:
            result = self._incomplete_without_model(state, None, stop_reason)
            self._log_completion(
                result, state, availability_seconds, cache_hit=cache_hit
            )
            return result
        context = build_grounded_context(
            question=question,
            availability=availability,
            query_contexts=state.query_contexts,
            evidence_blocks=[] if omit_evidence else state.evidence_blocks,
            remaining_model_requests=1,
            remaining_tool_calls=0,
            stop_reason=stop_reason,
        )
        try:
            call = await self._model_call(
                AIQAGroundedDecision,
                instructions=f"{GROUNDED_INSTRUCTIONS}\n\n{FINAL_INSTRUCTIONS}",
                context=context,
                label="grounded-decision-v1-final",
                state=state,
            )
        except (AIQAContextLimitError, TimeoutError):
            result = self._incomplete_without_model(state, None, stop_reason)
        else:
            decision = call.output
            if decision.outcome is not AIQAGroundedOutcome.FINAL_ANSWER:
                result = self._incomplete_without_model(state, None, stop_reason)
            else:
                assert decision.answer is not None
                assert decision.confidence is not None
                result = AIQAOrchestratorResult(
                    outcome=AIQAResponseOutcome.INCOMPLETE,
                    skill=AIQASkillName.LYTIR_DATA,
                    answer=decision.answer,
                    confidence=decision.confidence,
                    usage=state.usage,
                    data_context=self._data_context(
                        state, None, stop_reason=stop_reason
                    ),
                    data_status=(
                        AIQADataStatus.PARTIAL_COVERAGE
                        if state.partial_coverage
                        else None
                    ),
                )
        self._log_completion(result, state, availability_seconds, cache_hit=cache_hit)
        return result

    async def _answer_outside_available_range(
        self,
        *,
        question: str,
        data_request: AIQAEarthquakeDataRequest,
        requested_start: datetime,
        requested_end: datetime,
        availability: dict[str, object],
        latest: datetime | None,
        state: _RunState,
        availability_seconds: float,
        cache_hit: bool,
    ) -> AIQAOrchestratorResult:
        call = await self._model_call(
            AIQAOutOfRangeAnswer,
            instructions=OUT_OF_RANGE_INSTRUCTIONS,
            context=build_out_of_range_context(
                question=question,
                availability=availability,
                data_request=data_request,
                requested_start_utc=_format_utc(requested_start),
                requested_end_utc=_format_utc(requested_end),
            ),
            label="out-of-range-v1",
            state=state,
        )
        data_context = self._data_context(state, latest, stop_reason=None)
        data_context.update(
            {
                "data_status": AIQADataStatus.OUTSIDE_AVAILABLE_RANGE,
                "requested_start_utc": _format_utc(requested_start),
                "requested_end_utc": _format_utc(requested_end),
                "available_from_utc": _format_utc(self._config.available_from_utc),
            }
        )
        result = AIQAOrchestratorResult(
            outcome=AIQAResponseOutcome.ANSWERED,
            skill=AIQASkillName.LYTIR_DATA,
            answer=call.output.answer,
            confidence=call.output.confidence,
            usage=state.usage,
            data_context=data_context,
            data_status=AIQADataStatus.OUTSIDE_AVAILABLE_RANGE,
            knowledge_source=AIQAKnowledgeSource.MODEL_KNOWLEDGE,
            events=tuple(call.output.events),
        )
        self._log_completion(result, state, availability_seconds, cache_hit=cache_hit)
        return result

    async def _model_call[DecisionT: BaseModel](
        self,
        output_type: type[DecisionT],
        *,
        instructions: str,
        context: str,
        label: str,
        state: _RunState,
    ) -> AIQAModelCallResult[DecisionT]:
        if state.model_requests >= self._config.max_model_requests:
            raise AIQAOrchestrationUnavailableError("model request budget exhausted")
        remaining = self._remaining(state)
        state.model_requests += 1
        try:
            call = await self._model.run_structured(
                output_type,
                instructions=instructions,
                context=context,
                timeout_seconds=remaining,
                debug=self._config.debug,
                request_label=label,
            )
        except AIQAContextLimitError:
            state.model_requests -= 1
            raise
        except TimeoutError:
            raise
        except Exception as exc:
            if self._config.debug:
                logging.exception("AI Q&A debug model failure: label=%s", label)
            else:
                logging.error(
                    "AI Q&A model request failed: label=%s error_type=%s",
                    label,
                    type(exc).__name__,
                )
            raise AIQAOrchestrationUnavailableError(
                "the configured model request failed"
            ) from None
        state.usage = _add_usage(state.usage, call.usage)
        return call

    def _validate_data_request(
        self,
        request: AIQAEarthquakeDataRequest,
        *,
        now: datetime,
        latest: datetime | None,
    ) -> tuple[EarthquakeQuery, dict[str, object]]:
        if request.time_range.timezone:
            try:
                ZoneInfo(request.time_range.timezone)
            except ZoneInfoNotFoundError as exc:
                raise _ClarificationRequired(
                    "Please clarify the location or timezone for the requested period."
                ) from exc

        if request.time_range.mode == "default":
            requested_end = now
            requested_start = max(
                now - timedelta(hours=DEFAULT_DATA_WINDOW_HOURS),
                self._config.available_from_utc,
            )
        else:
            assert request.time_range.start_utc is not None
            assert request.time_range.end_utc is not None
            requested_start = _aware_utc(request.time_range.start_utc, "start time")
            requested_end = _aware_utc(request.time_range.end_utc, "end time")

        if requested_start >= requested_end:
            raise _ClarificationRequired(
                "Please provide a time range whose start is earlier than its end."
            )
        if requested_end <= self._config.available_from_utc:
            raise _OutsideAvailableRange(requested_start, requested_end)
        if requested_start >= now:
            raise _ClarificationRequired(
                "The requested time range is entirely in the future. Please use "
                f"a start time earlier than {_format_utc(now)}."
            )

        start = max(requested_start, self._config.available_from_utc)
        end = min(requested_end, now)
        partial_coverage = start != requested_start or end != requested_end

        if (end - start).total_seconds() > self._config.max_window_hours * 3600:
            raise _ClarificationRequired(
                "Please narrow the time range to no more than "
                f"{self._config.max_window_hours} hours."
            )

        latitude = request.latitude
        longitude = request.longitude
        if (latitude is None) != (longitude is None):
            raise _ClarificationRequired(
                "Please clarify the location so both latitude and longitude can be determined."
            )
        if request.location_name and latitude is None:
            raise _ClarificationRequired(
                f"Please clarify which {request.location_name} location you mean."
            )
        if latitude is not None and not -90 <= latitude <= 90:
            raise _ClarificationRequired("Please clarify the requested latitude.")
        if longitude is not None and not -180 <= longitude <= 180:
            raise _ClarificationRequired("Please clarify the requested longitude.")
        if latitude is None:
            if request.radius_km is not None:
                raise _ClarificationRequired(
                    "Please provide a location for the requested radius."
                )
            radius_km = None
        else:
            radius_km = 5.0 if request.radius_km is None else request.radius_km
            if radius_km <= 0 or radius_km > self._config.max_radius_km:
                raise _ClarificationRequired(
                    "Please use a positive radius no greater than "
                    f"{self._config.max_radius_km:g} km."
                )

        if request.min_magnitude is not None and not math.isfinite(
            request.min_magnitude
        ):
            raise _ClarificationRequired("Please clarify the minimum magnitude.")
        if request.max_magnitude is not None and not math.isfinite(
            request.max_magnitude
        ):
            raise _ClarificationRequired("Please clarify the maximum magnitude.")
        if (
            request.min_magnitude is not None
            and request.max_magnitude is not None
            and request.min_magnitude > request.max_magnitude
        ):
            raise _ClarificationRequired(
                "Please use a minimum magnitude no greater than the maximum magnitude."
            )

        query = EarthquakeQuery(
            start_time=start,
            end_time=end,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            min_magnitude=request.min_magnitude,
            max_magnitude=request.max_magnitude,
        )
        query_context: dict[str, object] = {
            "time_range_mode": request.time_range.mode,
            "start_utc": _format_utc(start),
            "end_utc": _format_utc(end),
            "location_name": request.location_name,
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": radius_km,
            "location_confidence": request.location_confidence,
            "timezone": request.time_range.timezone,
            "min_magnitude": request.min_magnitude,
            "max_magnitude": request.max_magnitude,
            "latest_observed_at_utc": None if latest is None else _format_utc(latest),
        }
        if partial_coverage:
            query_context.update(
                {
                    "data_status": AIQADataStatus.PARTIAL_COVERAGE,
                    "requested_range": {
                        "start_utc": _format_utc(requested_start),
                        "end_utc": _format_utc(requested_end),
                    },
                    "effective_range": {
                        "start_utc": _format_utc(start),
                        "end_utc": _format_utc(end),
                    },
                    "available_from_utc": _format_utc(self._config.available_from_utc),
                    "utc_now": _format_utc(now),
                }
            )
        return query, query_context

    def _remaining(self, state: _RunState) -> float:
        remaining = self._config.max_execution_seconds - (
            perf_counter() - state.started_at
        )
        if remaining < MIN_MODEL_TIME_SECONDS:
            raise TimeoutError("AI Q&A execution deadline was reached")
        return remaining

    def _availability_context(
        self,
        now: datetime,
        latest: datetime | None,
        *,
        available: bool,
    ) -> dict[str, object]:
        return {
            "utc_now": _format_utc(now),
            "earthquake_data": {
                "available_from_utc": _format_utc(self._config.available_from_utc),
                "latest_observed_at_utc": None
                if latest is None
                else _format_utc(latest),
                "latest_lookup_available": available,
                "default_window_hours": DEFAULT_DATA_WINDOW_HOURS,
                "max_window_hours": self._config.max_window_hours,
                "max_radius_km": self._config.max_radius_km,
                "continuous_coverage_guaranteed": False,
            },
        }

    def _clarification(
        self,
        answer: str,
        state: _RunState,
        availability: dict[str, object],
    ) -> AIQAOrchestratorResult:
        earthquake_data = availability["earthquake_data"]
        assert isinstance(earthquake_data, dict)
        return AIQAOrchestratorResult(
            outcome=AIQAResponseOutcome.CLARIFICATION_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            answer=answer,
            confidence=None,
            usage=state.usage,
            data_context=dict(earthquake_data),
        )

    def _data_context(
        self,
        state: _RunState,
        latest: datetime | None,
        *,
        stop_reason: str | None,
    ) -> dict[str, object]:
        context: dict[str, object] = {
            "queries": state.query_contexts,
            "matched_records": state.matched_records,
            "tool_calls": state.tool_calls,
            "latest_observed_at_utc": None if latest is None else _format_utc(latest),
        }
        if stop_reason:
            context["stop_reason"] = stop_reason
        if state.partial_coverage:
            context["data_status"] = AIQADataStatus.PARTIAL_COVERAGE
        return context

    def _incomplete_without_model(
        self,
        state: _RunState,
        latest: datetime | None,
        stop_reason: str,
    ) -> AIQAOrchestratorResult:
        return AIQAOrchestratorResult(
            outcome=AIQAResponseOutcome.INCOMPLETE,
            skill=AIQASkillName.LYTIR_DATA,
            answer=(
                "The execution budget ended before I could complete the answer. "
                "Please narrow the time range or location and try again."
            ),
            confidence=0.0,
            usage=state.usage,
            data_context=self._data_context(state, latest, stop_reason=stop_reason),
            data_status=(
                AIQADataStatus.PARTIAL_COVERAGE if state.partial_coverage else None
            ),
        )

    def _log_completion(
        self,
        result: AIQAOrchestratorResult,
        state: _RunState,
        availability_seconds: float,
        *,
        cache_hit: bool,
    ) -> None:
        logging.info(
            "AI Q&A completed: outcome=%s skill=%s model_requests=%d "
            "tool_calls=%d input_tokens=%d output_tokens=%d matched_records=%d "
            "availability_seconds=%.6f availability_cache_hit=%s total_seconds=%.6f",
            result.outcome,
            result.skill,
            state.model_requests,
            state.tool_calls,
            state.usage.input_tokens,
            state.usage.output_tokens,
            state.matched_records,
            availability_seconds,
            cache_hit,
            perf_counter() - state.started_at,
        )


def _add_usage(left: AIQAUsage, right: AIQAUsage) -> AIQAUsage:
    return AIQAUsage(
        requests=left.requests + right.requests,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _ClarificationRequired(f"Please provide {name} with a timezone.")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _query_fingerprint(query: EarthquakeQuery) -> str:
    payload = {
        "start": _format_utc(query.start_time),
        "end": _format_utc(query.end_time),
        "latitude": query.latitude,
        "longitude": query.longitude,
        "radius_km": query.radius_km,
        "min_magnitude": query.min_magnitude,
        "max_magnitude": query.max_magnitude,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _log_evidence(
    evidence: EarthquakeEvidence,
    query: EarthquakeQuery,
    query_seconds: float,
) -> None:
    result = evidence.query_result
    logging.info(
        "AI Q&A earthquake skill completed: candidates=%d duplicates=%d "
        "selected=%d matched=%d evidence_characters=%d window_hours=%.3f "
        "query_seconds=%.6f",
        result.candidates,
        result.duplicates,
        result.selected,
        evidence.record_count,
        evidence.character_count,
        (query.end_time - query.start_time).total_seconds() / 3600,
        query_seconds,
    )
