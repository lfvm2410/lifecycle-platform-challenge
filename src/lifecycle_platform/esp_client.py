"""Stub of the platform-provided ``ESPClient`` interface.

The challenge prompt instructs us NOT to modify the interface. In production
this module would be replaced by the real ESP SDK (e.g. Twilio, MessageBird).
For testing we mock ``ESPClient.send_batch`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Response:
    """Minimal duck-typed Response matching the prompt's contract.

    The real ESP returns a ``requests.Response``-shaped object; we only need
    the two attributes the prompt's snippet uses.
    """

    status_code: int
    body: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        """Return the parsed JSON body, mirroring ``requests.Response.json``."""
        return self.body


class ESPClient:
    """Sends recipient batches to the Email Service Provider.

    NOTE: The signature is intentionally identical to the prompt. Concrete
    transport, auth, and retry behaviour live behind this facade so callers
    only see ``send_batch`` and a ``Response``.
    """

    def __init__(self, *, api_key: str, base_url: str = "https://esp.example.com") -> None:
        self.api_key = api_key
        self.base_url = base_url

    def send_batch(self, campaign_id: str, recipients: list[dict[str, Any]]) -> Response:
        """Sends a batch of recipients to the ESP.

        Returns a Response with .status_code and .json().
        """
        # Real implementation would POST to {base_url}/campaigns/{id}/sends
        # using self.api_key. This stub raises so anyone who wires the real
        # client without monkey-patching it in tests fails loudly.
        raise NotImplementedError(
            "ESPClient.send_batch is provided by the platform; " "tests must patch this method."
        )
