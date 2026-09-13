"""Explicit registry for executable Q&A skills."""

from models import AIQASkillName
from services.ai_qa.skills.earthquake_data import EarthquakeDataSkill


class AIQASkillRegistry:
    """Reject every model-selected executable skill except allowlisted ones."""

    def __init__(self, earthquake_data: EarthquakeDataSkill) -> None:
        self._skills = {AIQASkillName.LYTIR_DATA: earthquake_data}

    def get_earthquake_data(self, name: AIQASkillName) -> EarthquakeDataSkill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"unsupported executable skill: {name}") from exc
