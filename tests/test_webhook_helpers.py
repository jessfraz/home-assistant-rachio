"""Deterministic tests for the Home Assistant-independent webhook logic."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

HELPER_PATH = (
    Path(__file__).parents[1] / "custom_components" / "rachio" / "webhook_helpers.py"
)
SPEC = spec_from_file_location("rachio_webhook_helpers", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helpers = module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)


def _valve_event(
    event_id: str,
    timestamp: str,
    *,
    valve_id: str = "valve-one",
    event_type: str = "VALVE_RUN_START_EVENT",
) -> dict:
    """Build a complete current-generation valve event."""
    return {
        "eventId": event_id,
        "eventType": event_type,
        "externalId": "homeassistant.rachio:webhook-id",
        "payload": {"runType": "QUICK_RUN"},
        "resourceId": valve_id,
        "resourceType": "VALVE",
        "timestamp": timestamp,
    }


def test_signature_matches_rachio_documentation_example() -> None:
    """Match Rachio's published v2 webhook signature exactly."""
    token = "0b6de780-6f47-410b-8c2a-c6e9c506f3f4"
    payload = {
        "eventId": "e040e09b-3d09-3dca-a688-3b24401d53b0",
        "eventType": "VALVE_RUN_START_EVENT",
        "externalId": "123-123-123",
        "payload": {
            "durationSeconds": "181",
            "flowDetected": False,
            "programId": "48a7181c-e06e-4a4f-bd65-92567fce0b81",
            "runType": "PROGRAM",
            "startTime": "2024-12-11T20:20:48Z",
        },
        "resourceId": "205aa289-8fe0-4f8e-8fc2-032f5f1e1c24",
        "resourceType": "VALVE",
        "timestamp": "2024-12-11T20:20:52Z",
    }
    signature = "b8b1a1ee62542e3910c41f405371d5e419f30055f6ce07d776e8cbee84d7b14d"

    assert helpers.webhook_signature(token, payload) == signature
    assert helpers.verify_webhook_signature(token, payload, signature)
    assert not helpers.verify_webhook_signature(token, payload, "0" * 64)
    assert not helpers.verify_webhook_signature(token, payload, None)


def test_event_deduplicator_is_bounded() -> None:
    """Reject repeats while allowing an evicted old ID again."""
    seen = helpers.EventDeduplicator(max_events=2)

    assert seen.accept("one")
    assert not seen.accept("one")
    assert seen.accept("two")
    assert seen.accept("three")
    assert seen.accept("one")


def test_registration_reconciliation() -> None:
    """Find only this config entry's complete valve registration."""
    external_id = helpers.stable_external_id("webhook-id")
    assert external_id == "homeassistant.rachio:webhook-id"
    desired = {
        "externalId": external_id,
        "url": "https://example.test/api/webhook/webhook-id",
        "eventTypes": ["VALVE_RUN_END_EVENT", "VALVE_RUN_START_EVENT"],
    }
    unrelated = {"externalId": "somebody-else"}

    assert helpers.find_owned_webhooks([unrelated, desired], external_id) == [desired]
    assert helpers.webhook_registration_matches(
        desired,
        external_id=external_id,
        webhook_url="https://example.test/api/webhook/webhook-id",
    )
    assert not helpers.webhook_registration_matches(
        {**desired, "url": "https://stale.example.test/webhook"},
        external_id=external_id,
        webhook_url="https://example.test/api/webhook/webhook-id",
    )


