"""Home Assistant fixtures for the custom Rachio integration."""

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import loader
from homeassistant.const import CONF_API_KEY, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = ["pytest_homeassistant_custom_component"]

BASE_STATION_ID = "base-station-one"
BASE_STATION_SERIAL = "BASE123"
VALVE_ID = "valve-one"
WEBHOOK_ID = "existing-webhook-id"
WEBHOOK_URL = "https://example.test/api/webhook/existing-webhook-id"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Load this repository's integration instead of HA Core's copy."""
    component_root = str(Path(__file__).parents[1] / "custom_components")
    custom_components = sys.modules.get("custom_components")
    if custom_components is None:
        custom_components = ModuleType("custom_components")
        custom_components.__path__ = [component_root]
        sys.modules["custom_components"] = custom_components
    elif component_root not in custom_components.__path__:
        custom_components.__path__.insert(0, component_root)
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an existing Rachio config entry from the Core integration."""
    return MockConfigEntry(
        domain="rachio",
        data={
            CONF_API_KEY: "test-api-key",
            CONF_WEBHOOK_ID: WEBHOOK_ID,
        },
        unique_id="test-user-id",
    )


@pytest.fixture
def mock_rachio() -> Generator[MagicMock]:
    """Provide a stateful fake at the RachioPy boundary."""
    registrations: list[dict] = []
    cloud = ModuleType("homeassistant.components.cloud")

    class CloudNotConnected(HomeAssistantError):
        """Stand in for the cloud integration's connectivity exception."""

    cloud.CloudNotConnected = CloudNotConnected
    cloud.async_active_subscription = MagicMock(return_value=False)
    cloud.async_create_cloudhook = AsyncMock()
    cloud.async_delete_cloudhook = AsyncMock()

    with (
        patch.dict(sys.modules, {"homeassistant.components.cloud": cloud}),
        patch("rachiopy.Rachio") as rachio_class,
    ):
        rachio = rachio_class.return_value
        rachio.person.info.return_value = ({"status": 200}, {"id": "test-user-id"})
        rachio.person.get.return_value = (
            {"status": 200},
            {"username": "test-user", "devices": []},
        )
        rachio.valve.list_base_stations.return_value = (
            {"status": 200},
            {
                "baseStations": [
                    {"id": BASE_STATION_ID, "serialNumber": BASE_STATION_SERIAL}
                ]
            },
        )
        rachio.valve.list_valves.return_value = (
            {"status": 200},
            {
                "valves": [
                    {
                        "id": VALVE_ID,
                        "name": "Dog Water",
                        "detectFlow": False,
                        "state": {
                            "reportedState": {
                                "batteryStatus": "GOOD",
                                "connected": True,
                                "defaultRuntimeSeconds": 600,
                            }
                        },
                    }
                ]
            },
        )
        rachio.summary.get_valve_day_views.return_value = (
            {"status": 200},
            {"valveDayViews": []},
        )
        rachio.valve.start_watering.return_value = ({"status": 204}, {})
        rachio.valve.stop_watering.return_value = ({"status": 204}, {})
        rachio.valve.set_default_runtime.return_value = ({"status": 204}, {})

        def list_webhooks(path: str):
            assert path.startswith("webhook/listWebhooks?")
            return {"status": 200}, {"webhooks": list(registrations)}

        def create_webhook(path: str, body: dict):
            assert path == "webhook/createWebhook"
            registrations.append(
                {
                    "id": "remote-webhook-one",
                    "externalId": body["externalId"],
                    "url": body["url"],
                    "eventTypes": body["eventTypes"],
                }
            )
            return {"status": 200}, {}

        def delete_webhook(path: str):
            webhook_id = path.removeprefix("webhook/deleteWebhook/")
            registrations[:] = [
                item for item in registrations if item["id"] != webhook_id
            ]
            return {"status": 200}, {}

        rachio.valve.valve_get_request.side_effect = list_webhooks
        rachio.valve.valve_post_request.side_effect = create_webhook
        rachio.valve.valve_delete_request.side_effect = delete_webhook
        rachio.test_registrations = registrations

        yield rachio
