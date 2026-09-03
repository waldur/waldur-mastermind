"""Which event groups a deployment can actually emit.

``EVENT_GROUP_MAPPING`` is a static catalogue of every event group Waldur knows
about, and every deployment ships all of it. That is correct for *dispatch* --
matching an emitted event against a hook, expanding ``?feature=`` filters,
validating a hook write -- and wrong for *discovery*: the notification dialog
asked users of an HPC-only deployment to choose between OpenStack floating IPs
and OpenStack RBAC policies, neither of which can ever fire there.

So discovery goes through this module instead. Every group declares how to tell
whether this deployment can emit it, and :func:`get_available_event_groups`
returns only the groups whose declaration holds. Nothing here narrows delivery:
a group hidden from the catalogue is still deliverable, still writable through
the hooks API and still expandable in a filter, so an existing subscription is
never invalidated by a deployment changing shape.

Declarations are evaluated against live data, not operator configuration -- the
deployment already knows whether it runs OpenStack, and asking an operator to
maintain a second list only adds a way to be wrong. See
waldur/waldur-mastermind#340.
"""

import logging
from dataclasses import dataclass

from constance import config
from django.apps import apps
from django.core.cache import cache
from django.db import transaction

from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup

logger = logging.getLogger(__name__)

CACHE_KEY = "waldur_core.logging.available_event_groups"

# OfferingTypes and FeatureFlag are invalidated explicitly when an offering or
# a feature toggle is written. ConstanceSetting has no signal to hang a
# receiver off, so for that one the timeout is how long a change takes to be
# noticed.
CACHE_TIMEOUT = 5 * 60


class Availability:
    """How to decide whether a deployment can emit a group's events."""

    def is_available(self) -> bool:
        raise NotImplementedError


class Always(Availability):
    """Core behaviour that every deployment has by virtue of being Waldur."""

    def is_available(self) -> bool:
        return True


@dataclass(frozen=True)
class OfferingTypes(Availability):
    """Available once the marketplace holds an offering served by this plugin.

    Offerings are provisioned by an administrator before anyone can order from
    them, so their presence leads user activity rather than trailing it.
    """

    types: tuple[str, ...]

    def is_available(self) -> bool:
        offering = apps.get_model("marketplace", "Offering")
        return offering.objects.filter(type__in=self.types).exists()


@dataclass(frozen=True)
class FeatureFlag(Availability):
    """Available while a `core.Feature` toggle is on."""

    key: str

    def is_available(self) -> bool:
        feature = apps.get_model("core", "Feature")
        return feature.objects.filter(key=self.key, value=True).exists()


@dataclass(frozen=True)
class ConstanceSetting(Availability):
    """Available while a Constance setting is truthy."""

    key: str

    def is_available(self) -> bool:
        return bool(getattr(config, self.key))


ALWAYS = Always()

# OpenStack plugin offering types. Kept as literals rather than imported from
# waldur_mastermind.marketplace.enums: waldur_core must not import mastermind.
# test_availability.py asserts they stay registered offering types, so a rename
# fails there instead of silently hiding the groups.
OPENSTACK_OFFERINGS = OfferingTypes(
    ("OpenStack.Tenant", "OpenStack.Instance", "OpenStack.Volume")
)

EVENT_GROUP_AVAILABILITY: dict[EventGroup, Availability] = {
    # Gating these two on "does the deployment hold an access subnet / a
    # credit yet" reads natural -- both are administrator-provisioned -- but
    # each group's contents are that record's own *_CREATION_SUCCEEDED events.
    # Nobody could subscribe until after the first one was created, and for a
    # security or finance notification the first event is the one worth having.
    EventGroup.ACCESS_SUBNETS: ALWAYS,
    EventGroup.AUTH: ALWAYS,
    EventGroup.CALL: FeatureFlag("marketplace.show_call_management_functionality"),
    EventGroup.CHAT: ConstanceSetting("AI_ASSISTANT_ENABLED"),
    EventGroup.CREDITS: ALWAYS,
    EventGroup.CUSTOMERS: ALWAYS,
    EventGroup.INVOICES: ALWAYS,
    EventGroup.OFFERING_ACCOUNTING: ALWAYS,
    EventGroup.ONBOARDING: FeatureFlag("customer.show_onboarding"),
    EventGroup.OPENSTACK_FLOATING_IP: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_NETWORK: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_PORT: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_RBAC: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_RESOURCES: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_ROUTER: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_SECURITY_GROUP: OPENSTACK_OFFERINGS,
    EventGroup.OPENSTACK_SUBNET: OPENSTACK_OFFERINGS,
    EventGroup.PERMISSIONS: ALWAYS,
    EventGroup.PROJECTS: ALWAYS,
    EventGroup.PROPOSAL: FeatureFlag("marketplace.show_call_management_functionality"),
    EventGroup.PROVIDERS: ALWAYS,
    EventGroup.RESOURCES: ALWAYS,
    EventGroup.REVIEW: FeatureFlag("marketplace.show_call_management_functionality"),
    EventGroup.SSH: ALWAYS,
    EventGroup.SUPPORT: ALWAYS,
    EventGroup.TERMS_OF_SERVICE: ALWAYS,
    EventGroup.USERS: ALWAYS,
}


def get_undeclared_groups() -> list[EventGroup]:
    """Groups with no availability declaration. Reported by a system check."""
    return [
        group for group in EVENT_GROUP_MAPPING if group not in EVENT_GROUP_AVAILABILITY
    ]


def _is_available(group: EventGroup, state: dict) -> bool:
    declaration = EVENT_GROUP_AVAILABILITY.get(group)
    if declaration is None:
        # The system check reports this; advertise the group meanwhile rather
        # than hiding events a deployment does emit.
        return True
    try:
        return declaration.is_available()
    except Exception:
        state["degraded"] = True
        # A missing table, an unreachable Constance backend or a model from an
        # app that is not loaded must not take the catalogue down. Fail open:
        # an extra group in the dialog is a smaller failure than a missing one.
        logger.warning(
            "Unable to evaluate availability of event group %s, advertising it.",
            group.value,
            exc_info=True,
        )
        return True


def get_available_group_keys() -> list[str]:
    """Keys of the event groups this deployment can emit, cached."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    state: dict = {}
    keys = [group.value for group in EVENT_GROUP_MAPPING if _is_available(group, state)]
    if not state.get("degraded"):
        # Never cache a fail-open answer. A declaration that raised because a
        # worker served traffic mid-migrate would otherwise pin "advertise
        # everything" for CACHE_TIMEOUT after the fault cleared, with only an
        # Offering write able to shift it.
        cache.set(CACHE_KEY, keys, CACHE_TIMEOUT)
    return keys


def get_available_event_groups() -> dict[str, list[str]]:
    """`get_event_groups()` narrowed to what this deployment can emit."""
    available = set(get_available_group_keys())
    return {
        key.value: [item.value for item in value]
        for key, value in EVENT_GROUP_MAPPING.items()
        if key.value in available
    }


def invalidate_cache(*args, **kwargs) -> None:
    """Drop the cached set."""
    cache.delete(CACHE_KEY)


def invalidate_cache_on_commit(*args, **kwargs) -> None:
    """Signal receiver that defers the drop until the transaction commits.

    post_save fires inside the transaction, so dropping the key there leaves a
    window in which another request recomputes the set from data the commit has
    not published yet and re-caches the stale answer for a full CACHE_TIMEOUT.
    """
    transaction.on_commit(invalidate_cache)
