import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import azure.functions as func
import pytest

from blueprints import earthquake_retrieval as retrieval_module
from blueprints.earthquake_retrieval import (
    _parse_format,
    _parse_query,
    retrieve_earthquake,
    retrieve_earthquakes,
)
from config import ServiceSettings
from models import EarthquakeQuery, EarthquakeRetrievalRecord
from services import EarthquakeQueryResult, EarthquakeQueryUnavailableError

NOW = datetime(2026, 9, 11, 13, tzinfo=UTC)
RECORD_ID = "11111111-1111-4111-8111-111111111111"


class FakeService:
    def __init__(
        self,
        items: list[EarthquakeRetrievalRecord] | None = None,
        exact: EarthquakeRetrievalRecord | None = None,
        error: Exception | None = None,
    ) -> None:
        self.items = items or []
        self.exact = exact
        self.error = error
        self.query_filters: EarthquakeQuery | None = None
        self.record_id: str | None = None

    def query(self, filters: EarthquakeQuery) -> EarthquakeQueryResult:
        if self.error is not None:
            raise self.error
        self.query_filters = filters
        return EarthquakeQueryResult(
            items=self.items,
            candidates=len(self.items),
            malformed=0,
            duplicates=0,
            selected=len(self.items),
        )

    def get_by_id(self, record_id: str) -> EarthquakeRetrievalRecord | None:
        if self.error is not None:
            raise self.error
        self.record_id = record_id
        return self.exact


def settings() -> ServiceSettings:
    return ServiceSettings()


def record(**overrides: object) -> EarthquakeRetrievalRecord:
    values: dict[str, object] = {
        "id": RECORD_ID,
        "event_type": "earthquake",
        "magnitude": 2.4,
        "place": "Example",
        "occurred_at_utc": "2026-09-11T12:30:00Z",
        "updated_at_utc": "2026-09-11T12:35:00Z",
        "longitude": -122.4,
        "latitude": 38.7,
        "depth_km": 4.2,
    }
    values.update(overrides)
    return cast(EarthquakeRetrievalRecord, values)


def request(
    *, params: dict[str, str] | None = None, route_params: dict[str, str] | None = None
) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url="http://localhost:7071/api/earthquakes",
        headers={},
        params=params or {},
        route_params=route_params or {},
        body=b"",
    )


def install_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "USGS_EARTHQUAKE_API_URL",
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    )
    monkeypatch.setenv("IOT_HUB_DEVICE_CONNECTION_STRING", "not-a-real-secret")
    monkeypatch.setenv(
        "COSMOS_EARTHQUAKE_DB_CONNECTION_STRING",
        "AccountEndpoint=https://example.invalid/;AccountKey=not-a-real-key;",
    )
    monkeypatch.setenv("COSMOS_EARTHQUAKE_DB_NAME", "earthquakes")
    monkeypatch.setenv("COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME", "data")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_WINDOW_HOURS", "720")
    monkeypatch.setenv("EARTHQUAKE_QUERY_MAX_RADIUS_KM", "1000")


@pytest.fixture(autouse=True)
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    install_environment(monkeypatch)


def install_service(monkeypatch: pytest.MonkeyPatch, service: FakeService) -> None:
    monkeypatch.setattr(retrieval_module, "_service_for", lambda _settings: service)


def test_build_service_uses_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeDatabase:
        def get_container_client(self, container_name: str) -> object:
            calls.append(("container", container_name))
            return object()

    class FakeCosmosClient:
        @classmethod
        def from_connection_string(cls, connection_string: str) -> "FakeCosmosClient":
            calls.append(("connection", connection_string))
            return cls()

        def get_database_client(self, database_name: str) -> FakeDatabase:
            calls.append(("database", database_name))
            return FakeDatabase()

    retrieval_module._build_service.cache_clear()
    monkeypatch.setattr(retrieval_module, "CosmosClient", FakeCosmosClient)

    retrieval_module._build_service("not-a-real-secret", "earthquakes", "data")

    assert calls == [
        ("connection", "not-a-real-secret"),
        ("database", "earthquakes"),
        ("container", "data"),
    ]
    retrieval_module._build_service.cache_clear()


