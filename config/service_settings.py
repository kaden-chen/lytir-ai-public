"""Application settings loaded from environment variables."""

import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property
from urllib.parse import urlsplit, urlunsplit

DEFAULT_EARTHQUAKE_QUERY_MAX_WINDOW_HOURS = 720
DEFAULT_EARTHQUAKE_QUERY_MAX_RADIUS_KM = 1000.0
MAX_AI_QA_MODEL_CONTEXT_WINDOW_TOKENS = 2_097_152
MAX_AI_QA_MODEL_MAX_OUTPUT_TOKENS = 1_048_576
DEFAULT_AI_QA_MODEL_TIMEOUT_SECONDS = 30.0
DEFAULT_AI_QA_MAX_MODEL_REQUESTS = 5
DEFAULT_AI_QA_MAX_TOOL_CALLS = 3
DEFAULT_AI_QA_MAX_EXECUTION_SECONDS = 210
AI_QA_MODEL_PREFIX = "AI_QA_MODEL_"
AI_QA_MODEL_ENABLED_SUFFIX = "_ENABLED"
ENVIRONMENT_EMPTY_VALUE = "__NA__"
ENVIRONMENT_VARIABLE_CONFIGURED = "configured"
ENVIRONMENT_VARIABLE_MISSING = "missing"
SERVICE_ENVIRONMENT_VARIABLE_NAMES = (
    "AI_QA_MAX_EXECUTION_SECONDS",
    "AI_QA_MAX_MODEL_REQUESTS",
    "AI_QA_MAX_TOOL_CALLS",
    "AzureWebJobsStorage",
    "COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME",
    "COSMOS_EARTHQUAKE_DB_CONNECTION_STRING",
    "COSMOS_EARTHQUAKE_DB_NAME",
    "EARTHQUAKE_DATA_AVAILABLE_FROM_UTC",
    "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
    "EARTHQUAKE_QUERY_MAX_WINDOW_HOURS",
    "ADMIN_USER_EMAILS",
    "FIREBASE_PROJECT_ID",
    "FUNCTIONS_WORKER_RUNTIME",
    "IOT_HUB_DEVICE_CONNECTION_STRING",
    "JSON_FILE_ENCODING",
    "SERVICE_MODE",
    "USGS_CRON_JOB_SCHEDULE",
    "USGS_EARTHQUAKE_API_URL",
)
AI_QA_MODEL_ENVIRONMENT_SUFFIXES = (
    "API_KEY",
    "ASSET_NAME",
    "CONTEXT_WINDOW_TOKENS",
    "ENABLED",
    "ENDPOINT_URL",
    "FREQUENCY_PENALTY",
    "MAX_OUTPUT_TOKENS",
    "MODEL_NAME",
    "NOTE",
    "PRESENCE_PENALTY",
    "PROXY_AI_SERVICE_NAME",
    "PROXY_API_KEY",
    "STREAM",
    "TIMEOUT_SECONDS",
    "TOP_P",
)


class AIQAModelAuthMode(StrEnum):
    """Supported authentication paths for an AI Q&A model profile."""

    API_KEY = "api_key"
    MANAGED_IDENTITY = "managed_identity"


class AIQAModelHost(StrEnum):
    """Public host classification for diagnostic model metadata."""

    AZURE = "azure"
    NATIVE_OPENAI = "native_openai"


@dataclass(frozen=True)
class AIQAModelSettings:
    """Validated settings for the single enabled AI Q&A model profile."""

    profile_name: str
    endpoint_url: str
    asset_name: str
    model_name: str
    auth_mode: AIQAModelAuthMode
    api_key: str = field(repr=False)
    context_window_tokens: int
    max_output_tokens: int
    timeout_seconds: float

    @property
    def model_host(self) -> AIQAModelHost:
        if self.auth_mode is AIQAModelAuthMode.MANAGED_IDENTITY:
            return AIQAModelHost.AZURE
        return AIQAModelHost.NATIVE_OPENAI


@dataclass(frozen=True)
class AIQASettings:
    """Validated production Q&A orchestration settings."""

    earthquake_data_available_from_utc: datetime
    max_model_requests: int
    max_tool_calls: int
    max_execution_seconds: int
    debug: bool


