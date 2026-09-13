import json
from datetime import datetime

import azure.functions as func
import pytest

from function_app import app, diag


def test_diag_returns_request_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_MODE", "debug")
    monkeypatch.delenv("AI_QA_MAX_TOOL_CALLS", raising=False)
    request = func.HttpRequest(
        method="GET",
        url="http://localhost:7071/api/diag?example=value",
        headers={},
        params={"example": "value"},
        route_params={},
        body=b"",
    )

    response = diag(request)
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert payload["url"] == request.url
    assert payload["params"] == {"example": "value"}
    assert payload["status"] == "200 - OK"
    assert payload["environment_variables"]["SERVICE_MODE"] == "configured"
    assert payload["environment_variables"]["AI_QA_MAX_TOOL_CALLS"] == "missing"
    assert set(payload["environment_variables"].values()) <= {
        "configured",
        "missing",
    }

    utc_now = datetime.fromisoformat(payload["utc_now"])
    assert utc_now.tzinfo is not None
    assert utc_now.utcoffset() is not None


def test_diag_redacts_function_key_query_parameter() -> None:
    request = func.HttpRequest(
        method="GET",
        url="http://localhost:7071/api/diag?code=test_api_code&example=value",
        headers={},
        params={"code": "test_api_code", "example": "value"},
        route_params={},
        body=b"",
    )

    response = diag(request)
    payload = json.loads(response.get_body())

    assert payload["url"] == "http://localhost:7071/api/diag?code=***&example=value"
    assert payload["params"] == {"code": "***", "example": "value"}
    assert "test_api_code" not in response.get_body().decode()


def test_http_trigger_authorization_and_routes() -> None:
    function_metadata = {
        function.get_function_name(): json.loads(function.get_function_json())
        for function in app.get_functions()
    }

    for function_name in (
        "diag",
        "answer_ai_qa",
        "diagnose_ai_qa",
        "acquire_earthquakes",
        "retrieve_earthquakes",
        "retrieve_earthquake",
    ):
        http_trigger = next(
            binding
            for binding in function_metadata[function_name]["bindings"]
            if binding["type"] == "httpTrigger"
        )
        assert http_trigger["authLevel"] == "FUNCTION"

    for function_name, route in (
        ("proxy_api", "proxy/{target}"),
        ("proxy_api_with_id", "proxy/{target}/{id}"),
    ):
        proxy_trigger = next(
            binding
            for binding in function_metadata[function_name]["bindings"]
            if binding["type"] == "httpTrigger"
        )
        assert proxy_trigger["authLevel"] == "ANONYMOUS"
        assert proxy_trigger["methods"] == ["GET", "POST"]
        assert proxy_trigger["route"] == route

    acquisition_trigger = next(
        binding
        for binding in function_metadata["acquire_earthquakes"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert acquisition_trigger["methods"] == ["POST"]
    assert acquisition_trigger["route"] == "earthquakes/acquire"

    diagnostic_trigger = next(
        binding
        for binding in function_metadata["diag"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert diagnostic_trigger["methods"] == ["GET"]
    assert diagnostic_trigger["route"] == "diag"

    ai_qa_trigger = next(
        binding
        for binding in function_metadata["diagnose_ai_qa"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert ai_qa_trigger["methods"] == ["POST"]
    assert ai_qa_trigger["route"] == "diag/ai-qa"

    production_ai_qa_trigger = next(
        binding
        for binding in function_metadata["answer_ai_qa"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert production_ai_qa_trigger["methods"] == ["POST"]
    assert production_ai_qa_trigger["route"] == "ai/qa"

    collection_trigger = next(
        binding
        for binding in function_metadata["retrieve_earthquakes"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert collection_trigger["methods"] == ["GET"]
    assert collection_trigger["route"] == "earthquakes"

    exact_trigger = next(
        binding
        for binding in function_metadata["retrieve_earthquake"]["bindings"]
        if binding["type"] == "httpTrigger"
    )
    assert exact_trigger["methods"] == ["GET"]
    assert exact_trigger["route"] == "earthquakes/{id}"

    timer_trigger = next(
        binding
        for binding in function_metadata["scheduled_earthquake_acquisition"]["bindings"]
        if binding["type"] == "timerTrigger"
    )
    assert timer_trigger["schedule"] == "%USGS_CRON_JOB_SCHEDULE%"
    assert timer_trigger["runOnStartup"] is False
    assert timer_trigger["useMonitor"] is True
