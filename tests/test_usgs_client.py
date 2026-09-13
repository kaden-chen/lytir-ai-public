from unittest.mock import Mock

import pytest
import requests

from services import USGSClient


def test_usgs_client_downloads_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"type": "FeatureCollection", "features": []}
    response = Mock()
    response.json.return_value = payload
    get = Mock(return_value=response)
    monkeypatch.setattr("services.usgs_client.requests.get", get)

    result = USGSClient("https://example.com/feed.geojson").download()

    assert result is payload
    response.raise_for_status.assert_called_once_with()
    get.assert_called_once_with(
        "https://example.com/feed.geojson",
        headers={"Accept": "application/geo+json"},
        timeout=15.0,
    )


def test_usgs_client_rejects_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock()
    response.json.return_value = []
    monkeypatch.setattr(
        "services.usgs_client.requests.get", Mock(return_value=response)
    )

    with pytest.raises(ValueError, match="must be a JSON object"):
        USGSClient("https://example.com/feed.geojson").download()


def test_usgs_client_reports_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.json.side_effect = requests.exceptions.JSONDecodeError(
        "Invalid JSON", "not-json", 0
    )
    monkeypatch.setattr(
        "services.usgs_client.requests.get", Mock(return_value=response)
    )

    with pytest.raises(ValueError, match="USGS returned invalid JSON"):
        USGSClient("https://example.com/feed.geojson").download()


def test_usgs_client_propagates_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("service unavailable")
    monkeypatch.setattr(
        "services.usgs_client.requests.get", Mock(return_value=response)
    )

    with pytest.raises(requests.HTTPError, match="service unavailable"):
        USGSClient("https://example.com/feed.geojson").download()


def test_usgs_client_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        USGSClient("https://example.com/feed.geojson", timeout_seconds=0)
