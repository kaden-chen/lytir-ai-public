"""Allowlisted earthquake retrieval and evidence formatting skill."""

from dataclasses import dataclass

from models import EarthquakeQuery, EarthquakeRetrievalRecord
from services import EarthquakeQueryResult, EarthquakeQueryService


@dataclass(frozen=True)
class EarthquakeEvidence:
    """Complete public metadata evidence from one validated query."""

    text: str
    record_count: int
    character_count: int
    query_result: EarthquakeQueryResult


class EarthquakeDataSkill:
    """Execute validated filters without exposing database query capability."""

    def __init__(self, service: EarthquakeQueryService) -> None:
        self._service = service

    def execute(self, query: EarthquakeQuery) -> EarthquakeEvidence:
        result = self._service.query(query)
        lines = [_evidence_line(item) for item in result.items]
        text = "\n".join(lines) if lines else "NO_MATCHING_EARTHQUAKES"
        return EarthquakeEvidence(
            text=text,
            record_count=len(result.items),
            character_count=len(text),
            query_result=result,
        )


def _evidence_line(item: EarthquakeRetrievalRecord) -> str:
    magnitude = "unknown" if item["magnitude"] is None else f"{item['magnitude']:g}"
    place = item["place"] or "place unavailable"
    return (
        f"{item['occurred_at_utc']} | M{magnitude} | {place} | "
        f"{item['latitude']:g},{item['longitude']:g} | "
        f"depth {item['depth_km']:g} km"
    )
