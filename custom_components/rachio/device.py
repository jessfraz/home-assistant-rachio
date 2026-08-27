"""Adapter to wrap the rachiopy api for home assistant."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.storage import Store
from rachiopy import Rachio
from requests.exceptions import RequestException

from .const import (
    KEY_BASE_STATIONS,
    KEY_DEVICES,
    KEY_ENABLED,
    KEY_EXTERNAL_ID,
    KEY_FLEX_SCHEDULES,
    KEY_ID,
    KEY_MAC_ADDRESS,
    KEY_MODEL,
    KEY_NAME,
    KEY_SCHEDULES,
    KEY_SERIAL_NUMBER,
    KEY_STATUS,
    KEY_USERNAME,
    KEY_ZONES,
    LISTEN_EVENT_TYPES,
    WEBHOOK_CONST_ID,
)
from .coordinator import RachioScheduleUpdateCoordinator, RachioUpdateCoordinator
from .response_helpers import RachioResponse, RachioResponseError, require_success
from .v2_api import RachioV2WebhookError, RachioV2WebhookManager
from .webhook_helpers import ValveEventOrder

_LOGGER = logging.getLogger(__name__)

PERMISSION_ERROR = "7"
EVENT_ORDER_STORE_VERSION = 1
EVENT_ORDER_STORE_DELAY = 1
EVENT_ORDER_STORES = "rachio_v2_event_order_stores"


def event_order_store_key(entry_id: str) -> str:
    """Return the config-entry-scoped replay-protection Store key."""
    return f"rachio.v2_event_order.{entry_id}"


type RachioConfigEntry = ConfigEntry[RachioPerson]


class RachioPerson:
    """Represent a Rachio user."""

    def __init__(self, rachio: Rachio, config_entry: RachioConfigEntry) -> None:
        """Create an object from the provided API instance."""
        # Use API token to get user ID
        self.rachio = rachio
        self.config_entry = config_entry
        self.username = None
        self._id: str | None = None
        self._controllers: list[RachioIro] = []
        self._base_stations: list[RachioBaseStation] = []
        self._v2_event_order = ValveEventOrder()
        self._v2_event_store: Store[dict[str, Any]] | None = None

    async def async_setup(self, hass: HomeAssistant) -> None:
        """Create rachio devices."""
        await hass.async_add_executor_job(self._setup, hass)

    def _setup(self, hass: HomeAssistant) -> None:
        """Rachio device setup."""
        rachio = self.rachio

        response = rachio.person.info()
        if is_invalid_auth_code(int(response[0][KEY_STATUS])):
            raise ConfigEntryAuthFailed(f"API key error: {response}")
        if int(response[0][KEY_STATUS]) != HTTPStatus.OK:
            raise ConfigEntryNotReady(f"API Error: {response}")
        self._id = response[1][KEY_ID]

        # Use user ID to get user data
        data = rachio.person.get(self._id)
        if is_invalid_auth_code(int(data[0][KEY_STATUS])):
            raise ConfigEntryAuthFailed(f"User ID error: {data}")
        if int(data[0][KEY_STATUS]) != HTTPStatus.OK:
            raise ConfigEntryNotReady(f"API Error: {data}")
        self.username = data[1][KEY_USERNAME]
        devices: list[dict[str, Any]] = data[1][KEY_DEVICES]
        base_station_data = rachio.valve.list_base_stations(self._id)
        base_stations: list[dict[str, Any]] = base_station_data[1][KEY_BASE_STATIONS]

        for controller in devices:
            webhooks = rachio.notification.get_device_webhook(controller[KEY_ID])[1]
            # The API does not provide a way to tell if a controller is shared
            # or if they are the owner. To work around this problem
            # we fetch the webhooks
            # before we setup the device so we can skip it instead of failing.
            # webhooks are normally a list, however if there is an error
            # rachio hands us back a dict
            if isinstance(webhooks, dict):
                if webhooks.get("code") == PERMISSION_ERROR:
                    _LOGGER.warning(
                        (
                            "Not adding controller '%s', only controllers owned by '%s'"
                            " may be added"
                        ),
                        controller[KEY_NAME],
                        self.username,
                    )
                else:
                    _LOGGER.error(
                        "Failed to add rachio controller '%s' because of an error: %s",
                        controller[KEY_NAME],
                        webhooks.get("error", "Unknown Error"),
                    )
                continue

            rachio_iro = RachioIro(hass, rachio, controller, webhooks)
            rachio_iro.setup()
            self._controllers.append(rachio_iro)

        base_count = len(base_stations)
        self._base_stations.extend(
            RachioBaseStation(
                rachio,
                base,
                RachioUpdateCoordinator(
                    hass, rachio, self.config_entry, base, base_count
                ),
                RachioScheduleUpdateCoordinator(hass, rachio, self.config_entry, base),
            )
            for base in base_stations
        )

        _LOGGER.debug('Using Rachio API as user "%s"', self.username)

    @property
    def user_id(self) -> str | None:
        """Get the user ID as defined by the Rachio API."""
        return self._id

    @property
    def controllers(self) -> list[RachioIro]:
        """Get a list of controllers managed by this account."""
        return self._controllers

    @property
    def base_stations(self) -> list[RachioBaseStation]:
        """List of smart hose timer base stations."""
        return self._base_stations

    def start_multiple_zones(self, zones) -> None:
        """Start multiple zones."""
        self.rachio.zone.start_multiple(zones)

    def setup_v2_webhooks(self, webhook_url: str, webhook_id: str) -> list[str]:
        """Register current Smart Hose Timer webhooks without touching legacy hooks."""
        manager = RachioV2WebhookManager(self.rachio, webhook_url, webhook_id)
        failures: list[str] = []
        for base_station in self.base_stations:
            for valve_id in base_station.status_coordinator.data or {}:
                try:
                    manager.ensure_valve_webhook(valve_id)
                except (RachioV2WebhookError, RequestException) as err:
                    failures.append(f"{valve_id}: {err}")
        return failures

    def remove_v2_webhooks(self, webhook_url: str, webhook_id: str) -> list[str]:
        """Remove only current webhooks owned by this config entry."""
        manager = RachioV2WebhookManager(self.rachio, webhook_url, webhook_id)
        failures: list[str] = []
        for base_station in self.base_stations:
            for valve_id in base_station.status_coordinator.data or {}:
                try:
                    manager.remove_owned_valve_webhooks(valve_id)
                except (RachioV2WebhookError, RequestException) as err:
                    failures.append(f"{valve_id}: {err}")
        return failures

    def remove_legacy_webhooks(self) -> list[str]:
        """Remove controller webhooks and their shutdown listeners."""
        failures: list[str] = []
        for controller in self.controllers:
            try:
                controller.unload()
            except Exception as err:
                failures.append(f"{controller.controller_id}: {err}")
        return failures

    async def async_load_v2_event_order(self, hass: HomeAssistant) -> None:
        """Load persisted per-valve replay protection before accepting webhooks."""
        store = Store[dict[str, Any]](
            hass,
            EVENT_ORDER_STORE_VERSION,
            event_order_store_key(self.config_entry.entry_id),
            private=True,
        )
        stores: dict[str, Store[dict[str, Any]]] = hass.data.setdefault(
            EVENT_ORDER_STORES, {}
        )
        store = stores.setdefault(self.config_entry.entry_id, store)
        self._v2_event_store = store
        self._v2_event_order = ValveEventOrder.from_store_data(await store.async_load())
        restored_metadata = self._v2_event_order.metadata_by_valve()
        for base_station in self.base_stations:
            valve_ids = base_station.status_coordinator.data or {}
            base_station.status_coordinator.event_data.update(
                {
                    valve_id: metadata
                    for valve_id, metadata in restored_metadata.items()
                    if valve_id in valve_ids
                }
            )

    async def async_flush_v2_event_order(self) -> None:
        """Flush the same Store instance before runtime data is discarded."""
        if self._v2_event_store is not None:
            await self._v2_event_store.async_save(self._v2_event_order.as_store_data())

    def handle_v2_webhook(self, event: dict[str, Any]) -> bool:
        """Apply only a valid event newer than this valve's persisted cursor."""
        if not self._v2_event_order.would_accept(event):
            return False
        for base_station in self.base_stations:
            if (
                metadata := base_station.status_coordinator.async_handle_v2_event(event)
            ) is not None:
                if not self._v2_event_order.record(event, metadata):
                    return False
                if self._v2_event_store is not None:
                    self._v2_event_store.async_delay_save(
                        self._v2_event_order.as_store_data, EVENT_ORDER_STORE_DELAY
                    )
                return True
        return False