class ServiceSettings:
    """Environment settings owned by the Function App."""

    @staticmethod
    def environment_variable_availability() -> dict[str, str]:
        """Report key existence without reading or interpreting setting values."""
        names: set[str] = set(SERVICE_ENVIRONMENT_VARIABLE_NAMES)
        names.update(name for name in os.environ if name.startswith(AI_QA_MODEL_PREFIX))
        for profile_name in _configured_ai_qa_model_profile_names():
            names.update(
                f"{AI_QA_MODEL_PREFIX}{profile_name}_{suffix}"
                for suffix in AI_QA_MODEL_ENVIRONMENT_SUFFIXES
            )
        return {
            name: (
                ENVIRONMENT_VARIABLE_CONFIGURED
                if name in os.environ
                else ENVIRONMENT_VARIABLE_MISSING
            )
            for name in sorted(names)
        }

    @staticmethod
    def firebase_project_id() -> str:
        """Load the Firebase project identity without unrelated settings."""
        return _required("FIREBASE_PROJECT_ID")

    @staticmethod
    def firebase_admin_emails() -> frozenset[str]:
        """Load normalized emails that receive the derived admin role."""
        value = _environment_value("ADMIN_USER_EMAILS")
        if not value:
            return frozenset()
        return frozenset(
            email for item in value.split(",") if (email := item.strip().casefold())
        )

    def __init__(self) -> None:
        self.usgs_earthquake_api_url = _required("USGS_EARTHQUAKE_API_URL")
        self.iot_hub_device_connection_string = _required(
            "IOT_HUB_DEVICE_CONNECTION_STRING"
        )
        json_file_encoding = _environment_value("JSON_FILE_ENCODING")
        self.json_file_encoding = (
            "utf-8" if json_file_encoding is None else json_file_encoding
        )
        self.service_mode = _environment_value("SERVICE_MODE") or ""
        self.cosmos_earthquake_db_connection_string = _required(
            "COSMOS_EARTHQUAKE_DB_CONNECTION_STRING"
        )
        self.cosmos_earthquake_db_name = _required("COSMOS_EARTHQUAKE_DB_NAME")
        self.cosmos_earthquake_data_container_name = _required(
            "COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME"
        )
        self.earthquake_query_max_window_hours = _positive_integer(
            "EARTHQUAKE_QUERY_MAX_WINDOW_HOURS",
            default=DEFAULT_EARTHQUAKE_QUERY_MAX_WINDOW_HOURS,
        )
        self.earthquake_query_max_radius_km = _minimum_finite_number(
            "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
            minimum=5,
            default=DEFAULT_EARTHQUAKE_QUERY_MAX_RADIUS_KM,
        )

    @cached_property
    def ai_qa(self) -> AIQASettings:
        """Load production Q&A-only settings when that endpoint is used."""
        max_model_requests = _positive_integer(
            "AI_QA_MAX_MODEL_REQUESTS",
            default=DEFAULT_AI_QA_MAX_MODEL_REQUESTS,
        )
        max_tool_calls = _positive_integer(
            "AI_QA_MAX_TOOL_CALLS",
            default=DEFAULT_AI_QA_MAX_TOOL_CALLS,
        )
        if max_model_requests <= max_tool_calls:
            raise ValueError(
                "AI_QA_MAX_MODEL_REQUESTS must be greater than AI_QA_MAX_TOOL_CALLS"
            )
        return AIQASettings(
            earthquake_data_available_from_utc=_required_utc_datetime(
                "EARTHQUAKE_DATA_AVAILABLE_FROM_UTC"
            ),
            max_model_requests=max_model_requests,
            max_tool_calls=max_tool_calls,
            max_execution_seconds=_positive_integer(
                "AI_QA_MAX_EXECUTION_SECONDS",
                default=DEFAULT_AI_QA_MAX_EXECUTION_SECONDS,
            ),
            debug=self.debug_mode,
        )

    @property
    def debug_mode(self) -> bool:
        """Enable diagnostic detail only for the exact explicit mode."""
        return self.service_mode == "debug"

    @cached_property
    def ai_qa_model(self) -> AIQAModelSettings:
        """Load the single enabled AI Q&A model profile on first use."""
        return _load_ai_qa_model_settings()


def _required(name: str) -> str:
    value = _environment_value(name)
    if value is None or not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_integer(name: str, *, default: int) -> int:
    value = _environment_value(name)
    if value is None or not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _minimum_finite_number(name: str, *, minimum: float, default: float) -> float:
    value = _environment_value(name)
    if value is None or not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a finite number at least {minimum:g}"
        ) from exc
    if not math.isfinite(parsed) or parsed < minimum:
        raise ValueError(f"{name} must be a finite number at least {minimum:g}")
    return parsed