def test_query_defaults_to_previous_two_hours() -> None:
    parsed = _parse_query({}, settings(), now=NOW)

    assert parsed.start_time == NOW - timedelta(hours=2)
    assert parsed.end_time == NOW


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"start_time": "2026-09-11T12:00:00"}, "must include a UTC offset"),
        (
            {
                "start_time": "2026-08-01T00:00:00Z",
                "end_time": "2026-09-11T00:00:00Z",
            },
            "time window cannot exceed 720 hours",
        ),
        (
            {
                "start_time": "2026-09-11T13:00:00Z",
                "end_time": "2026-09-11T12:00:00Z",
            },
            "start_time must be earlier than end_time",
        ),
        ({"latitude": "1"}, "must be supplied together"),
        ({"radius_km": "5"}, "radius requires latitude and longitude"),
        (
            {"latitude": "91", "longitude": "0"},
            "latitude must be between -90 and 90",
        ),
        (
            {"min_magnitude": "5", "max_magnitude": "2"},
            "min_magnitude cannot exceed max_magnitude",
        ),
        (
            {"latitude": "0", "longitude": "0", "radius_km": "0"},
            "radius must be greater than zero",
        ),
        (
            {"latitude": "0", "longitude": "0", "radius_km": "1001"},
            "radius cannot exceed 1000 km",
        ),
        (
            {"latitude": "0", "longitude": "0", "radius_km": "nan"},
            "radius_km must be finite",
        ),
    ],
)
def test_query_rejects_invalid_filters(params: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_query(params, settings(), now=NOW)


def test_query_converts_miles_and_defaults_radius() -> None:
    miles = _parse_query(
        {"latitude": "38.7", "longitude": "-122.4", "radius_mi": "10"},
        settings(),
        now=NOW,
    )
    default = _parse_query(
        {"latitude": "38.7", "longitude": "-122.4"}, settings(), now=NOW
    )

    assert miles.radius_km == pytest.approx(16.09344)
    assert default.radius_km == 5


def test_radius_km_takes_precedence_over_radius_mi() -> None:
    parsed = _parse_query(
        {
            "latitude": "38.7",
            "longitude": "-122.4",
            "radius_km": "8",
            "radius_mi": "not-a-number",
        },
        settings(),
        now=NOW,
    )

    assert parsed.radius_km == 8


def test_fmt_alias_and_format_precedence() -> None:
    assert _parse_format({"fmt": "CSV"}) == "csv"
    assert _parse_format({"format": "json", "fmt": "csv"}) == "json"
    assert _parse_format({"format": "", "fmt": "csv"}) == "csv"


def test_collection_endpoint_returns_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(items=[record()])
    install_service(monkeypatch, service)

    response = retrieve_earthquakes(request())
    payload = json.loads(response.get_body())

    assert response.status_code == 200
    assert payload["count"] == 1
    assert payload["items"] == [record()]
    assert service.query_filters is not None
    assert payload["time_range"] == {
        "start_utc": service.query_filters.start_time.isoformat().replace(
            "+00:00", "Z"
        ),
        "end_utc": service.query_filters.end_time.isoformat().replace("+00:00", "Z"),
    }


def test_collection_endpoint_returns_safe_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(items=[record(place="=malicious()", magnitude=None)])
    install_service(monkeypatch, service)

    response = retrieve_earthquakes(request(params={"fmt": "csv"}))
    rows = list(csv.DictReader(io.StringIO(response.get_body().decode())))

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.headers["X-Result-Count"] == "1"
    assert list(rows[0]) == list(retrieval_module.CSV_FIELDS)
    assert rows[0]["place"] == "'=malicious()"
    assert rows[0]["magnitude"] == ""


def test_exact_endpoint_validates_uuid_and_returns_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(exact=record())
    install_service(monkeypatch, service)

    response = retrieve_earthquake(request(route_params={"id": RECORD_ID}))

    assert response.status_code == 200
    assert json.loads(response.get_body())["id"] == RECORD_ID
    assert service.record_id == RECORD_ID


def test_exact_endpoint_rejects_collection_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(exact=record())
    install_service(monkeypatch, service)

    response = retrieve_earthquake(
        request(params={"latitude": "1"}, route_params={"id": RECORD_ID})
    )

    assert response.status_code == 400


def test_exact_endpoint_rejects_invalid_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(exact=record())
    install_service(monkeypatch, service)

    response = retrieve_earthquake(request(route_params={"id": "not-a-uuid"}))

    assert response.status_code == 400


def test_exact_endpoint_returns_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    install_service(monkeypatch, service)

    response = retrieve_earthquake(request(route_params={"id": RECORD_ID}))

    assert response.status_code == 404


def test_endpoint_maps_cosmos_unavailable_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(error=EarthquakeQueryUnavailableError("unavailable"))
    install_service(monkeypatch, service)

    response = retrieve_earthquakes(request())

    assert response.status_code == 503


def test_endpoint_maps_invalid_application_configuration_to_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COSMOS_EARTHQUAKE_DB_CONNECTION_STRING", raising=False)

    response = retrieve_earthquakes(request())

    assert response.status_code == 500
    assert json.loads(response.get_body())["reason"] == (
        "Earthquake retrieval is not configured"
    )
