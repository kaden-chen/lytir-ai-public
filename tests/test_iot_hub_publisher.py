import json

import pytest
from azure.iot.device import Message

from models import EarthquakeEnvelope, EarthquakeMetadata
from services import IoTHubPublisher
from services.iot_hub_publisher import PublishResult


class FakeIoTHubClient:
    def __init__(self, failing_ids: set[str] | None = None) -> None:
        self.connected = False
        self.shutdown_called = False
        self.messages: list[Message] = []
        self.failing_ids = failing_ids or set()

    def connect(self) -> None:
        self.connected = True

    def send_message(self, message: Message) -> None:
        if message.message_id in self.failing_ids:
            raise RuntimeError("simulated publish failure")
        self.messages.append(message)

    def shutdown(self) -> None:
        self.shutdown_called = True


def envelope(event_id: str) -> EarthquakeEnvelope:
    metadata: EarthquakeMetadata = {
        "event_type": "earthquake",
        "magnitude": 2.4,
        "place": "Example",
        "occurred_at_utc": "2026-09-11T12:30:00Z",
        "updated_at_utc": None,
        "longitude": -122.4,
        "latitude": 38.7,
        "depth_km": 4.2,
        "source_url": None,
    }
    return {
        "id": event_id,
        "schema_version": 1,
        "source": "usgs",
        "metadata": metadata,
        "event": {"type": "Feature", "id": event_id},
    }


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeIoTHubClient,
) -> None:
    class FakeClientFactory:
        @staticmethod
        def create_from_connection_string(_connection_string: str) -> FakeIoTHubClient:
            return client

    monkeypatch.setattr(
        "services.iot_hub_publisher.IoTHubDeviceClient", FakeClientFactory
    )


def test_publisher_sends_json_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeIoTHubClient()
    install_fake_client(monkeypatch, client)

    result = IoTHubPublisher("not-a-real-connection-string").publish(
        [envelope("event-1")]
    )

    assert result == PublishResult(published=1, failed=0)
    assert client.connected
    assert client.shutdown_called
    assert client.messages[0].message_id == "event-1"
    assert client.messages[0].content_type == "application/json"
    assert client.messages[0].content_encoding == "utf-8"
    assert json.loads(client.messages[0].data)["event"]["id"] == "event-1"


def test_publisher_continues_after_message_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeIoTHubClient(failing_ids={"event-1"})
    install_fake_client(monkeypatch, client)

    result = IoTHubPublisher("not-a-real-connection-string").publish(
        [envelope("event-1"), envelope("event-2")]
    )

    assert result == PublishResult(published=1, failed=1)
    assert [message.message_id for message in client.messages] == ["event-2"]
    assert client.shutdown_called