def test_reconciliation_plan_keeps_one_current_and_deletes_owned_duplicates() -> None:
    """Keep one deterministic current record and touch no unrelated record."""
    external_id = helpers.stable_external_id("webhook-id")
    url = "https://example.test/api/webhook/webhook-id"
    current = {
        "id": "current-b",
        "externalId": external_id,
        "url": url,
        "eventTypes": list(helpers.VALVE_EVENT_TYPES),
    }
    current_first = {**current, "id": "current-a"}
    stale = {**current, "id": "stale", "url": "https://stale.example.test"}
    unrelated = {**current, "id": "unrelated", "externalId": "someone-else"}

    plan = helpers.plan_webhook_reconciliation(
        [current, stale, unrelated, current_first],
        external_id=external_id,
        webhook_url=url,
    )

    assert plan == helpers.WebhookReconciliationPlan(
        keep_id="current-a",
        delete_ids=("current-b", "stale"),
        create=False,
    )


def test_reconciliation_plan_deletes_stale_owned_before_create() -> None:
    """Replace stale owned registrations while preserving other owners."""
    external_id = helpers.stable_external_id("webhook-id")
    plan = helpers.plan_webhook_reconciliation(
        [
            {"id": "stale-b", "externalId": external_id, "url": "old"},
            {"id": "other", "externalId": "someone-else", "url": "old"},
            {"id": "stale-a", "externalId": external_id, "url": "older"},
        ],
        external_id=external_id,
        webhook_url="https://current.example.test",
    )

    assert plan == helpers.WebhookReconciliationPlan(
        keep_id=None,
        delete_ids=("stale-a", "stale-b"),
        create=True,
    )
    assert helpers.plan_owned_webhook_cleanup(
        [
            {"id": "owned", "externalId": external_id},
            {"id": "other", "externalId": "someone-else"},
        ],
        external_id,
    ) == ("owned",)


def test_reconciliation_rejects_owned_record_without_deletable_id() -> None:
    """Fail closed when an owned stale record cannot be addressed safely."""
    external_id = helpers.stable_external_id("webhook-id")
    with pytest.raises(ValueError, match="no valid id"):
        helpers.plan_webhook_reconciliation(
            [{"externalId": external_id, "url": "stale"}],
            external_id=external_id,
            webhook_url="https://current.example.test",
        )


def test_event_order_rejects_invalid_duplicate_and_non_newer_events() -> None:
    """Accept only strictly newer valid timestamps for each valve."""
    order = helpers.ValveEventOrder()
    first = _valve_event("first", "2024-12-11T20:20:52Z")
    assert order.record(first, {"last_run_reason": "QUICK_RUN"})
    assert not order.would_accept(first)
    assert not order.would_accept(_valve_event("older", "2024-12-11T20:20:51Z"))
    assert not order.would_accept(
        _valve_event("equal", "2024-12-11T20:20:52.000000+00:00")
    )
    assert not order.would_accept(_valve_event("naive", "2024-12-11T20:20:53"))
    assert not order.would_accept(_valve_event("invalid", "yesterday"))
    assert order.would_accept(_valve_event("newer", "2024-12-11T20:20:53+00:00"))
    assert order.would_accept(
        _valve_event(
            "other-valve-older",
            "2024-12-10T20:20:52Z",
            valve_id="valve-two",
        )
    )


def test_event_order_persists_metadata_and_bounded_ids_across_restart() -> None:
    """Restore ordering and sensor metadata without storing credentials."""
    order = helpers.ValveEventOrder(max_events=2)
    assert order.record(
        _valve_event("one", "2024-12-11T20:20:51Z"),
        {"last_run_reason": "QUICK_RUN", "last_seen": "first"},
    )
    assert order.record(
        _valve_event("two", "2024-12-11T20:20:52Z"),
        {"last_watering_end": "2024-12-11T20:20:52Z"},
    )
    assert order.record(_valve_event("three", "2024-12-11T20:20:53Z"))

    stored = order.as_store_data()
    restored = helpers.ValveEventOrder.from_store_data(stored, max_events=2)

    assert stored["recent_event_ids"] == ["two", "three"]
    assert "token" not in repr(stored).lower()
    assert restored.metadata_by_valve() == {
        "valve-one": {
            "last_run_reason": "QUICK_RUN",
            "last_seen": "first",
            "last_watering_end": "2024-12-11T20:20:52Z",
        }
    }
    assert not restored.would_accept(
        _valve_event("after-restart-equal", "2024-12-11T20:20:53Z")
    )
    assert restored.would_accept(_valve_event("after-restart", "2024-12-11T20:20:54Z"))


