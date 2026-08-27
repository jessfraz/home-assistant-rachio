"""Deterministic tests for Rachio command response validation."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

HELPER_PATH = (
    Path(__file__).parents[1] / "custom_components" / "rachio" / "response_helpers.py"
)
SPEC = spec_from_file_location("rachio_response_helpers", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helpers = module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)


@pytest.mark.parametrize("status", [200, "201", 204, 299])
def test_require_success_accepts_every_2xx(status: int | str) -> None:
    """Return the response payload only for confirmed successes."""
    payload = {"result": "ok"}
    assert (
        helpers.require_success(({"status": status}, payload), "open valve") is payload
    )


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_require_success_rejects_4xx_and_5xx(status: int) -> None:
    """Reject command responses that must not drive optimistic entity state."""
    with pytest.raises(helpers.RachioResponseError, match=f"HTTP {status}"):
        helpers.require_success(({"status": status}, {}), "open valve")


@pytest.mark.parametrize("headers", [{}, {"status": None}, {"status": "nope"}])
def test_require_success_rejects_missing_or_invalid_status(headers: dict) -> None:
    """Fail closed when RachioPy does not return a usable HTTP status."""
    with pytest.raises(helpers.RachioResponseError, match="invalid HTTP status"):
        helpers.require_success((headers, {}), "open valve")
