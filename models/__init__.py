"""Domain data shapes."""

from models.ai_qa import (
    AI_QA_MAX_QUESTION_CHARACTERS,
    AIQADiagnosticRequest,
    AIQADiagnosticResponse,
    AIQAEarthquakeDataRequest,
    AIQAGroundedDecision,
    AIQAModelAnswer,
    AIQAModelKnowledgeEvent,
    AIQAOutOfRangeAnswer,
    AIQAResult,
    AIQARouterDecision,
    AIQATimeRange,
    AIQAUsage,
)
from models.ai_qa_enums import (
    AIQADataStatus,
    AIQAGroundedOutcome,
    AIQAKnowledgeSource,
    AIQAResponseOutcome,
    AIQARouterOutcome,
    AIQASkillName,
)
from models.earthquake import (
    EarthquakeEnvelope,
    EarthquakeMetadata,
    EarthquakeMetadataRecord,
)
from models.earthquake_retrieval import EarthquakeQuery, EarthquakeRetrievalRecord

__all__ = [
    "AI_QA_MAX_QUESTION_CHARACTERS",
    "AIQAEarthquakeDataRequest",
    "AIQADiagnosticRequest",
    "AIQADiagnosticResponse",
    "AIQADataStatus",
    "AIQAGroundedDecision",
    "AIQAGroundedOutcome",
    "AIQAKnowledgeSource",
    "AIQAModelKnowledgeEvent",
    "AIQAModelAnswer",
    "AIQAOutOfRangeAnswer",
    "AIQARouterDecision",
    "AIQARouterOutcome",
    "AIQAResponseOutcome",
    "AIQAResult",
    "AIQATimeRange",
    "AIQAUsage",
    "AIQASkillName",
    "EarthquakeEnvelope",
    "EarthquakeMetadata",
    "EarthquakeMetadataRecord",
    "EarthquakeQuery",
    "EarthquakeRetrievalRecord",
]