def test_start_event_updates_a_copy_immediately() -> None:
    """A start event opens only its valve and records truthful metadata."""
    original = {
        "valve-one": {
            "id": "valve-one",
            "state": {"reportedState": {"connected": True}},
        }
    }
    event = {
        "eventId": "start-one",
        "eventType": "VALVE_RUN_START_EVENT",
        "externalId": "homeassistant.rachio:webhook-id",
        "payload": {
            "durationSeconds": "360",
            "flowDetected": False,
            "runType": "QUICK_RUN",
            "startTime": "2024-12-11T22:21:35Z",
        },
        "resourceId": "valve-one",
        "resourceType": "VALVE",
        "timestamp": "2024-12-11T22:21:39Z",
    }

    result = helpers.apply_valve_event(original, event)

    assert result is not None
    updated, valve_id, metadata = result
    assert valve_id == "valve-one"
    assert "lastWateringAction" not in original["valve-one"]["state"]["reportedState"]
    assert (
        updated["valve-one"]["state"]["reportedState"]["lastWateringAction"]
        == event["payload"]
    )
    assert metadata == {
        "last_seen": "2024-12-11T22:21:39Z",
        "last_run_reason": "QUICK_RUN",
    }


def test_end_event_closes_valve_and_records_end_time() -> None:
    """An end event removes active watering and records its delivery time."""
    original = {
        "valve-one": {
            "id": "valve-one",
            "state": {
                "reportedState": {
                    "connected": True,
                    "lastWateringAction": {"runType": "QUICK_RUN"},
                }
            },
        }
    }
    event = {
        "eventId": "end-one",
        "eventType": "VALVE_RUN_END_EVENT",
        "payload": {
            "durationSeconds": "51",
            "endReason": "STOPPED",
            "flowDetected": False,
            "runType": "QUICK_RUN",
            "startTime": "2024-12-11T22:21:35Z",
        },
        "resourceId": "valve-one",
        "resourceType": "VALVE",
        "timestamp": "2024-12-11T22:22:30Z",
    }

    result = helpers.apply_valve_event(original, event)

    assert result is not None
    updated, _, metadata = result
    assert "lastWateringAction" not in updated["valve-one"]["state"]["reportedState"]
    assert metadata == {
        "last_seen": "2024-12-11T22:22:30Z",
        "last_run_reason": "QUICK_RUN",
        "last_watering_end": "2024-12-11T22:22:30Z",
    }


def test_irrelevant_event_is_ignored() -> None:
    """Do not invent state from unsupported event or resource types."""
    original = {"valve-one": {"id": "valve-one"}}

    assert (
        helpers.apply_valve_event(
            original,
            {
                "eventType": "PROGRAM_RAIN_SKIP_CREATED_EVENT",
                "payload": {},
                "resourceId": "valve-one",
                "resourceType": "PROGRAM",
            },
        )
        is None
    )


def test_legacy_webhook_shape_is_not_a_current_valve_event() -> None:
    """Legacy controller events may have eventType but not current v2 fields."""
    legacy_event = {
        "eventType": "RAIN_DELAY_EVENT",
        "type": "RAIN_DELAY",
        "subType": "RAIN_DELAY_ON",
        "externalId": "homeassistant.rachio:legacy-auth",
    }
    assert not helpers.is_current_webhook_event(legacy_event)
    assert helpers.is_current_webhook_event(
        {
            "eventId": "event-id",
            "eventType": "VALVE_RUN_START_EVENT",
            "payload": {},
            "resourceId": "valve-id",
            "resourceType": "VALVE",
        }
    )
