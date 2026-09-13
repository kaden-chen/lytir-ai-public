"""Azure Functions blueprints."""

from blueprints.ai_qa import blueprint as ai_qa_blueprint
from blueprints.ai_qa_diagnostic import blueprint as ai_qa_diagnostic_blueprint
from blueprints.api_proxy import blueprint as api_proxy_blueprint
from blueprints.diagnostic import blueprint as diagnostic_blueprint
from blueprints.earthquake_acquisition import (
    blueprint as earthquake_acquisition_blueprint,
)
from blueprints.earthquake_retrieval import blueprint as earthquake_retrieval_blueprint
from blueprints.earthquake_scheduler import blueprint as earthquake_scheduler_blueprint

__all__ = [
    "api_proxy_blueprint",
    "ai_qa_blueprint",
    "ai_qa_diagnostic_blueprint",
    "diagnostic_blueprint",
    "earthquake_acquisition_blueprint",
    "earthquake_retrieval_blueprint",
    "earthquake_scheduler_blueprint",
]
