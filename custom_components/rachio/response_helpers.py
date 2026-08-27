"""Pure helpers for validating RachioPy responses."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

type RachioResponse = tuple[Mapping[str, Any], Any]


class RachioResponseError(RuntimeError):
    """Raised when a RachioPy response does not confirm success."""


def require_success(response: RachioResponse, operation: str) -> Any:
    """Return the payload only when Rachio confirmed a 2xx response."""
    headers, payload = response
    try:
        status = int(headers["status"])
    except (KeyError, TypeError, ValueError) as err:
        raise RachioResponseError(
            f"Rachio {operation} returned an invalid HTTP status"
        ) from err
    if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        raise RachioResponseError(f"Rachio {operation} returned HTTP {status}")
    return payload
