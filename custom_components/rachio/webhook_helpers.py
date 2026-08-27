"""Pure helpers for Rachio v2 Smart Hose Timer webhooks."""

import hashlib
import hmac
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

VALVE_RUN_START_EVENT = "VALVE_RUN_START_EVENT"
VALVE_RUN_END_EVENT = "VALVE_RUN_END_EVENT"
VALVE_EVENT_TYPES = (VALVE_RUN_START_EVENT, VALVE_RUN_END_EVENT)
CURRENT_WEBHOOK_FIELDS = (
    "eventId",
    "eventType",
    "payload",
    "resourceId",
    "resourceType",
)


class ValveEventMetadata(TypedDict, total=False):
    """State derived only from authenticated v2 valve events."""

    last_seen: str
    last_run_reason: str
    last_watering_end: str


class ValveEventCursor(TypedDict):
    """Last accepted current-generation event for one valve."""

    event_id: str
    timestamp: str


class ValveEventOrderData(TypedDict):
    """Serializable replay-protection state."""

    metadata: dict[str, ValveEventMetadata]
    recent_event_ids: list[str]
    valves: dict[str, ValveEventCursor]


@dataclass(frozen=True)
class WebhookReconciliationPlan:
    """Actions needed to leave exactly one current owned webhook."""

    keep_id: str | None
    delete_ids: tuple[str, ...]
    create: bool


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize a webhook payload the way Rachio signs it."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def webhook_signature(api_token: str, payload: Mapping[str, Any]) -> str:
    """Return the Rachio HMAC-SHA256 signature for a payload."""
    return hmac.new(
        api_token.encode(), canonical_json_bytes(payload), hashlib.sha256
    ).hexdigest()


def verify_webhook_signature(
    api_token: str, payload: Mapping[str, Any], signature: str | None
) -> bool:
    """Verify a Rachio webhook signature without leaking timing information."""
    if not signature:
        return False
    return hmac.compare_digest(webhook_signature(api_token, payload), signature)


def is_current_webhook_event(payload: Mapping[str, Any]) -> bool:
    """Distinguish current signed events from legacy controller callbacks."""
    return all(key in payload for key in CURRENT_WEBHOOK_FIELDS)


class EventDeduplicator:
    """Keep a bounded in-memory set of recently delivered event IDs."""

    def __init__(
        self, max_events: int = 512, initial_event_ids: Sequence[str] = ()
    ) -> None:
        """Initialize the event cache."""
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._max_events = max_events
        self._events: OrderedDict[str, None] = OrderedDict()
        for event_id in initial_event_ids[-max_events:]:
            if isinstance(event_id, str) and event_id:
                self._events[event_id] = None

    def contains(self, event_id: str) -> bool:
        """Return whether an event ID is in the bounded cache."""
        return event_id in self._events

    def record(self, event_id: str) -> None:
        """Record an event ID, evicting the oldest ID when necessary."""
        self._events[event_id] = None
        self._events.move_to_end(event_id)
        if len(self._events) > self._max_events:
            self._events.popitem(last=False)

    def accept(self, event_id: str) -> bool:
        """Return whether an event ID has not already been seen."""
        if event_id in self._events:
            self._events.move_to_end(event_id)
            return False
        self.record(event_id)
        return True

    def event_ids(self) -> list[str]:
        """Return event IDs from oldest to newest for persistence."""
        return list(self._events)