class RachioIro:
    """Represent a Rachio Iro."""

    def __init__(
        self,
        hass: HomeAssistant,
        rachio: Rachio,
        data: dict[str, Any],
        webhooks: list[dict[str, Any]],
    ) -> None:
        """Initialize a Rachio device."""
        self.hass = hass
        self.rachio = rachio
        self._id = data[KEY_ID]
        self.name = data[KEY_NAME]
        self.serial_number = data[KEY_SERIAL_NUMBER]
        self.mac_address = data[KEY_MAC_ADDRESS]
        self.model = data[KEY_MODEL]
        self._zones = data[KEY_ZONES]
        self._schedules = data[KEY_SCHEDULES]
        self._flex_schedules = data[KEY_FLEX_SCHEDULES]
        self._init_data = data
        self._webhooks: list[dict[str, Any]] = webhooks
        self._current_webhook_id: str | None = None
        self._remove_stop_listener: CALLBACK_TYPE | None = None
        _LOGGER.debug('%s has ID "%s"', self, self.controller_id)

    def setup(self) -> None:
        """Rachio Iro setup for webhooks."""
        # Listen for all updates
        self._init_webhooks()

    def unload(self) -> None:
        """Remove config-entry-owned and shutdown-owned webhook state."""
        if self._remove_stop_listener is not None:
            self._remove_stop_listener()
            self._remove_stop_listener = None
        self._deinit_webhooks()

    def _deinit_webhooks(self) -> None:
        """Stop getting updates from the Rachio API."""
        if not self._webhooks:
            # Fetch again because setup clears the initial snapshot after using it.
            self._webhooks = self.rachio.notification.get_device_webhook(
                self.controller_id
            )[1]
        for webhook in self._webhooks:
            if webhook[KEY_EXTERNAL_ID].startswith(WEBHOOK_CONST_ID) or (
                webhook[KEY_ID] == self._current_webhook_id
            ):
                self.rachio.notification.delete(webhook[KEY_ID])
        self._webhooks = []

    def _handle_hass_stop(self, _) -> None:
        """Clean up owned controller webhooks during Home Assistant shutdown."""
        self._remove_stop_listener = None
        self._deinit_webhooks()

    def _init_webhooks(self) -> None:
        """Start getting updates from the Rachio API."""
        # First delete any old webhooks that may have stuck around.
        self._deinit_webhooks()

        # Choose which events to listen for and get their IDs
        event_types = [
            {"id": event_type[KEY_ID]}
            for event_type in self.rachio.notification.get_webhook_event_type()[1]
            if event_type[KEY_NAME] in LISTEN_EVENT_TYPES
        ]

        # Register to listen to these events from the device
        url = self.rachio.webhook_url
        auth = WEBHOOK_CONST_ID + self.rachio.webhook_auth
        new_webhook = self.rachio.notification.add(
            self.controller_id, auth, url, event_types
        )
        # Save ID for deletion at shutdown
        self._current_webhook_id = new_webhook[1][KEY_ID]
        self._remove_stop_listener = self.hass.bus.listen(
            EVENT_HOMEASSISTANT_STOP, self._handle_hass_stop
        )

    @override
    def __str__(self) -> str:
        """Display the controller as a string."""
        return f'Rachio controller "{self.name}"'

    @property
    def controller_id(self) -> str:
        """Return the Rachio API controller ID."""
        return self._id

    @property
    def current_schedule(self) -> str:
        """Return the schedule that the device is running right now."""
        return self.rachio.device.current_schedule(self.controller_id)[1]

    @property
    def init_data(self) -> dict:
        """Return the information used to set up the controller."""
        return self._init_data

    def list_zones(self, include_disabled=False) -> list:
        """Return a list of the zone dicts connected to the device."""
        # All zones
        if include_disabled:
            return self._zones

        # Only enabled zones
        return [z for z in self._zones if z[KEY_ENABLED]]

    def get_zone(self, zone_id) -> dict | None:
        """Return the zone with the given ID."""
        for zone in self.list_zones(include_disabled=True):
            if zone[KEY_ID] == zone_id:
                return zone

        return None

    def list_schedules(self) -> list:
        """Return a list of fixed schedules."""
        return self._schedules

    def list_flex_schedules(self) -> list:
        """Return a list of flex schedules."""
        return self._flex_schedules

    def stop_watering(self) -> None:
        """Stop watering all zones connected to this controller."""
        self.rachio.device.stop_water(self.controller_id)
        _LOGGER.debug("Stopped watering of all zones on %s", self)

    def pause_watering(self, duration) -> None:
        """Pause watering on this controller."""
        self.rachio.device.pause_zone_run(self.controller_id, duration * 60)
        _LOGGER.debug("Paused watering on %s for %s minutes", self, duration)

    def resume_watering(self) -> None:
        """Resume paused watering on this controller."""
        self.rachio.device.resume_zone_run(self.controller_id)
        _LOGGER.debug("Resuming watering on %s", self)


