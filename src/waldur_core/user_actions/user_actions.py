"""Bridges the persistent ``UserAction`` queue into the dashboard feed.

The dashboard feed and the ``UserAction`` queue grew up separately: the queue
is materialised every six hours by ``update-user-actions`` and supports
silencing and corrective actions, while dashboard providers compute their items
live. Rendering both produced two "Pending actions" sections, so the dashboard
now renders one feed and this provider folds the queue into it.

Deliberately an adapter rather than a rewrite of either side. Moving the live
providers into the queue would make profile-completeness and ToS prompts up to
six hours stale, and neither has an object to hang the model's mandatory
``content_type``/``object_id`` on.
"""

from typing import Any

from constance import config
from django.contrib.auth import get_user_model
from django.db.models import Case, F, IntegerField, Value, When
from django.utils import timezone

from waldur_core.core.fields import StringUUID

from . import models
from .providers import (
    DASHBOARD_LIST_LIMIT,
    DASHBOARD_VARIANT_ORDER,
    BaseDashboardProvider,
    register_dashboard_provider,
)

User = get_user_model()

# UserAction.urgency has choices, but the queue's own
# ``ordering = ["-urgency", ...]`` sorts the stored values as text: "medium"
# before "low" before "high". Mapping to the feed's variants gives the rows a
# meaning-preserving order instead of inheriting that one.
URGENCY_TO_VARIANT = {
    models.UserAction.UrgencyChoices.HIGH: "error",
    models.UserAction.UrgencyChoices.MEDIUM: "warning",
    models.UserAction.UrgencyChoices.LOW: "info",
}
DEFAULT_VARIANT = "info"
# Derived rather than spelled out a second time: the provider's own cut and the
# feed's sort have to agree on what outranks what, including for a value
# outside the choices, which nothing at the database level prevents.
URGENCY_RANK = {
    urgency: DASHBOARD_VARIANT_ORDER[variant]
    for urgency, variant in URGENCY_TO_VARIANT.items()
}
DEFAULT_URGENCY_RANK = DASHBOARD_VARIANT_ORDER[DEFAULT_VARIANT]


def is_legacy_queue_enabled() -> bool:
    """Whether the UserAction queue is in use on this deployment.

    USER_ACTIONS_ENABLED is what tasks.update_user_actions checks before
    populating anything, so it is the switch that decides whether the queue
    exists. Rows outlive it being turned off, hence the explicit check rather
    than relying on the queue being empty.

    Deliberately not the user.pending_user_actions feature flag: that one is
    read only by the frontend, and it now gates the standalone user-actions
    page rather than this feed.
    """
    return config.USER_ACTIONS_ENABLED


def _hex_uuid(value):
    """Render a UUID the way the rest of the feed does.

    UserAction keeps these on plain UUIDFields, so they arrive as uuid.UUID and
    DRF renders them hyphenated. Every other provider passes a UuidMixin value,
    whose StringUUID renders bare hex — and hex is the form Waldur's routes and
    lookups use, so a hyphenated target_uuid produces deep links that miss.
    """
    return StringUUID(value.hex) if value else None


class LegacyUserActionDashboardProvider(BaseDashboardProvider):
    """Surfaces non-silenced ``UserAction`` rows as dashboard feed items."""

    action_type = "legacy_user_action"
    display_name = "Pending User Actions"

    def get_dashboard_pending_actions(self, user: User) -> list[dict[str, Any]]:
        if not is_legacy_queue_enabled():
            return []

        now = timezone.now()
        # Mirrors UserActionViewSet.get_queryset: a row the user has silenced,
        # permanently or until a future date, is not pending for them. Reusing
        # that filter is what gives the feed silencing for free.
        queryset = (
            models.UserAction.objects.filter(user=user, is_silenced=False)
            .exclude(silenced_until__gt=now)
            .select_related("content_type")
            # The corrective actions below dereference the GenericForeignKey,
            # which select_related cannot span; without this the endpoint runs
            # a query per row on every dashboard load.
            .prefetch_related("related_object")
            .annotate(
                urgency_rank=Case(
                    *[
                        When(urgency=urgency, then=Value(rank))
                        for urgency, rank in URGENCY_RANK.items()
                    ],
                    default=Value(DEFAULT_URGENCY_RANK),
                    output_field=IntegerField(),
                )
            )
            # nulls_last spelled out rather than left to the backend's default:
            # the slice below decides which rows survive, so undated rows
            # crowding out dated ones would be invisible until the cap bites.
            .order_by("urgency_rank", F("due_date").asc(nulls_last=True), "-created")
        )

        # The view caps each provider at DASHBOARD_LIST_LIMIT anyway; slicing
        # here means a user with a long backlog does not build corrective
        # actions for rows that are about to be discarded.
        return [
            self._to_feed_item(action, user)
            for action in queryset[:DASHBOARD_LIST_LIMIT]
        ]

    def _to_feed_item(self, action: models.UserAction, user: User) -> dict[str, Any]:
        return {
            "type": action.action_type,
            "title": action.title,
            "description": action.description,
            "variant": URGENCY_TO_VARIANT.get(action.urgency, DEFAULT_VARIANT),
            "deadline": action.due_date,
            "count": None,
            "target_uuid": _hex_uuid(action.resource_uuid or action.offering_uuid),
            "customer_uuid": _hex_uuid(action.organization_uuid),
            # Addressing the existing silence/unsilence/execute_action
            # endpoints needs the row's uuid, so exposing it here is what lets
            # the card keep those controls without a new endpoint.
            "uuid": action.uuid,
            "urgency": action.urgency,
            "route_name": action.route_name or None,
            "route_params": action.route_params or {},
            "can_silence": True,
            "actions": self._corrective_actions(action, user),
        }

    def _corrective_actions(self, action: models.UserAction, user: User):
        """Corrective actions for a row, as CorrectiveAction dataclasses.

        Returned unserialised: CorrectiveActionSerializer already describes
        this shape, and hand-mapping it to dicts here is what let the feed's
        copy of the contract drift from the queue's.
        """
        if action.related_object is None:
            # The target was deleted and cleanup has not caught up — the
            # signal handler defers to a celery task, and there is a periodic
            # sweep behind that. Providers dereference the object unguarded,
            # and the dispatcher drops a whole provider when one raises, so a
            # single stale row would otherwise hide every queued action.
            return []
        return action.get_corrective_actions_for_user(user)


register_dashboard_provider(LegacyUserActionDashboardProvider)
