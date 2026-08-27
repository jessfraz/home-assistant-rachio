"""Client-side registration for Rachio's current webhook service."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from rachiopy import Rachio

from .response_helpers import RachioResponseError, require_success
from .webhook_helpers import (
    VALVE_EVENT_TYPES,
    plan_owned_webhook_cleanup,
    plan_webhook_reconciliation,
    stable_external_id,
)

_LOGGER = logging.getLogger(__name__)


class RachioV2WebhookError(RuntimeError):
    """Raised when Rachio's current webhook API cannot be reconciled."""


class RachioV2WebhookManager:
    """Register Smart Hose Timer valve events without replacing legacy hooks."""

    def __init__(self, rachio: Rachio, webhook_url: str, webhook_id: str) -> None:
        """Initialize the webhook manager."""
        self._rachio = rachio
        self._webhook_url = webhook_url
        self.external_id = stable_external_id(webhook_id)

    def _list_webhooks(self, valve_id: str) -> list[Mapping[str, Any]]:
        query = urlencode({"resource_id.valve_id": valve_id})
        headers, payload = self._rachio.valve.valve_get_request(
            f"webhook/listWebhooks?{query}"
        )
        try:
            require_success((headers, payload), "list webhooks")
            return _extract_webhooks(payload)
        except (RachioResponseError, ValueError) as err:
            raise RachioV2WebhookError(str(err)) from err

    def _create_webhook(self, valve_id: str) -> None:
        response = self._rachio.valve.valve_post_request(
            "webhook/createWebhook",
            {
                "resourceId": {"valveId": valve_id},
                "url": self._webhook_url,
                "externalId": self.external_id,
                "eventTypes": list(VALVE_EVENT_TYPES),
            },
        )
        try:
            require_success(response, "create webhook")
        except RachioResponseError as err:
            raise RachioV2WebhookError(str(err)) from err

    def _delete_webhook(self, webhook_id: str) -> None:
        response = self._rachio.valve.valve_delete_request(
            f"webhook/deleteWebhook/{webhook_id}"
        )
        try:
            require_success(response, "delete webhook")
        except RachioResponseError as err:
            raise RachioV2WebhookError(str(err)) from err

    def ensure_valve_webhook(self, valve_id: str) -> bool:
        """Ensure one stable webhook exists, returning whether it was created."""
        webhooks = self._list_webhooks(valve_id)
        try:
            plan = plan_webhook_reconciliation(
                webhooks,
                external_id=self.external_id,
                webhook_url=self._webhook_url,
            )
        except ValueError as err:
            raise RachioV2WebhookError(
                f"Rachio valve {valve_id} returned an invalid owned webhook: {err}"
            ) from err

        for webhook_id in plan.delete_ids:
            self._delete_webhook(webhook_id)
        if not plan.create:
            if plan.delete_ids:
                _LOGGER.info(
                    "Removed %d stale or duplicate Rachio webhook(s) for valve %s",
                    len(plan.delete_ids),
                    valve_id,
                )
            return False

        self._create_webhook(valve_id)
        return True

    def remove_owned_valve_webhooks(self, valve_id: str) -> int:
        """Delete every webhook owned by this config entry for one valve."""
        try:
            owned_ids = plan_owned_webhook_cleanup(
                self._list_webhooks(valve_id), self.external_id
            )
        except ValueError as err:
            raise RachioV2WebhookError(
                f"Rachio valve {valve_id} returned an invalid owned webhook: {err}"
            ) from err
        for webhook_id in owned_ids:
            self._delete_webhook(webhook_id)
        return len(owned_ids)


def _extract_webhooks(payload: Any) -> list[Mapping[str, Any]]:
    """Normalize listWebhooks responses without depending on undocumented fields."""
    candidates: Any = payload
    if isinstance(payload, Mapping):
        candidates = payload.get("webhooks", payload.get("data", []))
    if not isinstance(candidates, Sequence) or isinstance(candidates, str | bytes):
        raise RachioV2WebhookError("Rachio listWebhooks returned an invalid payload")
    if not all(isinstance(item, Mapping) for item in candidates):
        raise RachioV2WebhookError("Rachio listWebhooks returned invalid records")
    return list(candidates)