class RachioBaseStation:
    """Represent a smart hose timer base station."""

    def __init__(
        self,
        rachio: Rachio,
        data: dict[str, Any],
        status_coordinator: RachioUpdateCoordinator,
        schedule_coordinator: RachioScheduleUpdateCoordinator,
    ) -> None:
        """Initialize a smart hose timer base station."""
        self.rachio = rachio
        self._id = data[KEY_ID]
        self.status_coordinator = status_coordinator
        self.schedule_coordinator = schedule_coordinator

    def start_watering(self, valve_id: str, duration: int) -> None:
        """Start watering on this valve."""
        self._require_success(
            self.rachio.valve.start_watering(valve_id, duration), "start watering"
        )

    def stop_watering(self, valve_id: str) -> None:
        """Stop watering on this valve."""
        self._require_success(
            self.rachio.valve.stop_watering(valve_id), "stop watering"
        )

    def set_default_runtime(self, valve_id: str, duration: int) -> None:
        """Set the runtime used by the timer's physical button."""
        self._require_success(
            self.rachio.valve.set_default_runtime(valve_id, duration),
            "set default runtime",
        )

    @staticmethod
    def _require_success(response: RachioResponse, operation: str) -> None:
        """Map Rachio response failures to Home Assistant action failures."""
        try:
            require_success(response, operation)
        except RachioResponseError as err:
            raise HomeAssistantError(str(err)) from err

    def create_skip(self, program_id: str, timestamp: str) -> None:
        """Create a skip for a scheduled event."""
        self.rachio.program.create_skip_overrides(program_id, timestamp)


def is_invalid_auth_code(http_status_code: int) -> bool:
    """HTTP status codes that mean invalid auth."""
    return http_status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