class ValveEventOrder:
    """Reject replayed or out-of-order valve events across restarts."""

    def __init__(
        self,
        *,
        latest_by_valve: Mapping[str, ValveEventCursor] | None = None,
        metadata_by_valve: Mapping[str, ValveEventMetadata] | None = None,
        recent_event_ids: Sequence[str] = (),
        max_events: int = 512,
    ) -> None:
        """Initialize validated event cursors and a bounded event-ID cache."""
        self._latest_by_valve: dict[str, ValveEventCursor] = {}
        if latest_by_valve:
            for valve_id, cursor in latest_by_valve.items():
                if not isinstance(valve_id, str) or not valve_id:
                    continue
                if not isinstance(cursor, Mapping):
                    continue
                event_id = cursor.get("event_id")
                timestamp = cursor.get("timestamp")
                if (
                    isinstance(event_id, str)
                    and event_id
                    and isinstance(timestamp, str)
                    and _parse_event_timestamp(timestamp) is not None
                ):
                    self._latest_by_valve[valve_id] = {
                        "event_id": event_id,
                        "timestamp": timestamp,
                    }
        self._metadata_by_valve: dict[str, ValveEventMetadata] = {}
        if metadata_by_valve:
            for valve_id, metadata in metadata_by_valve.items():
                if (
                    not isinstance(valve_id, str)
                    or not valve_id
                    or not isinstance(metadata, Mapping)
                ):
                    continue
                clean_metadata: ValveEventMetadata = {}
                for key in ("last_seen", "last_run_reason", "last_watering_end"):
                    if isinstance(value := metadata.get(key), str) and value:
                        clean_metadata[key] = value  # type: ignore[literal-required]
                if clean_metadata:
                    self._metadata_by_valve[valve_id] = clean_metadata
        self._recent_event_ids = EventDeduplicator(max_events, recent_event_ids)

    @classmethod
    def from_store_data(
        cls, data: Mapping[str, Any] | None, *, max_events: int = 512
    ) -> ValveEventOrder:
        """Restore only well-formed replay-protection state."""
        if not isinstance(data, Mapping):
            return cls(max_events=max_events)
        latest = data.get("valves")
        metadata = data.get("metadata")
        recent = data.get("recent_event_ids")
        return cls(
            latest_by_valve=latest if isinstance(latest, Mapping) else None,
            metadata_by_valve=(metadata if isinstance(metadata, Mapping) else None),
            recent_event_ids=(
                recent
                if isinstance(recent, Sequence) and not isinstance(recent, str | bytes)
                else ()
            ),
            max_events=max_events,
        )

    def would_accept(self, event: Mapping[str, Any]) -> bool:
        """Return whether an event is valid and newer for its valve."""
        identity = _event_identity(event)
        if identity is None:
            return False
        event_id, valve_id, timestamp = identity
        if self._recent_event_ids.contains(event_id):
            return False
        if cursor := self._latest_by_valve.get(valve_id):
            previous = _parse_event_timestamp(cursor["timestamp"])
            if previous is None or timestamp <= previous:
                return False
        return True

    def record(
        self,
        event: Mapping[str, Any],
        metadata: Mapping[str, str] | None = None,
    ) -> bool:
        """Persist the ordering cursor for an event that is still acceptable."""
        if not self.would_accept(event):
            return False
        identity = _event_identity(event)
        if identity is None:
            return False
        event_id, valve_id, timestamp = identity
        self._latest_by_valve[valve_id] = {
            "event_id": event_id,
            "timestamp": timestamp.astimezone(UTC).isoformat(),
        }
        if metadata:
            current_metadata = self._metadata_by_valve.setdefault(valve_id, {})
            for key in ("last_seen", "last_run_reason", "last_watering_end"):
                if isinstance(value := metadata.get(key), str) and value:
                    current_metadata[key] = value  # type: ignore[literal-required]
        self._recent_event_ids.record(event_id)
        return True

    def metadata_by_valve(self) -> dict[str, ValveEventMetadata]:
        """Return a copy of the event metadata restored for entity state."""
        return deepcopy(self._metadata_by_valve)

    def as_store_data(self) -> ValveEventOrderData:
        """Return JSON-serializable replay-protection state without secrets."""
        return {
            "metadata": self.metadata_by_valve(),
            "recent_event_ids": self._recent_event_ids.event_ids(),
            "valves": deepcopy(self._latest_by_valve),
        }


def _parse_event_timestamp(value: str) -> datetime | None:
    """Parse a timezone-aware ISO 8601 webhook timestamp."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _event_identity(
    event: Mapping[str, Any],
) -> tuple[str, str, datetime] | None:
    """Return the identity used for per-valve event ordering."""
    event_id = event.get("eventId")
    valve_id = event.get("resourceId")
    timestamp = event.get("timestamp")
    if (
        event.get("eventType") not in VALVE_EVENT_TYPES
        or event.get("resourceType") != "VALVE"
        or not isinstance(event_id, str)
        or not event_id
        or not isinstance(valve_id, str)
        or not valve_id
        or not isinstance(timestamp, str)
        or (parsed := _parse_event_timestamp(timestamp)) is None
    ):
        return None
    return event_id, valve_id, parsed


def stable_external_id(webhook_id: str) -> str:
    """Return an external ID in Home Assistant Core's Rachio namespace."""
    return f"homeassistant.rachio:{webhook_id}"


