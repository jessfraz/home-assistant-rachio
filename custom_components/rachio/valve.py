"""Native water valves for Rachio Smart Hose Timers."""

from datetime import timedelta
from typing import override

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MANUAL_RUN_MINS, DEFAULT_MANUAL_RUN_MINS, KEY_CURRENT_STATUS
from .device import RachioConfigEntry
from .entity import RachioHoseTimerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RachioConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up native Smart Hose Timer valves."""
    person = config_entry.runtime_data
    async_add_entities(
        RachioWaterValve(person, base_station, valve, base_station.status_coordinator)
        for base_station in person.base_stations
        for valve in base_station.status_coordinator.data.values()
    )


class RachioWaterValve(RachioHoseTimerEntity, ValveEntity):
    """Represent a Smart Hose Timer as a native water valve."""

    _attr_device_class = ValveDeviceClass.WATER
    _attr_name = None
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(self, person, base, data, coordinator) -> None:
        """Initialize a Smart Hose Timer valve."""
        super().__init__(data, coordinator)
        self._person = person
        self._base = base
        self._attr_unique_id = f"{self.id}-water-valve"
        self._update_attr()

    @override
    async def async_open_valve(self) -> None:
        """Start watering for the configured manual run duration."""
        run_time = timedelta(
            minutes=self._person.config_entry.options.get(
                CONF_MANUAL_RUN_MINS, DEFAULT_MANUAL_RUN_MINS
            )
        )
        await self.hass.async_add_executor_job(
            self._base.start_watering, self.id, int(run_time.total_seconds())
        )
        self._attr_is_closed = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @override
    async def async_close_valve(self) -> None:
        """Stop watering."""
        await self.hass.async_add_executor_job(self._base.stop_watering, self.id)
        self._attr_is_closed = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    @override
    def _update_attr(self) -> None:
        """Update state from the coordinator."""
        self._attr_is_closed = KEY_CURRENT_STATUS not in self.reported_state
