"""Configurable values for Rachio Smart Hose Timers."""

from typing import override

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KEY_DEFAULT_RUNTIME
from .device import RachioConfigEntry
from .entity import RachioHoseTimerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RachioConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Smart Hose Timer numbers."""
    person = config_entry.runtime_data
    async_add_entities(
        RachioDefaultRuntimeNumber(base_station, valve, base_station.status_coordinator)
        for base_station in person.base_stations
        for valve in base_station.status_coordinator.data.values()
    )


class RachioDefaultRuntimeNumber(RachioHoseTimerEntity, NumberEntity):
    """Set the runtime used by the timer's physical button."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 1440
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_translation_key = "default_runtime"

    def __init__(self, base, data, coordinator) -> None:
        """Initialize the default runtime number."""
        super().__init__(data, coordinator)
        self._base = base
        self._attr_unique_id = f"{self.id}-default-runtime"
        self._update_attr()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set the physical-button runtime through Rachio's documented endpoint."""
        await self.hass.async_add_executor_job(
            self._base.set_default_runtime, self.id, round(value * 60)
        )
        self._attr_native_value = value
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    @override
    def _update_attr(self) -> None:
        """Update the configured runtime from reported valve state."""
        runtime = self.reported_state.get(KEY_DEFAULT_RUNTIME)
        try:
            self._attr_native_value = (
                float(runtime) / 60 if runtime is not None else None
            )
        except TypeError, ValueError:
            self._attr_native_value = None
