"""Publish normalized earthquake envelopes to Azure IoT Hub."""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from azure.iot.device import IoTHubDeviceClient, Message

from models import EarthquakeEnvelope


@dataclass(frozen=True)
class PublishResult:
    """Counts produced by one IoT Hub publishing operation."""

    published: int
    failed: int


class IoTHubPublisher:
    """Publish one JSON message per normalized earthquake event."""

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string

    def publish(self, events: Iterable[EarthquakeEnvelope]) -> PublishResult:
        """Publish all events, continuing after individual message failures."""
        client = IoTHubDeviceClient.create_from_connection_string(
            self._connection_string
        )
        client.connect()

        published = 0
        failed = 0
        try:
            for event in events:
                try:
                    message = Message(  # type: ignore[no-untyped-call]
                        json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    )
                    message.content_type = "application/json"
                    message.content_encoding = "utf-8"
                    message.message_id = event["id"]
                    client.send_message(message)
                    published += 1
                except Exception:
                    failed += 1
                    logging.exception(
                        "Failed to publish earthquake event %s", event["id"]
                    )
        finally:
            client.shutdown()

        return PublishResult(published=published, failed=failed)
