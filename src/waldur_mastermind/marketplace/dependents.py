"""What other apps have built on an offering, a plan or a component.

A call for proposals, for one, takes an offering in, has applicants request
its components on one of its plans, and orders the award on them later.
Archiving the offering, its plan or a component from under it breaks that
quietly. Marketplace does not import those apps, so they register here a
provider that says what depends on the object, and the marketplace actions
that would pull it away ask first: they refuse, or name what they affect.

A provider is ``provider(change, obj)`` returning a ``Dependent`` or None.
``change`` names what is about to happen (one of ``Changes``), ``obj`` is the
offering, plan or component.
"""

import dataclasses

from django.utils.translation import gettext as _
from rest_framework import exceptions


class Changes:
    # The offering stops taking orders: archived, paused, sent back to
    # draft, made unavailable.
    OFFERING_WITHDRAWN = "offering_withdrawn"
    OFFERING_DELETED = "offering_deleted"
    # Moved to another organisation without its team.
    OFFERING_TEAM_DROPPED = "offering_team_dropped"
    PLAN_ARCHIVED = "plan_archived"
    PLAN_DELETED = "plan_deleted"
    COMPONENT_REMOVED = "component_removed"
    # Its billing type or limit period changes, or its maximum is lowered.
    COMPONENT_CHANGED = "component_changed"


@dataclasses.dataclass
class Dependent:
    """What depends on the object, said for the person about to change it."""

    summary: str
    # Refused for everyone but staff.
    blocks: bool = False
    # Refused for staff too: the change would destroy what depends on it.
    blocks_staff: bool = False


_providers: list = []


def register_provider(provider) -> None:
    if provider not in _providers:
        _providers.append(provider)


def dependents(change: str, obj) -> list[Dependent]:
    return [
        dependent
        for provider in _providers
        if (dependent := provider(change, obj)) is not None
    ]


class DependentsConflict(exceptions.APIException):
    status_code = 409
    default_code = "dependents"


def guard(change: str, obj, user) -> list[Dependent]:
    """Refuse ``change`` of ``obj`` if what depends on it forbids it for
    ``user``; otherwise return the dependents, for the caller to report."""
    found = dependents(change, obj)
    is_staff = bool(user and user.is_staff)
    refusing = [
        dependent
        for dependent in found
        if dependent.blocks_staff or (dependent.blocks and not is_staff)
    ]
    if refusing:
        raise DependentsConflict(
            " ".join(dependent.summary for dependent in refusing)
            + (
                " "
                + _(
                    "Staff can make the change, but should first agree with "
                    "whoever runs these."
                )
                if not is_staff and not any(d.blocks_staff for d in refusing)
                else ""
            )
        )
    return found


_renamers: list = []


def register_component_renamer(renamer) -> None:
    """Register ``renamer(offering, old_type, new_type)``, called inside the
    transaction that renames a component type, to carry the rename into what
    names the type."""
    if renamer not in _renamers:
        _renamers.append(renamer)


def component_renamed(offering, old_type: str, new_type: str) -> None:
    for renamer in _renamers:
        renamer(offering, old_type, new_type)
