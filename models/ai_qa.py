"""Request and response contracts for the AI Q&A diagnostic endpoint."""

import math
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from models.ai_qa_enums import (
    AIQAGroundedOutcome,
    AIQARouterOutcome,
    AIQASkillName,
)

AI_QA_MAX_QUESTION_CHARACTERS = 4000

DiagnosticQuestion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=AI_QA_MAX_QUESTION_CHARACTERS,
    ),
]


class AIQADiagnosticRequest(BaseModel):
    """A bounded generic question used to verify model connectivity."""

    model_config = ConfigDict(extra="forbid")

    question: DiagnosticQuestion


class AIQAModelAnswer(BaseModel):
    """Structured answer requested from the configured model."""

    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    confidence: float = Field(
        ge=0,
        le=1,
        description="Model-reported confidence from 0 (lowest) to 1 (highest)",
    )


class AIQAUsage(BaseModel):
    """Stable subset of token and request usage reported by PydanticAI."""

    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AIQAResult(BaseModel):
    """Provider-neutral result returned by the internal AI Q&A service."""

    answer: str
    confidence: float
    usage: AIQAUsage


class AIQADiagnosticResponse(BaseModel):
    """Successful single-round diagnostic response."""

    question: str
    answer: str
    confidence: float
    usage: AIQAUsage
    execution_seconds: float = Field(ge=0)
    model_host: str
    model_profile: str
    model_name: str
    utc_now: datetime


class AIQATimeRange(BaseModel):
    """Model-planned time intent for one earthquake-data request."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["default", "explicit"]
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    timezone: str | None = Field(
        default=None,
        description="IANA timezone inferred from the question or named location",
    )

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.mode == "explicit" and (self.start_utc is None or self.end_utc is None):
            raise ValueError("explicit time ranges require start_utc and end_utc")
        if self.mode == "default" and (
            self.start_utc is not None or self.end_utc is not None
        ):
            raise ValueError("default time ranges cannot include timestamps")
        return self


class AIQAEarthquakeDataRequest(BaseModel):
    """Typed, allowlisted earthquake filters proposed by the model."""

    model_config = ConfigDict(extra="forbid")

    time_range: AIQATimeRange
    location_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None
    location_confidence: float | None = Field(default=None, ge=0, le=1)
    min_magnitude: float | None = None
    max_magnitude: float | None = None

    @model_validator(mode="after")
    def validate_finite_numbers(self) -> Self:
        values = (
            self.latitude,
            self.longitude,
            self.radius_km,
            self.min_magnitude,
            self.max_magnitude,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("earthquake filters must be finite")
        return self


class AIQAModelKnowledgeEvent(BaseModel):
    """Nullable earthquake metadata supplied from general model knowledge."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["earthquake"] | None = None
    magnitude: float | None = None
    place: str | None = None
    occurred_at_utc: datetime | None = None
    updated_at_utc: datetime | None = None
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    depth_km: float | None = None

    @model_validator(mode="after")
    def validate_finite_numbers(self) -> Self:
        values = (self.magnitude, self.longitude, self.latitude, self.depth_km)
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("earthquake metadata values must be finite")
        return self


class AIQAOutOfRangeAnswer(BaseModel):
    """Best-effort answer when Lytir has no data for the requested period."""

    model_config = ConfigDict(extra="forbid")

    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    confidence: float = Field(ge=0, le=1)
    events: list[AIQAModelKnowledgeEvent] = Field(default_factory=list, max_length=10)


class AIQARouterDecision(BaseModel):
    """Structured first-round route, direct answer, or clarification."""

    model_config = ConfigDict(extra="forbid")

    outcome: AIQARouterOutcome
    skill: AIQASkillName
    answer: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    data_request: AIQAEarthquakeDataRequest | None = None
    clarification: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome is AIQARouterOutcome.ANSWERED:
            if (
                self.skill is AIQASkillName.LYTIR_DATA
                or not self.answer
                or self.confidence is None
            ):
                raise ValueError(
                    "direct answers require a non-data skill and confidence"
                )
        elif self.outcome is AIQARouterOutcome.DATA_REQUIRED:
            if self.skill is not AIQASkillName.LYTIR_DATA or self.data_request is None:
                raise ValueError("data_required must contain a Lytir data request")
        else:
            if self.skill is not AIQASkillName.LYTIR_DATA or not self.clarification:
                raise ValueError("clarification_required must contain a clarification")
        return self


class AIQAGroundedDecision(BaseModel):
    """Structured decision after one or more earthquake-data calls."""

    model_config = ConfigDict(extra="forbid")

    outcome: AIQAGroundedOutcome
    answer: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    data_request: AIQAEarthquakeDataRequest | None = None
    clarification: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome is AIQAGroundedOutcome.FINAL_ANSWER:
            if not self.answer or self.confidence is None:
                raise ValueError("final answers require answer and confidence")
        elif self.outcome is AIQAGroundedOutcome.ADDITIONAL_DATA_REQUIRED:
            if self.data_request is None:
                raise ValueError("additional_data_required needs a data request")
        else:
            if not self.clarification:
                raise ValueError("clarification_required needs a clarification")
        return self
