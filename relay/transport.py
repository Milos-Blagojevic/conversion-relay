"""HTTP for the platform clients. Live mode uses `requests`; sandbox mode swaps in the simulator.

The platform clients (`meta.py`, `google.py`) build the same requests in both modes, so the sandbox
exercises the code that talks to Meta and Google, not a stand-in for it.
"""

from __future__ import annotations

from typing import Any, Protocol

import requests

TIMEOUT_SECONDS = 15


class Response(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


class PlatformError(RuntimeError):
    """A platform API answered with an error (or not at all) on a monitoring call."""


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        data: dict | None = None,
        headers: dict | None = None,
    ) -> Response: ...


class RequestsTransport:
    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method, url, *, params=None, json=None, data=None, headers=None):
        return self.session.request(
            method, url, params=params, json=json, data=data, headers=headers, timeout=self.timeout
        )


def body(response: Response) -> Any:
    """The JSON body, or the raw text when the platform answered with something else."""
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:500]}
