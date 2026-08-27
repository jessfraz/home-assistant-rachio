# Rachio Enhanced for Home Assistant

> [!WARNING]
> This is an experimental custom integration that overrides Home Assistant's
> built-in `rachio` integration. It controls real irrigation hardware through a
> cloud API. Review releases before upgrading and keep a current Home Assistant
> backup. It is not yet suitable for unattended safety-critical watering.

This fork keeps the built-in integration's domain, config entry, API token,
webhook ID, cloudhook URL, device identifiers, entity unique IDs, and legacy
Smart Hose Timer switch. It adds:

- a native `ValveEntity` with the water device class for every Smart Hose Timer;
- a number entity for the documented physical-button default runtime;
- authenticated current-generation valve webhooks for immediate state changes;
- last watering end and last run reason sensors;
- a disabled-by-default diagnostic last-seen sensor.

It deliberately does **not** invent battery percentages, flow rate, water
volume, signal strength, leak detection, or clog detection. Rachio's supported
Smart Hose Timer responses and current valve events do not provide those values.

## Provenance

The component started from Home Assistant Core
[`2026.8.3`](https://github.com/home-assistant/core/tree/759e4658f40b3ccb671d418b8a0ed95224bf4561/homeassistant/components/rachio),
commit `759e4658f40b3ccb671d418b8a0ed95224bf4561`. Changes are maintained here so
they can be tested before proposing focused upstream patches.

The current webhook implementation follows Rachio's documented
[`createWebhook` and `listWebhooks` service](https://rachio.readme.io/reference/webhooks).
Incoming valve events are verified with Rachio's documented HMAC-SHA256
signature over canonical, alphabetically sorted JSON. Per-valve timestamps,
recent event IDs, and event-derived sensor metadata are persisted so duplicate,
equal-timestamp, and out-of-order events remain rejected across restarts. A
verified event updates the coordinator immediately and also schedules the normal
debounced API refresh.

## Install

Requires Home Assistant `2026.8.3` or newer.

### HACS custom repository

Once this repository is published:

1. In HACS, add `https://github.com/jessfraz/home-assistant-rachio` as a custom
   repository with category **Integration**.
2. Install **Rachio Enhanced**.
3. Restart Home Assistant.

Do not delete or re-create the existing Rachio config entry. Because this uses
the same `rachio` domain, Home Assistant loads the custom component in place of
Core and reuses the existing credentials and registries.

### Manual test install

Copy `custom_components/rachio` into the Home Assistant configuration directory
as `custom_components/rachio`, then restart Home Assistant. Do not copy the
repository's test or CI files into the configuration directory.

## Legacy switch compatibility

Existing `switch.*` Smart Hose Timer entities keep their entity IDs and current
enabled/disabled registry state, so an existing `switch.dog_water` automation
continues to work. On a new install, the compatibility switch is disabled by
default and the native `valve.*` entity is the default control. Enable the
legacy switch manually only when an existing automation still needs it.

## Roll back

1. While this custom integration is still installed, disable the existing
   Rachio config entry. This flushes local event-order state and makes a
   best-effort attempt to remove only the current entry's owned valve webhooks.
2. Remove **Rachio Enhanced** in HACS, or remove only
   `config/custom_components/rachio` for a manual install.
3. Restart Home Assistant and re-enable the same Rachio config entry.
4. Confirm the built-in Rachio integration loads and the existing legacy switch
   and controller entities return.

Do not remove the Rachio config entry during rollback. Core will reuse its API
token, webhook ID, cloudhook, device IDs, and legacy unique IDs. The new valve,
number, and event sensors will become unavailable because Core does not define
them; they may be removed from the entity registry after rollback is verified.
Current-generation valve webhook registrations use Core's
`homeassistant.rachio:` namespace and the existing HA webhook URL. Cleanup is
deliberately best effort so a Rachio outage cannot prevent local unload or entry
removal. If the API was unavailable during step 1, an owned registration may
remain temporarily. The built-in integration may reject those signed callbacks,
but its normal polling is unaffected. Reinstalling the fork reconciles only that
entry's exact external ID; unrelated registrations are never removed.

## Development

The security- and reconciliation-sensitive logic is in a pure-standard-library
module so it can be tested without Home Assistant mocks:

```console
uv run --python 3.14.2 --with pytest-homeassistant-custom-component \
  --with RachioPy==1.1.0 pytest
ruff check .
ruff format --check .
```

The suite includes deterministic standard-library tests and contract tests
against Home Assistant `2026.8.3`. It fakes only the RachioPy network boundary;
no test contacts Rachio or mutates live Home Assistant state.
