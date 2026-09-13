import os
from datetime import UTC, datetime

import pytest

from config import AIQAModelAuthMode, ServiceSettings


def install_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "USGS_EARTHQUAKE_API_URL",
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    )
    monkeypatch.setenv(
        "IOT_HUB_DEVICE_CONNECTION_STRING",
        "HostName=example.invalid;DeviceId=test;SharedAccessKey=not-a-real-key",
    )
    monkeypatch.setenv("JSON_FILE_ENCODING", "utf-8")
    monkeypatch.setenv(
        "COSMOS_EARTHQUAKE_DB_CONNECTION_STRING",
        "AccountEndpoint=https://example.invalid/;AccountKey=not-a-real-key;",
    )
    monkeypatch.setenv("COSMOS_EARTHQUAKE_DB_NAME", "earthquakes")
    monkeypatch.setenv("COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME", "data")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_WINDOW_HOURS", "720")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_RADIUS_KM", "1000")


def clear_ai_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AI_QA_MODEL_"):
            monkeypatch.delenv(name)


def test_environment_variable_availability_includes_optional_profile_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_ai_model_settings(monkeypatch)
    monkeypatch.setenv("USGS_EARTHQUAKE_API_URL", "https://example.invalid/feed")
    monkeypatch.setenv("SERVICE_MODE", "   ")
    monkeypatch.setenv("AI_QA_MODEL_TEST_ENABLED", "false")
    monkeypatch.setenv("AI_QA_MODEL_TEST_ENDPOINT_URL", "https://example.invalid")
    monkeypatch.setenv("AI_QA_MODEL_TEST_API_KEY", "")

    availability = ServiceSettings.environment_variable_availability()

    assert availability["USGS_EARTHQUAKE_API_URL"] == "configured"
    assert availability["SERVICE_MODE"] == "configured"
    assert availability["ADMIN_USER_EMAILS"] == "missing"
    assert availability["FIREBASE_PROJECT_ID"] == "missing"
    assert availability["AI_QA_MODEL_TEST_ENABLED"] == "configured"
    assert availability["AI_QA_MODEL_TEST_ENDPOINT_URL"] == "configured"
    assert availability["AI_QA_MODEL_TEST_API_KEY"] == "configured"
    assert availability["AI_QA_MODEL_TEST_TIMEOUT_SECONDS"] == "missing"
    assert list(availability) == sorted(availability)


def test_firebase_project_id_is_loaded_without_other_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIREBASE_PROJECT_ID", " test-project ")

    assert ServiceSettings.firebase_project_id() == "test-project"


def test_firebase_project_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)

    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID is required"):
        ServiceSettings.firebase_project_id()


def test_firebase_admin_emails_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ADMIN_USER_EMAILS",
        " Admin@Example.com,second@example.com, ADMIN@example.com, ,",
    )

    assert ServiceSettings.firebase_admin_emails() == {
        "admin@example.com",
        "second@example.com",
    }


def test_firebase_admin_emails_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_USER_EMAILS", raising=False)

    assert ServiceSettings.firebase_admin_emails() == frozenset()


def install_ai_model_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str,
    endpoint: str,
    api_key: str = "",
) -> None:
    prefix = f"AI_QA_MODEL_{profile}_"
    monkeypatch.setenv(f"{prefix}ENABLED", "true")
    monkeypatch.setenv(f"{prefix}ENDPOINT_URL", endpoint)
    monkeypatch.setenv(f"{prefix}ASSET_NAME", "test-deployment")
    monkeypatch.setenv(f"{prefix}MODEL_NAME", "gpt-test")
    monkeypatch.setenv(f"{prefix}API_KEY", api_key)
    monkeypatch.setenv(f"{prefix}CONTEXT_WINDOW_TOKENS", "4096")
    monkeypatch.setenv(f"{prefix}MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv(f"{prefix}TIMEOUT_SECONDS", "15")


def test_service_settings_load_application_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)

    settings = ServiceSettings()

    assert settings.usgs_earthquake_api_url.endswith("all_hour.geojson")
    assert settings.json_file_encoding == "utf-8"
    assert settings.iot_hub_device_connection_string.startswith(
        "HostName=example.invalid"
    )
    assert settings.cosmos_earthquake_db_connection_string.startswith(
        "AccountEndpoint=https://example.invalid/"
    )
    assert settings.cosmos_earthquake_db_name == "earthquakes"
    assert settings.cosmos_earthquake_data_container_name == "data"
    assert settings.earthquake_query_max_window_hours == 720
    assert settings.earthquake_query_max_radius_km == 1000


