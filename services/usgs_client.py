"""Download earthquake data from the configured USGS feed."""

from typing import Any, cast

import requests


class USGSClient:
    """Small HTTP client for a USGS GeoJSON feed."""

    def __init__(self, url: str, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._url = url
        self._timeout_seconds = timeout_seconds

    def download(self) -> dict[str, Any]:
        """Return the configured feed as a parsed JSON object."""
        response = requests.get(
            self._url,
            headers={"Accept": "application/geo+json"},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ValueError("USGS returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("USGS response must be a JSON object")
        return cast(dict[str, Any], payload)
