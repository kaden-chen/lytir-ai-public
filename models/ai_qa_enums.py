"""Stable domain values used by the AI Q&A protocol."""

from enum import StrEnum


class AIQARouterOutcome(StrEnum):
    """Outcomes allowed from the first model routing decision."""

    ANSWERED = "answered"
    DATA_REQUIRED = "data_required"
    CLARIFICATION_REQUIRED = "clarification_required"


class AIQAGroundedOutcome(StrEnum):
    """Outcomes allowed after earthquake evidence has been supplied."""

    FINAL_ANSWER = "final_answer"
    ADDITIONAL_DATA_REQUIRED = "additional_data_required"
    CLARIFICATION_REQUIRED = "clarification_required"


class AIQAResponseOutcome(StrEnum):
    """Outcomes exposed by the public AI Q&A endpoint."""

    ANSWERED = "answered"
    CLARIFICATION_REQUIRED = "clarification_required"
    INCOMPLETE = "incomplete"


class AIQADataStatus(StrEnum):
    """Availability status for supplemental Q&A data."""

    PARTIAL_COVERAGE = "partial_coverage"
    OUTSIDE_AVAILABLE_RANGE = "outside_available_range"


class AIQAKnowledgeSource(StrEnum):
    """Provenance for supplemental facts returned with an answer."""

    MODEL_KNOWLEDGE = "model_knowledge"


class AIQASkillName(StrEnum):
    """Stable names of routes available to the AI Q&A orchestrator."""

    GENERAL = "general"
    LYTIR_INFO = "lytir_info"
    LYTIR_DATA = "lytir_data"
