"""Home Assistant 2026.8.3 integration contract tests."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.exceptions import (
    ConnectionError,
    ConnectTimeout,
    ReadTimeout,
    RequestException,
)

VALVE_ID = "valve-one"
WEBHOOK_URL = "https://example.test/api/webhook/existing-webhook-id"
CONF_MANUAL_RUN_MINS = "manual_run_mins"
EVENT_ORDER_STORES = "rachio_v2_event_order_stores"


async def _setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, mock_rachio
) -> tuple:
    """Set up through Home Assistant's real config-entry machinery."""
    entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook") as register,
        patch.object(component, "async_unregister_webhook") as unregister,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return register, unregister


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("private connection diagnostic"),
        ConnectTimeout("private connection diagnostic"),
        ReadTimeout("private connection diagnostic"),
    ],
    ids=["connection-error", "connect-timeout", "read-timeout"],
)
async def test_setup_retries_transport_failure_and_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_rachio: MagicMock,
    caplog: pytest.LogCaptureFixture,
    failure: RequestException,
) -> None:
    """Let HA retry transport failures without exposing their raw diagnostic."""
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    mock_rachio.person.info.side_effect = failure
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook") as register,
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
        assert mock_config_entry.reason == "Could not connect to the Rachio API"
        assert hass.states.get("valve.dog_water") is None
        register.assert_not_called()
        assert not any(
            record.name == "custom_components.rachio"
            and record.levelno >= logging.ERROR
            for record in caplog.records
        )

        mock_rachio.person.info.side_effect = None
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert hass.states.get("valve.dog_water").state == "closed"
        register.assert_called_once()


async def test_setup_preserves_ids_and_prefers_native_valve(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Expose native entities on the same device without replacing legacy IDs."""
    await _setup_entry(hass, mock_config_entry, mock_rachio)

    registry = er.async_get(hass)
    valve_entry = registry.async_get_entity_id(
        "valve", "rachio", f"{VALVE_ID}-water-valve"
    )
    number_entry = registry.async_get_entity_id(
        "number", "rachio", f"{VALVE_ID}-default-runtime"
    )
    legacy_switch_id = registry.async_get_entity_id(
        "switch", "rachio", f"{VALVE_ID}-valve"
    )

    assert valve_entry == "valve.dog_water"
    assert number_entry == "number.dog_water_default_runtime"
    assert legacy_switch_id == "switch.dog_water"
    assert hass.states.get(valve_entry).state == "closed"
    assert hass.states.get(number_entry).state == "10.0"
    assert hass.states.get(legacy_switch_id) is None
    assert (
        registry.async_get(legacy_switch_id).disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )

    device = dr.async_get(hass).async_get_device(identifiers={("rachio", VALVE_ID)})
    assert device is not None
    assert registry.async_get(valve_entry).device_id == device.id
    assert registry.async_get(number_entry).device_id == device.id
    assert registry.async_get(legacy_switch_id).device_id == device.id


async def test_failed_valve_command_does_not_write_optimistic_state(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Leave the native valve closed when Rachio rejects the command."""
    await _setup_entry(hass, mock_config_entry, mock_rachio)
    mock_rachio.valve.start_watering.return_value = ({"status": 503}, {})

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        await hass.services.async_call(
            VALVE_DOMAIN,
            "open_valve",
            {ATTR_ENTITY_ID: "valve.dog_water"},
            blocking=True,
        )

    assert hass.states.get("valve.dog_water").state == "closed"


async def test_remote_failure_does_not_block_setup_or_unload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Keep polling usable and always complete the local lifecycle."""
    mock_rachio.valve.valve_get_request.return_value = ({"status": 503}, {})
    mock_rachio.valve.valve_get_request.side_effect = None
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()

    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook") as register,
        patch.object(component, "async_unregister_webhook") as unregister,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("valve.dog_water") is not None
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    register.assert_called_once()
    unregister.assert_called_once()
    assert hass.states.get("valve.dog_water").state == STATE_UNAVAILABLE


@pytest.mark.parametrize("unload_error", [None, RuntimeError("platform unload failed")])
async def test_failed_platform_unload_preserves_all_lifecycle_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_rachio,
    unload_error: RuntimeError | None,
) -> None:
    """Leave local, persisted, and remote state intact after failed unload."""
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook"),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)

    person = mock_config_entry.runtime_data
    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(
                return_value=False,
                side_effect=unload_error,
            ),
        ),
        patch.object(component, "async_unregister_webhook") as unregister,
        patch.object(person, "async_flush_v2_event_order", AsyncMock()) as flush,
        patch.object(person, "remove_v2_webhooks") as remove_v2,
        patch.object(person, "remove_legacy_webhooks") as remove_legacy,
    ):
        if unload_error is None:
            assert not await component.async_unload_entry(hass, mock_config_entry)
        else:
            with pytest.raises(RuntimeError, match="platform unload failed"):
                await component.async_unload_entry(hass, mock_config_entry)

    unregister.assert_not_called()
    flush.assert_not_awaited()
    remove_v2.assert_not_called()
    remove_legacy.assert_not_called()


async def test_legacy_controller_webhook_and_listener_unload_with_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Do not accumulate legacy controller webhook listeners across reloads."""
    controller_id = "controller-one"
    registrations: list[dict[str, str]] = []
    mock_rachio.person.get.return_value = (
        {"status": 200},
        {
            "username": "test-user",
            "devices": [
                {
                    "id": controller_id,
                    "name": "Front Irrigation",
                    "serialNumber": "IRO123",
                    "macAddress": "00:11:22:33:44:55",
                    "model": "GENERATION3",
                    "zones": [],
                    "scheduleRules": [],
                    "flexScheduleRules": [],
                    "status": "ONLINE",
                    "rainSensorTripped": False,
                    "on": True,
                }
            ],
        },
    )
    mock_rachio.valve.list_base_stations.return_value = (
        {"status": 200},
        {"baseStations": []},
    )
    mock_rachio.device.current_schedule.return_value = ({"status": 200}, {})
    mock_rachio.notification.get_device_webhook.side_effect = lambda _controller_id: (
        {"status": 200},
        list(registrations),
    )
    mock_rachio.notification.get_webhook_event_type.return_value = (
        {"status": 200},
        [
            {"id": "device-status", "name": "DEVICE_STATUS_EVENT"},
            {"id": "zone-status", "name": "ZONE_STATUS_EVENT"},
        ],
    )

    def add_webhook(_controller_id, external_id, url, _event_types):
        registration = {
            "id": "legacy-current",
            "externalId": external_id,
            "url": url,
        }
        registrations.append(registration)
        return {"status": 200}, registration

    def delete_webhook(webhook_id):
        registrations[:] = [
            registration
            for registration in registrations
            if registration["id"] != webhook_id
        ]
        return {"status": 204}, {}

    mock_rachio.notification.add.side_effect = add_webhook
    mock_rachio.notification.delete.side_effect = delete_webhook
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook"),
        patch.object(component, "async_unregister_webhook"),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        assert registrations == [
            {
                "id": "legacy-current",
                "externalId": mock_rachio.notification.add.call_args.args[1],
                "url": WEBHOOK_URL,
            }
        ]
        person = mock_config_entry.runtime_data
        controller = person.controllers[0]
        assert controller._remove_stop_listener is not None
        stop_listeners_loaded = hass.bus.async_listeners().get(
            EVENT_HOMEASSISTANT_STOP, 0
        )
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    assert registrations == []
    assert controller._remove_stop_listener is None
    assert (
        hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STOP, 0)
        < stop_listeners_loaded
    )
    mock_rachio.notification.delete.assert_called_once_with("legacy-current")