def _load_ai_qa_model_settings() -> AIQAModelSettings:
    enabled_profiles = []
    for name, value in os.environ.items():
        if not name.startswith(AI_QA_MODEL_PREFIX) or not name.endswith(
            AI_QA_MODEL_ENABLED_SUFFIX
        ):
            continue
        profile_name = name[len(AI_QA_MODEL_PREFIX) : -len(AI_QA_MODEL_ENABLED_SUFFIX)]
        if not profile_name:
            continue
        if _configuration_boolean(name, value):
            enabled_profiles.append(profile_name)

    enabled_profiles.sort()
    if len(enabled_profiles) != 1:
        raise ValueError(
            "exactly one AI_QA_MODEL_*_ENABLED setting must be true; "
            f"found {len(enabled_profiles)}"
        )

    profile_name = enabled_profiles[0]
    profile_prefix = f"{AI_QA_MODEL_PREFIX}{profile_name}_"
    endpoint_name = f"{profile_prefix}ENDPOINT_URL"
    endpoint_url = _normalize_ai_endpoint(endpoint_name, _required(endpoint_name))
    host = urlsplit(endpoint_url).hostname or ""
    api_key = _optional(f"{profile_prefix}API_KEY")

    if not api_key:
        if not _is_azure_ai_host(host):
            raise ValueError(
                f"{endpoint_name} must be an Azure AI endpoint when API_KEY is empty"
            )
        auth_mode = AIQAModelAuthMode.MANAGED_IDENTITY
    else:
        if _is_azure_ai_host(host):
            raise ValueError(
                f"{profile_prefix}API_KEY is forbidden for Azure AI Foundry profiles"
            )
        if host != "api.openai.com":
            raise ValueError(
                f"{endpoint_name} must use api.openai.com for native OpenAI"
            )
        auth_mode = AIQAModelAuthMode.API_KEY

    context_window_tokens = _required_bounded_positive_integer(
        f"{profile_prefix}CONTEXT_WINDOW_TOKENS",
        maximum=MAX_AI_QA_MODEL_CONTEXT_WINDOW_TOKENS,
    )
    max_output_tokens = _required_bounded_positive_integer(
        f"{profile_prefix}MAX_OUTPUT_TOKENS",
        maximum=MAX_AI_QA_MODEL_MAX_OUTPUT_TOKENS,
    )
    if context_window_tokens <= max_output_tokens:
        raise ValueError(
            f"{profile_prefix}CONTEXT_WINDOW_TOKENS must be greater than "
            f"{profile_prefix}MAX_OUTPUT_TOKENS"
        )

    return AIQAModelSettings(
        profile_name=profile_name,
        endpoint_url=endpoint_url,
        asset_name=_required(f"{profile_prefix}ASSET_NAME"),
        model_name=_required(f"{profile_prefix}MODEL_NAME"),
        auth_mode=auth_mode,
        api_key=api_key,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        timeout_seconds=_positive_finite_number(
            f"{profile_prefix}TIMEOUT_SECONDS",
            default=DEFAULT_AI_QA_MODEL_TIMEOUT_SECONDS,
        ),
    )


def _configured_ai_qa_model_profile_names() -> set[str]:
    """Find model profile names without requiring any one profile field."""
    profiles: set[str] = set()
    suffixes = sorted(AI_QA_MODEL_ENVIRONMENT_SUFFIXES, key=len, reverse=True)
    for name in os.environ:
        if not name.startswith(AI_QA_MODEL_PREFIX):
            continue
        remainder = name[len(AI_QA_MODEL_PREFIX) :]
        for suffix in suffixes:
            marker = f"_{suffix}"
            if remainder.endswith(marker):
                profile_name = remainder[: -len(marker)]
                if profile_name:
                    profiles.add(profile_name)
                break
    return profiles


def _required_utc_datetime(name: str) -> datetime:
    value = _required(name)
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        try:
            # Core Tools normalizes some ISO values from local.settings.json to
            # this timezone-free representation before exporting them.
            parsed = datetime.strptime(value, "%m/%d/%Y %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            raise ValueError(f"{name} must be a valid ISO 8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{name} must use UTC")
    return parsed.astimezone(UTC)


def _environment_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    return _normalize_environment_value(value)


def _normalize_environment_value(value: str) -> str:
    normalized = value.strip()
    return "" if normalized == ENVIRONMENT_EMPTY_VALUE else normalized


def _optional(name: str) -> str:
    return _environment_value(name) or ""


def _configuration_boolean(name: str, value: str) -> bool:
    normalized = _normalize_environment_value(value).casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _required_bounded_positive_integer(name: str, *, maximum: int) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a positive integer no greater than {maximum}"
        ) from exc
    if parsed <= 0 or parsed > maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return parsed


def _positive_finite_number(name: str, *, default: float) -> float:
    value = _environment_value(name)
    if value is None or not value:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _normalize_ai_endpoint(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS endpoint without credentials")

    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        path = path[: -len("/responses")]
    if not path.endswith("/v1"):
        raise ValueError(f"{name} must target an OpenAI-compatible v1 endpoint")

    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/", "", ""))


def _is_azure_ai_host(host: str) -> bool:
    return host.endswith(
        (".openai.azure.com", ".services.ai.azure.com", ".models.ai.azure.com")
    )
