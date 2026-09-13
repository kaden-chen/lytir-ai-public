import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
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
    AIQATimeRange,
    AIQAUsage,
    EarthquakeQuery,
)
from services import AIQAService, EarthquakeQueryResult
from services.ai_qa.availability import (
    EarthquakeAvailability,
    EarthquakeAvailabilityService,
)
from services.ai_qa.orchestrator import (
    AIQAOrchestrationUnavailableError,
    AIQAOrchestrator,
    AIQAOrchestratorConfig,
)
from services.ai_qa.skills import AIQASkillRegistry, EarthquakeEvidence
from services.ai_qa_service import AIQAModelCallResult
from services.earthquake_query_service import EarthquakeQueryUnavailableError

NOW = datetime(2026, 9, 12, 2, tzinfo=UTC)
AVAILABLE_FROM = datetime(2026, 9, 11, 17, tzinfo=UTC)


class FakeModelService:
    def __init__(self, *outputs: BaseModel) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def run_structured[OutputT: BaseModel](
        self,
        output_type: type[OutputT],
        **kwargs: Any,
    ) -> AIQAModelCallResult[OutputT]:
        self.calls.append({"output_type": output_type, **kwargs})
        output = self.outputs.pop(0)
        assert isinstance(output, output_type)
        return AIQAModelCallResult(
            output=output,
            usage=AIQAUsage(
                requests=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
            ),
        )


class FakeAvailabilityService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def get(self) -> EarthquakeAvailability:
        if self.fail:
            raise EarthquakeQueryUnavailableError("unavailable")
        return EarthquakeAvailability(
            datetime(2026, 9, 12, 1, 55, tzinfo=UTC), cache_hit=False
        )


class FakeEarthquakeSkill:
    def __init__(self, *, count: int = 1) -> None:
        self.count = count
        self.queries: list[EarthquakeQuery] = []

    def execute(self, query: EarthquakeQuery) -> EarthquakeEvidence:
        self.queries.append(query)
        items = [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "event_type": "earthquake",
                "magnitude": 2.4,
                "place": "10 km NW of Example",
                "occurred_at_utc": "2026-09-12T01:30:00Z",
                "updated_at_utc": "2026-09-12T01:35:00Z",
                "longitude": -96.8,
                "latitude": 32.8,
                "depth_km": 4.2,
            }
        ][: self.count]
        result = EarthquakeQueryResult(
            items=items,  # type: ignore[arg-type]
            candidates=len(items),
            malformed=0,
            duplicates=0,
            selected=len(items),
        )
        return EarthquakeEvidence(
            text="2026-09-12T01:30:00Z | M2.4 | Example | 32.8,-96.8 | depth 4.2 km",
            record_count=len(items),
            character_count=70,
            query_result=result,
        )


class FakeRegistry:
    def __init__(self, skill: FakeEarthquakeSkill) -> None:
        self.skill = skill

    def get_earthquake_data(self, name: AIQASkillName) -> FakeEarthquakeSkill:
        assert name is AIQASkillName.LYTIR_DATA
        return self.skill


def config(*, debug: bool = False) -> AIQAOrchestratorConfig:
    return AIQAOrchestratorConfig(
        available_from_utc=AVAILABLE_FROM,
        max_window_hours=720,
        max_radius_km=1000,
        max_model_requests=5,
        max_tool_calls=3,
        max_execution_seconds=210,
        debug=debug,
    )


def orchestrator(
    model: FakeModelService,
    skill: FakeEarthquakeSkill,
    *,
    availability_fail: bool = False,
) -> AIQAOrchestrator:
    return AIQAOrchestrator(
        cast(AIQAService, model),
        cast(
            EarthquakeAvailabilityService,
            FakeAvailabilityService(fail=availability_fail),
        ),
        cast(AIQASkillRegistry, FakeRegistry(skill)),
        config(),
    )


def data_request(**overrides: object) -> AIQAEarthquakeDataRequest:
    values: dict[str, object] = {
        "time_range": AIQATimeRange(mode="default"),
        "location_name": "Dallas, Texas",
        "latitude": 32.7767,
        "longitude": -96.797,
        "radius_km": 80,
        "location_confidence": 0.99,
    }
    values.update(overrides)
    return AIQAEarthquakeDataRequest.model_validate(values)


def test_orchestrator_returns_general_answer_without_earthquake_data() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.ANSWERED,
            skill=AIQASkillName.GENERAL,
            answer="Tokyo is the capital of Japan.",
            confidence=0.99,
        )
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(orchestrator(model, skill).answer("Capital?", now=NOW))

    assert result.outcome is AIQAResponseOutcome.ANSWERED
    assert result.skill is AIQASkillName.GENERAL
    assert result.usage.requests == 1
    assert skill.queries == []