def test_service_settings_default_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    install_settings(monkeypatch)
    monkeypatch.delenv("JSON_FILE_ENCODING", raising=False)

    assert ServiceSettings().json_file_encoding == "utf-8"


@pytest.mark.parametrize(
    "missing_name",
    [
        "USGS_EARTHQUAKE_API_URL",
        "IOT_HUB_DEVICE_CONNECTION_STRING",
        "COSMOS_EARTHQUAKE_DB_CONNECTION_STRING",
        "COSMOS_EARTHQUAKE_DB_NAME",
        "COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME",
    ],
)
def test_service_settings_require_application_values(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.delenv(missing_name, raising=False)

    with pytest.raises(ValueError, match=f"{missing_name} is required"):
        ServiceSettings()


def test_service_settings_treats_na_as_empty_for_required_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("COSMOS_EARTHQUAKE_DB_NAME", "__NA__")

    with pytest.raises(ValueError, match="COSMOS_EARTHQUAKE_DB_NAME is required"):
        ServiceSettings()


def test_service_settings_default_query_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.delenv("EARTHQUAKE_QUERY_MAX_WINDOW_HOURS")
    monkeypatch.delenv("EARTHQUAKE_QUERY_MAX_RADIUS_KM")

    settings = ServiceSettings()

    assert settings.earthquake_query_max_window_hours == 720
    assert settings.earthquake_query_max_radius_km == 1000


def test_service_settings_treats_na_as_empty_for_defaulted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("JSON_FILE_ENCODING", "__NA__")
    monkeypatch.setenv("SERVICE_MODE", "__NA__")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_WINDOW_HOURS", "__NA__")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_RADIUS_KM", "__NA__")

    settings = ServiceSettings()

    assert settings.json_file_encoding == ""
    assert settings.service_mode == ""
    assert settings.earthquake_query_max_window_hours == 720
    assert settings.earthquake_query_max_radius_km == 1000


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "EARTHQUAKE_QUERY_MAX_WINDOW_HOURS",
            "0",
            "must be a positive integer",
        ),
        (
            "EARTHQUAKE_QUERY_MAX_WINDOW_HOURS",
            "1.5",
            "must be a positive integer",
        ),
        (
            "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
            "4.9",
            "must be a finite number at least 5",
        ),
        (
            "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
            "nan",
            "must be a finite number at least 5",
        ),
        (
            "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
            "not-a-number",
            "must be a finite number at least 5",
        ),
    ],
)
def test_service_settings_reject_invalid_query_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        ServiceSettings()


def test_service_settings_load_foundry_model_with_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    clear_ai_model_settings(monkeypatch)
    install_ai_model_profile(
        monkeypatch,
        profile="AZURE_GPT",
        endpoint="https://example.services.ai.azure.com/openai/v1/responses",
        api_key="__NA__",
    )

    model = ServiceSettings().ai_qa_model

    assert model.profile_name == "AZURE_GPT"
    assert model.endpoint_url == "https://example.services.ai.azure.com/openai/v1/"
    assert model.asset_name == "test-deployment"
    assert model.model_name == "gpt-test"
    assert model.auth_mode is AIQAModelAuthMode.MANAGED_IDENTITY
    assert model.model_host == "azure"
    assert model.api_key == ""
    assert model.context_window_tokens == 4096
    assert model.max_output_tokens == 2048
    assert model.timeout_seconds == 15


def test_service_settings_load_native_openai_model_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    clear_ai_model_settings(monkeypatch)
    install_ai_model_profile(
        monkeypatch,
        profile="NATIVE_GPT",
        endpoint="https://api.openai.com/v1/responses",
        api_key="not-a-real-key",
    )

    model = ServiceSettings().ai_qa_model

    assert model.profile_name == "NATIVE_GPT"
    assert model.endpoint_url == "https://api.openai.com/v1/"
    assert model.auth_mode is AIQAModelAuthMode.API_KEY
    assert model.model_host == "native_openai"
    assert model.api_key == "not-a-real-key"


