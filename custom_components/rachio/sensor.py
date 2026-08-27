"""Event-backed sensors for Rachio Smart Hose Timers."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.dt import parse_datetime

from .device import RachioConfigEntry
from .entity import RachioHoseTimerEntity

RUN_REASON_OPTIONS = ["button_press", "quick_run", "program"]


@dataclass(frozen=True, kw_only=True)
class RachioSensorEntityDescription(SensorEntityDescription):
    """Describe a Smart Hose Timer event sensor."""

    value_fn: Callable[[Mapping[str, str]], str | datetime | None]


def _timestamp_value(key: str) -> Callable[[Mapping[str, str]], datetime | None]:
    """Create a safe timestamp extractor."""

    def _value(metadata: Mapping[str, str]) -> datetime | None:
        if not (value := metadata.get(key)):
            return None
        return parse_datetime(value)

    return _value


def _run_reason_value(metadata: Mapping[str, str]) -> str | None:
    """Normalize documented Rachio run reasons for an enum sensor."""
    if not (value := metadata.get("last_run_reason")):
        return None
    value = value.lower()
    return value if value in RUN_REASON_OPTIONS else None


SENSOR_DESCRIPTIONS = (
    RachioSensorEntityDescription(
        key="last_watering_end",
        translation_key="last_watering_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp_value("last_watering_end"),
    ),
    RachioSensorEntityDescription(
        key="last_run_reason",
        translation_key="last_run_reason",
        device_class=SensorDeviceClass.ENUM,
        options=RUN_REASON_OPTIONS,
        value_fn=_run_reason_value,
    ),
    RachioSensorEntityDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_timestamp_value("last_seen"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RachioConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up authenticated webhook-backed sensors."""
    person = config_entry.runtime_data
    async_add_entities(
        RachioHoseTimerSensor(
            valve,
            base_station.status_coordinator,
            description,
        )
        for base_station in person.base_stations
        for valve in base_station.status_coordinator.data.values()
        for description in SENSOR_DESCRIPTIONS
    )


class RachioHoseTimerSensor(RachioHoseTimerEntity, SensorEntity):
    """Represent metadata from authenticated current-generation webhooks."""

    entity_description: RachioSensorEntityDescription

    def __init__(self, data, coordinator, description) -> None:
        """Initialize a Smart Hose Timer sensor."""
        super().__init__(data, coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self.id}-{description.key}"
        self._update_attr()

    @callback
    @override
    def _update_attr(self) -> None:
        """Update from verified event metadata."""
        metadata: Mapping[str, str] = self.coordinator.event_data.get(self.id, {})
        self._attr_native_value = self.entity_description.value_fn(metadata)
