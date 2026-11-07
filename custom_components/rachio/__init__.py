"""Integration with the Rachio Iro sprinkler system controller."""

import logging
import secrets

from homeassistant.components import cloud
from homeassistant.const import CONF_API_KEY, CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from rachiopy import Rachio
from requests.exceptions import ConnectionError, Timeout

from .const import CONF_CLOUDHOOK_URL, CONF_MANUAL_RUN_MINS, DOMAIN
from .device import (
    EVENT_ORDER_STORES,
    RachioConfigEntry,
    RachioPerson,
)
from .services import async_setup_services
from .webhooks import (
    async_get_or_create_registered_webhook_id_and_url,
    async_register_webhook,
    async_unregister_webhook,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VALVE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Rachio integration."""
    async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RachioConfigEntry) -> bool:
    """Unload a config entry."""
    person = entry.runtime_data
    webhook_url = person.rachio.webhook_url
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    # Do not tear down any shared lifecycle state unless every platform agreed to
    # unload. Home Assistant keeps the entry loaded after a False result.
    if not (
        unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    ):
        return False

    async_unregister_webhook(hass, entry)
    await person.async_flush_v2_event_order()

    try:
        cleanup_failures = await hass.async_add_executor_job(
            person.remove_v2_webhooks, webhook_url, webhook_id
        )
        if cleanup_failures:
            _LOGGER.warning(
                "Could not remove some owned Rachio valve webhooks: %s",
                "; ".join(cleanup_failures),
            )
    except Exception:
        _LOGGER.exception("Unexpected error removing owned Rachio valve webhooks")

    try:
        cleanup_failures = await hass.async_add_executor_job(
            person.remove_legacy_webhooks
        )
        if cleanup_failures:
            _LOGGER.warning(
                "Could not remove some owned legacy Rachio webhooks: %s",
                "; ".join(cleanup_failures),
            )
    except Exception:
        _LOGGER.exception("Unexpected error removing owned legacy Rachio webhooks")
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: RachioConfigEntry) -> None:
    """Remove a rachio config entry."""
    if hasattr(entry, "runtime_data"):
        person = entry.runtime_data
        try:
            cleanup_failures = await hass.async_add_executor_job(
                person.remove_v2_webhooks,
                person.rachio.webhook_url,
                entry.data[CONF_WEBHOOK_ID],
            )
            if cleanup_failures:
                _LOGGER.warning(
                    "Could not remove some owned Rachio valve webhooks during config "
                    "entry removal: %s",
                    "; ".join(cleanup_failures),
                )
        except Exception:
            _LOGGER.exception(
                "Unexpected error removing owned Rachio valve webhooks during config "
                "entry removal"
            )
        try:
            cleanup_failures = await hass.async_add_executor_job(
                person.remove_legacy_webhooks
            )
            if cleanup_failures:
                _LOGGER.warning(
                    "Could not remove some owned legacy Rachio webhooks during config "
                    "entry removal: %s",
                    "; ".join(cleanup_failures),
                )
        except Exception:
            _LOGGER.exception(
                "Unexpected error removing owned legacy Rachio webhooks during config "
                "entry removal"
            )
    stores: dict[str, Store[dict]] = hass.data.get(EVENT_ORDER_STORES, {})
    if store := stores.pop(entry.entry_id, None):
        await store.async_remove()
    if CONF_CLOUDHOOK_URL in entry.data:
        await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])


async def async_setup_entry(hass: HomeAssistant, entry: RachioConfigEntry) -> bool:
    """Set up the Rachio config entry."""

    config = entry.data
    options = entry.options

    # CONF_MANUAL_RUN_MINS can only come from a yaml import
    if not options.get(CONF_MANUAL_RUN_MINS) and config.get(CONF_MANUAL_RUN_MINS):
        options_copy = options.copy()
        options_copy[CONF_MANUAL_RUN_MINS] = config[CONF_MANUAL_RUN_MINS]
        hass.config_entries.async_update_entry(entry, options=options_copy)

    # Configure API
    api_key = config[CONF_API_KEY]
    rachio = Rachio(api_key)

    # Get the URL of this server
    rachio.webhook_auth = secrets.token_hex()
    try:
        webhook_url = await async_get_or_create_registered_webhook_id_and_url(
            hass, entry
        )
    except cloud.CloudNotConnected as exc:
        # User has an active cloud subscription, but the connection to the cloud is down
        raise ConfigEntryNotReady from exc
    rachio.webhook_url = webhook_url

    person = RachioPerson(rachio, entry)

    # Get the API user
    try:
        await person.async_setup(hass)
    except (ConnectionError, Timeout) as error:
        raise ConfigEntryNotReady("Could not connect to the Rachio API") from error

    # Check for Rachio controller devices
    if not person.controllers and not person.base_stations:
        _LOGGER.error("No Rachio devices found in account %s", person.username)
        return False
    _LOGGER.debug(
        (
            "%d Rachio device(s) found; The url %s must be accessible from the internet"
            " in order to receive updates"
        ),
        len(person.controllers) + len(person.base_stations),
        webhook_url,
    )

    for base in person.base_stations:
        await base.status_coordinator.async_config_entry_first_refresh()
        await base.schedule_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = person
    await person.async_load_v2_event_order(hass)

    # Finish entity setup before a remote callback can reach local state.
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise

    local_webhook_registered = False
    try:
        async_register_webhook(hass, entry)
        local_webhook_registered = True
    except ValueError:
        if local_webhook_registered:
            async_unregister_webhook(hass, entry)
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise

    try:
        reconciliation_failures = await hass.async_add_executor_job(
            person.setup_v2_webhooks,
            webhook_url,
            entry.data[CONF_WEBHOOK_ID],
        )
        if reconciliation_failures:
            _LOGGER.warning(
                "Could not reconcile some Rachio valve webhooks; polling remains "
                "available: %s",
                "; ".join(reconciliation_failures),
            )
    except Exception:
        _LOGGER.exception(
            "Unexpected error reconciling Rachio valve webhooks; polling remains "
            "available"
        )

    return True
