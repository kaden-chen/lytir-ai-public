"""Shared contracts for executable Q&A skills."""

from typing import Protocol

from models import EarthquakeQuery
from services.ai_qa.skills.earthquake_data import EarthquakeEvidence


class EarthquakeSkill(Protocol):
    """Only executable skill contract supported in the initial release."""

    def execute(self, query: EarthquakeQuery) -> EarthquakeEvidence: ...