async def test_unload_flushes_and_remove_cancels_same_store(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Flush a pending cursor write, then remove that same Store owner."""
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        patch.object(component, "async_register_webhook"),
        patch.object(component, "async_unregister_webhook"),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        person = mock_config_entry.runtime_data
        store = person._v2_event_store
        assert store is not None
        event = {
            "eventId": "persist-me",
            "eventType": "VALVE_RUN_END_EVENT",
            "payload": {},
            "resourceId": VALVE_ID,
            "resourceType": "VALVE",
            "timestamp": "2026-08-27T01:02:03Z",
        }
        assert person._v2_event_order.record(
            event, {"last_watering_end": event["timestamp"]}
        )
        store.async_delay_save(person._v2_event_order.as_store_data, 3600)

        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    stored = await store.async_load()
    assert stored is not None
    assert stored["valves"][VALVE_ID]["event_id"] == "persist-me"
    assert hass.data[EVENT_ORDER_STORES][mock_config_entry.entry_id] is store
    remove_store = AsyncMock(wraps=store.async_remove)
    store.async_remove = remove_store
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.entry_id not in hass.data[EVENT_ORDER_STORES]
    remove_store.assert_awaited_once_with()
    assert store._delay_handle is None


async def test_setup_propagates_auth_failure(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Let Home Assistant start reauthentication for an invalid saved key."""
    mock_rachio.person.info.return_value = ({"status": 401}, {})
    mock_config_entry.add_to_hass(hass)
    component = await (
        await async_get_integration(hass, "rachio")
    ).async_get_component()
    with (
        patch.object(
            component,
            "async_get_or_create_registered_webhook_id_and_url",
            return_value=WEBHOOK_URL,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await component.async_setup_entry(hass, mock_config_entry)


async def test_reauth_replaces_only_api_key(hass: HomeAssistant, mock_rachio) -> None:
    """Preserve webhook identity and options when replacing an API key."""
    entry = MockConfigEntry(
        domain="rachio",
        data={
            "api_key": "old-key",
            "webhook_id": "existing-webhook-id",
            "cloudhook_url": "https://hooks.example.test/existing",
            "future_core_field": "keep-me",
        },
        options={CONF_MANUAL_RUN_MINS: 23},
        unique_id="existing-user",
    )
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ):
        result = await hass.config_entries.flow.async_init(
            "rachio",
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "replacement-key"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        "api_key": "replacement-key",
        "webhook_id": "existing-webhook-id",
        "cloudhook_url": "https://hooks.example.test/existing",
        "future_core_field": "keep-me",
    }
    assert entry.options == {CONF_MANUAL_RUN_MINS: 23}


async def test_manual_runtime_option_is_bounded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_rachio
) -> None:
    """Reject values outside the supported one-day runtime range."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    schema = result["data_schema"]

    assert schema({CONF_MANUAL_RUN_MINS: "1"}) == {CONF_MANUAL_RUN_MINS: 1}
    assert schema({CONF_MANUAL_RUN_MINS: 1440}) == {CONF_MANUAL_RUN_MINS: 1440}
    with pytest.raises(vol.Invalid):
        schema({CONF_MANUAL_RUN_MINS: 0})
    with pytest.raises(vol.Invalid):
        schema({CONF_MANUAL_RUN_MINS: 1441})
