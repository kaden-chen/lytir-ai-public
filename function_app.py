import azure.functions as func

from blueprints import (
    ai_qa_blueprint,
    ai_qa_diagnostic_blueprint,
    api_proxy_blueprint,
    diagnostic_blueprint,
    earthquake_acquisition_blueprint,
    earthquake_retrieval_blueprint,
    earthquake_scheduler_blueprint,
)
from blueprints.diagnostic import diag

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_functions(diagnostic_blueprint)
app.register_functions(earthquake_scheduler_blueprint)
app.register_functions(earthquake_acquisition_blueprint)
app.register_functions(earthquake_retrieval_blueprint)
app.register_functions(ai_qa_diagnostic_blueprint)
app.register_functions(ai_qa_blueprint)
app.register_functions(api_proxy_blueprint)

__all__ = ["app", "diag"]