@pytest.mark.parametrize("enabled_count", [0, 2])
def test_service_settings_require_exactly_one_enabled_ai_model(
    monkeypatch: pytest.MonkeyPatch,
    enabled_count: int,
) -> None:
    install_settings(monkeypatch)
    clear_ai_model_settings(monkeypatch)
    for index in range(enabled_count):
        monkeypatch.setenv(f"AI_QA_MODEL_PROFILE_{index}_ENABLED", "true")

    with pytest.raises(ValueError, match="exactly one .* found"):
        _ = ServiceSettings().ai_qa_model


@pytest.mark.parametrize(
    ("endpoint", "api_key", "message"),
    [
        (
            "https://example.services.ai.azure.com/openai/v1/responses",
            "forbidden-key",
            "API_KEY is forbidden",
        ),
        (
            "https://api.openai.com/v1/responses",
            "",
            "must be an Azure AI endpoint",
        ),
    ],
)
def test_service_settings_reject_invalid_ai_authentication_combinations(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    api_key: str,
    message: str,
) -> None:
    install_settings(monkeypatch)
    clear_ai_model_settings(monkeypatch)
    install_ai_model_profile(
        monkeypatch,
        profile="TEST",
        endpoint=endpoint,
        api_key=api_key,
    )

    with pytest.raises(ValueError, match=message):
        _ = ServiceSettings().ai_qa_model


def test_service_settings_reject_excessive_ai_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    clear_ai_model_settings(monkeypatch)
    install_ai_model_profile(
        monkeypatch,
        profile="AZURE_GPT",
        endpoint="https://example.services.ai.azure.com/openai/v1/responses",
    )
    monkeypatch.setenv("AI_QA_MODEL_AZURE_GPT_MAX_OUTPUT_TOKENS", "1048577")

    with pytest.raises(ValueError, match="no greater than 1048576"):
        _ = ServiceSettings().ai_qa_model


def test_service_settings_load_qa_defaults_and_debug_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("EARTHQUAKE_DATA_AVAILABLE_FROM_UTC", "2026-09-11T17:00:00Z")
    monkeypatch.setenv("SERVICE_MODE", "debug")
    monkeypatch.delenv("AI_QA_MAX_MODEL_REQUESTS", raising=False)
    monkeypatch.delenv("AI_QA_MAX_TOOL_CALLS", raising=False)
    monkeypatch.delenv("AI_QA_MAX_EXECUTION_SECONDS", raising=False)

    qa = ServiceSettings().ai_qa

    assert qa.earthquake_data_available_from_utc == datetime(
        2026, 9, 11, 17, tzinfo=UTC
    )
    assert qa.max_model_requests == 5
    assert qa.max_tool_calls == 3
    assert qa.max_execution_seconds == 210
    assert qa.debug is True


def test_service_settings_accept_core_tools_normalized_utc_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("EARTHQUAKE_DATA_AVAILABLE_FROM_UTC", "09/11/2026 17:00:00")

    assert ServiceSettings().ai_qa.earthquake_data_available_from_utc == datetime(
        2026, 9, 11, 17, tzinfo=UTC
    )


@pytest.mark.parametrize("service_mode", ["", "DEBUG", "production"])
def test_service_settings_debug_mode_requires_exact_value(
    monkeypatch: pytest.MonkeyPatch,
    service_mode: str,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("EARTHQUAKE_DATA_AVAILABLE_FROM_UTC", "2026-09-11T17:00:00Z")
    monkeypatch.setenv("SERVICE_MODE", service_mode)

    assert ServiceSettings().ai_qa.debug is False


def test_service_settings_reject_invalid_qa_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_settings(monkeypatch)
    monkeypatch.setenv("EARTHQUAKE_DATA_AVAILABLE_FROM_UTC", "2026-09-11T17:00:00Z")
    monkeypatch.setenv("AI_QA_MAX_MODEL_REQUESTS", "3")
    monkeypatch.setenv("AI_QA_MAX_TOOL_CALLS", "3")

    with pytest.raises(ValueError, match="must be greater"):
        _ = ServiceSettings().ai_qa
