"""Hooks registered by marketplace into lower-layer ``waldur_core`` registries.

Keeping these registrations in ``marketplace`` lets ``waldur_core.logging`` and
``waldur_core.permissions`` remain independent of ``waldur_mastermind.marketplace``.
"""

import logging

from django.db.models import Exists, OuterRef, Q
from rest_framework import serializers

from waldur_core.logging.serializers import register_offering_access_validator
from waldur_core.permissions.pat_filtering import _ids_q, _or, register_pat_filter
from waldur_core.permissions.utils import (
    ActiveAccountGuard,
    register_active_account_guard,
)

from . import enums, models

logger = logging.getLogger(__name__)


def validate_offering_access_for_event_subscription(user, offering_uuid) -> None:
    """Verify ``user`` can create an EventSubscriptionQueue for ``offering_uuid``."""
    offering_exists = models.Offering.objects.filter(uuid=offering_uuid).exists()
    if not offering_exists:
        raise serializers.ValidationError(
            f"Offering with UUID {offering_uuid} does not exist"
        )

    # Check user has access to the offering via standard permissions
    user_offerings = models.Offering.objects.all().filter_for_user(user)
    if user_offerings.filter(uuid=offering_uuid).exists():
        return

    # ISD identity managers can access non-archived/draft offerings
    # for STOMP event subscription queue creation
    if user.is_identity_manager and user.managed_isds:
        if models.Offering.objects.filter(
            uuid=offering_uuid,
            state__in=enums.OfferingStates.ISD_ALLOWED_STATES,
        ).exists():
            return

    raise serializers.ValidationError("You do not have access to this offering")


def _has_active_course_account(user) -> bool:
    ok_course_accounts = models.CourseAccount.objects.filter(
        user=user,
        state=enums.CourseAccountState.OK,
        project__is_removed=False,
    )
    if ok_course_accounts.exists():
        logger.debug(
            "User %s (uuid=%s) skipped for deactivation: has %d OK course account(s) in active projects",
            user.username,
            user.uuid,
            ok_course_accounts.count(),
        )
        return True
    return False


def _course_account_deactivation_detail(user) -> str | None:
    all_course_accounts = models.CourseAccount.objects.filter(user=user)
    if not all_course_accounts.exists():
        return None
    ca_details = list(
        all_course_accounts.values_list(
            "uuid", "state", "project__uuid", "project__is_removed"
        )
    )
    return (
        f"No active roles, {all_course_accounts.count()} course account(s) "
        f"but none in OK state with active project. Details: {ca_details}"
    )


def _active_course_account_exists() -> Exists:
    return Exists(
        models.CourseAccount.objects.filter(
            user=OuterRef("pk"),
            state=enums.CourseAccountState.OK,
            project__is_removed=False,
        )
    )


COURSE_ACCOUNT_GUARD = ActiveAccountGuard(
    has_active_account=_has_active_course_account,
    deactivation_detail=_course_account_deactivation_detail,
    active_account_exists=_active_course_account_exists,
)


def register_core_hooks() -> None:
    """Register marketplace validators, guards, and PAT filters into waldur_core."""
    register_offering_access_validator(validate_offering_access_for_event_subscription)
    register_active_account_guard(COURSE_ACCOUNT_GUARD)

    @register_pat_filter(models.Offering)
    def _offering(ids):
        return _or(
            _ids_q("id", ids.get("offering")),
            _ids_q("customer_id", ids.get("customer")),
            _ids_q("customer__serviceprovider__id", ids.get("service_provider")),
        )

    @register_pat_filter(models.Resource)
    def _resource(ids):
        # NOTE: ``Resource.customer`` is a property that returns
        # ``project.customer`` — the *consumer* customer. ``get_scope_ancestors``
        # therefore only reaches ``project.customer``, never ``offering.customer``.
        # We must mirror that exactly: a binding to the offering's owner customer
        # is NOT permitted to act on resources sold via that offering when the
        # consumer is a different customer, so listing those resources here
        # would over-disclose. A binding to the offering's *service provider*
        # is: ``_pat_provider_binding_matches`` allows it, so it is listed here too.
        return _or(
            _ids_q("id", ids.get("resource")),
            _ids_q("offering_id", ids.get("offering")),
            _ids_q("project_id", ids.get("project")),
            _ids_q("project__customer_id", ids.get("customer")),
            _ids_q(
                "offering__customer__serviceprovider__id",
                ids.get("service_provider"),
            ),
        )

    @register_pat_filter(models.ResourceProject)
    def _resource_project(ids):
        # Same reasoning as ``_resource`` — only the consumer-side customer
        # chain is reachable from ``get_scope_ancestors``, plus the service
        # provider of the resource's offering.
        return _or(
            _ids_q("id", ids.get("resource_project")),
            _ids_q("resource_id", ids.get("resource")),
            _ids_q("resource__offering_id", ids.get("offering")),
            _ids_q("resource__project_id", ids.get("project")),
            _ids_q("resource__project__customer_id", ids.get("customer")),
            _ids_q(
                "resource__offering__customer__serviceprovider__id",
                ids.get("service_provider"),
            ),
        )

    @register_pat_filter(models.ServiceProvider, view_aware=True)
    def _service_provider(ids, view=None):
        # A token bound to an offering may list and retrieve the record of the
        # provider selling it: the site agent looks it up before anything
        # else. Only those two actions: a detail action such as list_users
        # finds the provider through this same filter and checks only the
        # user's roles, so it must not reach the provider from an offering
        # binding.
        offering_ids = (
            ids.get("offering")
            if getattr(view, "action", None) in ("list", "retrieve")
            else None
        )
        return _or(
            _ids_q("id", ids.get("service_provider")),
            _ids_q("customer_id", ids.get("customer")),
            Q(
                customer_id__in=models.Offering.objects.filter(
                    id__in=offering_ids
                ).values("customer_id")
            )
            if offering_ids
            else None,
        )