def test_orchestrator_answers_data_question_with_default_window() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(),
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer="Yes. One matching earthquake was observed.",
            confidence=0.94,
        ),
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(
        orchestrator(model, skill).answer("Any earthquakes near Dallas?", now=NOW)
    )

    assert result.outcome is AIQAResponseOutcome.ANSWERED
    assert result.skill is AIQASkillName.LYTIR_DATA
    assert result.usage.requests == 2
    assert len(skill.queries) == 1
    assert skill.queries[0].start_time == AVAILABLE_FROM
    assert skill.queries[0].end_time == NOW
    assert skill.queries[0].radius_km == 80
    assert result.data_context is not None
    assert result.data_context["matched_records"] == 1
    query_contexts = result.data_context["queries"]
    assert isinstance(query_contexts, list)
    assert query_contexts[0]["time_range_mode"] == "default"


def test_default_window_prompt_allows_labeled_history_for_unbounded_question() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(),
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer=(
                "Yes. Lytir found recent events. General historical knowledge, "
                "not verified by Lytir, also indicates earlier events."
            ),
            confidence=0.9,
        ),
    )

    result = asyncio.run(
        orchestrator(model, FakeEarthquakeSkill()).answer(
            "Has there ever been an earthquake near Dallas?", now=NOW
        )
    )

    assert result.outcome is AIQAResponseOutcome.ANSWERED
    instructions = model.calls[1]["instructions"]
    assert "clearly temporally unbounded" in instructions
    assert "historical facts from general model knowledge" in instructions
    assert "not verified by Lytir" in instructions
    assert "even when the recent evidence already answers" in instructions


def test_orchestrator_returns_clarification_for_invalid_model_radius() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(radius_km=1001),
        )
    )

    result = asyncio.run(
        orchestrator(model, FakeEarthquakeSkill()).answer("Nearby?", now=NOW)
    )

    assert result.outcome is AIQAResponseOutcome.CLARIFICATION_REQUIRED
    assert "no greater than 1000 km" in result.answer
    assert result.confidence is None


def test_orchestrator_uses_model_knowledge_for_unavailable_historical_range() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(
                time_range=AIQATimeRange(
                    mode="explicit",
                    start_utc=datetime(2000, 1, 1, tzinfo=UTC),
                    end_utc=datetime(2001, 1, 1, tzinfo=UTC),
                    timezone="America/Los_Angeles",
                )
            ),
        ),
        AIQAOutOfRangeAnswer(
            answer=(
                "Lytir cannot verify 2000 from its observations. General historical "
                "knowledge indicates that California experienced earthquakes."
            ),
            confidence=0.8,
            events=[
                AIQAModelKnowledgeEvent(
                    event_type="earthquake",
                    place="California",
                )
            ],
        ),
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(
        orchestrator(model, skill).answer(
            "Were there earthquakes in California in 2000?", now=NOW
        )
    )

    assert result.outcome is AIQAResponseOutcome.ANSWERED
    assert result.data_status is AIQADataStatus.OUTSIDE_AVAILABLE_RANGE
    assert result.knowledge_source is AIQAKnowledgeSource.MODEL_KNOWLEDGE
    assert result.events[0].place == "California"
    assert result.events[0].magnitude is None
    assert result.usage.requests == 2
    assert model.calls[1]["output_type"] is AIQAOutOfRangeAnswer
    assert '"retrieval_status":"not_attempted_outside_available_range"' in str(
        model.calls[1]["context"]
    )
    assert result.data_context is not None
    assert result.data_context["requested_start_utc"] == "2000-01-01T00:00:00Z"
    assert skill.queries == []


def test_orchestrator_queries_available_part_of_historical_overlap() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(
                time_range=AIQATimeRange(
                    mode="explicit",
                    start_utc=datetime(2026, 1, 1, tzinfo=UTC),
                    end_utc=datetime(2026, 9, 11, 18, tzinfo=UTC),
                )
            ),
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer=(
                "Yes, during the available portion. Coverage begins at "
                "2026-09-11T17:00:00Z."
            ),
            confidence=0.9,
        ),
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(orchestrator(model, skill).answer("Overlapping?", now=NOW))

    assert result.outcome is AIQAResponseOutcome.ANSWERED
    assert result.data_status is AIQADataStatus.PARTIAL_COVERAGE
    assert result.usage.requests == 2
    assert len(skill.queries) == 1
    assert skill.queries[0].start_time == AVAILABLE_FROM
    assert skill.queries[0].end_time == datetime(2026, 9, 11, 18, tzinfo=UTC)
    assert result.data_context is not None
    assert result.data_context["data_status"] == AIQADataStatus.PARTIAL_COVERAGE
    query_contexts = result.data_context["queries"]
    assert isinstance(query_contexts, list)
    query_context = query_contexts[0]
    assert query_context["requested_range"] == {
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-09-11T18:00:00Z",
    }
    assert query_context["effective_range"] == {
        "start_utc": "2026-09-11T17:00:00Z",
        "end_utc": "2026-09-11T18:00:00Z",
    }
    assert query_context["available_from_utc"] == "2026-09-11T17:00:00Z"
    assert "no matching records supports only" in model.calls[1]["instructions"]
    assert "general model knowledge" in model.calls[1]["instructions"]
    assert (
        "Never use model knowledge to fill a future period"
        in model.calls[1]["instructions"]
    )