def webhook_registration_matches(
    webhook: Mapping[str, Any],
    *,
    external_id: str,
    webhook_url: str,
    event_types: Sequence[str] = VALVE_EVENT_TYPES,
) -> bool:
    """Return whether an existing registration has the desired shape."""
    existing_event_types = webhook.get("eventTypes", webhook.get("event_types", ()))
    return (
        webhook.get("externalId", webhook.get("external_id")) == external_id
        and webhook.get("url") == webhook_url
        and set(existing_event_types) == set(event_types)
    )


def find_owned_webhooks(
    webhooks: Sequence[Mapping[str, Any]], external_id: str
) -> list[Mapping[str, Any]]:
    """Return registrations owned by this Home Assistant config entry."""
    return [
        item
        for item in webhooks
        if item.get("externalId", item.get("external_id")) == external_id
    ]


def plan_webhook_reconciliation(
    webhooks: Sequence[Mapping[str, Any]],
    *,
    external_id: str,
    webhook_url: str,
    event_types: Sequence[str] = VALVE_EVENT_TYPES,
) -> WebhookReconciliationPlan:
    """Plan an exact, ownership-scoped webhook reconciliation."""
    owned = find_owned_webhooks(webhooks, external_id)
    records_by_id: dict[str, Mapping[str, Any]] = {}
    for item in owned:
        webhook_id = item.get("id")
        if not isinstance(webhook_id, str) or not webhook_id:
            raise ValueError("owned webhook record has no valid id")
        records_by_id[webhook_id] = item

    matching_ids = sorted(
        webhook_id
        for webhook_id, item in records_by_id.items()
        if webhook_registration_matches(
            item,
            external_id=external_id,
            webhook_url=webhook_url,
            event_types=event_types,
        )
    )
    keep_id = matching_ids[0] if matching_ids else None
    delete_ids = tuple(
        webhook_id for webhook_id in sorted(records_by_id) if webhook_id != keep_id
    )
    return WebhookReconciliationPlan(
        keep_id=keep_id,
        delete_ids=delete_ids,
        create=keep_id is None,
    )


def plan_owned_webhook_cleanup(
    webhooks: Sequence[Mapping[str, Any]], external_id: str
) -> tuple[str, ...]:
    """Return only IDs of registrations owned by this config entry."""
    owned_ids: set[str] = set()
    for item in find_owned_webhooks(webhooks, external_id):
        webhook_id = item.get("id")
        if not isinstance(webhook_id, str) or not webhook_id:
            raise ValueError("owned webhook record has no valid id")
        owned_ids.add(webhook_id)
    return tuple(sorted(owned_ids))


def apply_valve_event(
    coordinator_data: Mapping[str, Mapping[str, Any]], event: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], str, ValveEventMetadata] | None:
    """Apply a supported v2 event to a copy of coordinator state."""
    event_type = event.get("eventType")
    valve_id = event.get("resourceId")
    if (
        event_type not in VALVE_EVENT_TYPES
        or event.get("resourceType") != "VALVE"
        or not isinstance(valve_id, str)
        or valve_id not in coordinator_data
    ):
        return None

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None

    new_data = dict(coordinator_data)
    valve = deepcopy(dict(coordinator_data[valve_id]))
    state = dict(valve.get("state", {}))
    reported_state = dict(state.get("reportedState", {}))

    metadata: ValveEventMetadata = {}
    if isinstance(timestamp := event.get("timestamp"), str):
        metadata["last_seen"] = timestamp
    if isinstance(run_reason := payload.get("runType"), str):
        metadata["last_run_reason"] = run_reason

    if event_type == VALVE_RUN_START_EVENT:
        reported_state["lastWateringAction"] = dict(payload)
    else:
        reported_state.pop("lastWateringAction", None)
        if isinstance(end_time := payload.get("endTime"), str):
            metadata["last_watering_end"] = end_time
        elif "last_seen" in metadata:
            metadata["last_watering_end"] = metadata["last_seen"]

    state["reportedState"] = reported_state
    valve["state"] = state
    new_data[valve_id] = valve
    return new_data, valve_id, metadata