def test_orchestrator_queries_available_part_of_future_overlap() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(
                time_range=AIQATimeRange(
                    mode="explicit",
                    start_utc=datetime(2026, 9, 12, 1, tzinfo=UTC),
                    end_utc=datetime(2026, 9, 13, 1, tzinfo=UTC),
                )
            ),
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer="One event was found before the current time.",
            confidence=0.9,
        ),
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(
        orchestrator(model, skill).answer("Any earthquakes today or tomorrow?", now=NOW)
    )

    assert result.data_status is AIQADataStatus.PARTIAL_COVERAGE
    assert len(skill.queries) == 1
    assert skill.queries[0].start_time == datetime(2026, 9, 12, 1, tzinfo=UTC)
    assert skill.queries[0].end_time == NOW


def test_partial_coverage_prompt_qualifies_an_empty_result() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(
                time_range=AIQATimeRange(
                    mode="explicit",
                    start_utc=datetime(2026, 1, 1, tzinfo=UTC),
                    end_utc=NOW,
                )
            ),
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer=(
                "No matching earthquakes were found in the available portion; "
                "the result does not cover the full requested year."
            ),
            confidence=0.8,
        ),
    )
    skill = FakeEarthquakeSkill(count=0)

    result = asyncio.run(
        orchestrator(model, skill).answer("Any earthquakes this year?", now=NOW)
    )

    assert result.data_status is AIQADataStatus.PARTIAL_COVERAGE
    assert result.data_context is not None
    assert result.data_context["matched_records"] == 0
    instructions = model.calls[1]["instructions"]
    assert '"none found in the available portion,"' in instructions
    assert '"none occurred" across the full requested range' in instructions


def test_orchestrator_requires_clarification_for_entirely_future_range() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(
                time_range=AIQATimeRange(
                    mode="explicit",
                    start_utc=datetime(2026, 9, 13, tzinfo=UTC),
                    end_utc=datetime(2026, 9, 14, tzinfo=UTC),
                )
            ),
        )
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(orchestrator(model, skill).answer("Tomorrow?", now=NOW))

    assert result.outcome is AIQAResponseOutcome.CLARIFICATION_REQUIRED
    assert "entirely in the future" in result.answer
    assert result.usage.requests == 1
    assert skill.queries == []


def test_orchestrator_allows_general_answer_when_availability_fails() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.ANSWERED,
            skill=AIQASkillName.GENERAL,
            answer="A general answer.",
            confidence=0.8,
        )
    )

    result = asyncio.run(
        orchestrator(model, FakeEarthquakeSkill(), availability_fail=True).answer(
            "General question", now=NOW
        )
    )

    assert result.answer == "A general answer."


def test_orchestrator_requires_availability_for_data_route() -> None:
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=data_request(),
        )
    )

    with pytest.raises(AIQAOrchestrationUnavailableError):
        asyncio.run(
            orchestrator(model, FakeEarthquakeSkill(), availability_fail=True).answer(
                "Earthquakes?", now=NOW
            )
        )


def test_orchestrator_rejects_duplicate_additional_query() -> None:
    request = data_request()
    model = FakeModelService(
        AIQARouterDecision(
            outcome=AIQARouterOutcome.DATA_REQUIRED,
            skill=AIQASkillName.LYTIR_DATA,
            data_request=request,
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.ADDITIONAL_DATA_REQUIRED,
            data_request=request,
        ),
        AIQAGroundedDecision(
            outcome=AIQAGroundedOutcome.FINAL_ANSWER,
            answer="Only one distinct query could be completed.",
            confidence=0.6,
        ),
    )
    skill = FakeEarthquakeSkill()

    result = asyncio.run(orchestrator(model, skill).answer("Compare it", now=NOW))

    assert result.outcome is AIQAResponseOutcome.INCOMPLETE
    assert len(skill.queries) == 1
    assert result.data_context is not None
    assert result.data_context["stop_reason"] == "duplicate_data_request"
    assert result.usage.requests == 3
