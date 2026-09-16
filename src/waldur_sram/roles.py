"""Placeholder organization roles for SRAM collaborations and their groups.

Every SRAM group (a collaboration or a group inside one) gets a customer-scoped
role, private to the group's organization, and SRAM's member list decides who
holds it:

- the role copies the permissions of ``SRAM_PLACEHOLDER_ROLE_TEMPLATE`` (none by
  default), is named ``CUSTOMER.<org-slug>.SRAM.<co>[.<group>]`` and described
  by the SRAM display name;
- a push grants it to members that lack it and revokes the grants SRAM made for
  members that left. A grant made by a person is left alone, and satisfies
  membership, until the group itself is deleted;
- deleting the group revokes every grant of the role and deletes the role.

Grants SRAM makes carry ``UserRole.source = "sram:<group uuid>"``.
"""

from __future__ import annotations

import logging
import re

from constance import config
from django.contrib.contenttypes.models import ContentType
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.models import Role, RoleAvailability, UserRole
from waldur_core.permissions.utils import (
    add_user,
    ensure_unique_role_name,
    validate_role_grant,
)
from waldur_core.structure.models import Customer
from waldur_core.users.scim.server.exceptions import ScimError

from . import models

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "sram:"
_NAME_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_-]+")
# ensure_unique_role_name appends "-2", "-3", ... on a clash.
_COLLISION_SUFFIX_RE = re.compile(r"-\d+$")


def grant_source(group: models.SramGroup) -> str:
    return f"{SOURCE_PREFIX}{group.uuid.hex}"


def _customer_ct():
    return ContentType.objects.get_for_model(Customer)


def placeholder_template() -> Role | None:
    name = (config.SRAM_PLACEHOLDER_ROLE_TEMPLATE or "").strip()
    if not name:
        return None
    role = Role.objects.filter(name=name, content_type=_customer_ct()).first()
    if role is None:
        raise ScimError(
            503,
            f"SRAM_PLACEHOLDER_ROLE_TEMPLATE: no organization role named {name!r}.",
        )
    return role


def _segment(value: str) -> str:
    return _NAME_SEGMENT_RE.sub("_", value).strip("_") or "_"


def role_name(group: models.SramGroup) -> str:
    parts = group.urn_parts[1:] or [group.uuid.hex]
    segments = ".".join(_segment(part) for part in parts)
    return f"CUSTOMER.{_segment(group.customer.slug)}.SRAM.{segments}"


def _sync_permissions(role: Role, template: Role | None) -> None:
    wanted = (
        set(template.permissions.values_list("permission", flat=True))
        if template
        else set()
    )
    current = set(role.permissions.values_list("permission", flat=True))
    for permission in wanted - current:
        role.add_permission(permission)
    for permission in current - wanted:
        role.delete_permission(permission)


def ensure_role(group: models.SramGroup) -> Role:
    """Create or refresh the group's placeholder role. Call inside a transaction."""
    template = placeholder_template()
    customer_ct = _customer_ct()
    role = group.role
    if role is None:
        role = Role(content_type=customer_ct, is_system_role=False)
    role.description = group.display_name
    name = role_name(group)
    if not role.pk or _COLLISION_SUFFIX_RE.sub("", role.name) != name:
        role.name = ensure_unique_role_name(name, exclude_id=role.pk)
    role.save()
    _sync_permissions(role, template)

    availability = RoleAvailability.objects.filter(role=role)
    if not availability.filter(
        content_type=customer_ct, object_id=group.customer_id
    ).exists():
        # The group moved to another organization, or the role is new.
        _revoke(role, UserRole.objects.filter(role=role, is_active=True), group)
        availability.delete()
        RoleAvailability.objects.create(
            role=role, content_type=customer_ct, object_id=group.customer_id
        )

    if group.role_id != role.id:
        group.role = role
        group.save(update_fields=["role"])
    return role


def _revoke(role: Role, user_roles, group: models.SramGroup, reason=None) -> None:
    for user_role in user_roles.select_related("user"):
        user_role.revoke(
            reason=reason
            or f"Removed from SRAM {group.get_kind_display().lower()} "
            f"{group.display_name}",
        )


def sync_members(group: models.SramGroup) -> None:
    """Make the role's holders follow SRAM's member list."""
    role = ensure_role(group)
    customer = group.customer
    held = UserRole.objects.filter(
        role=role,
        content_type=_customer_ct(),
        object_id=customer.id,
        is_active=True,
    )
    holders = set(held.values_list("user_id", flat=True))
    # A suspended account stays without the role: granting one would reactivate it.
    members = list(group.members.filter(is_active=True))
    member_ids = {user.id for user in members}

    for user in members:
        if user.id in holders:
            continue
        try:
            validate_role_grant(customer, user, role)
            add_user(
                customer,
                user,
                role,
                source=grant_source(group),
                reason=f"Member of SRAM {group.get_kind_display().lower()} "
                f"{group.display_name}",
            )
        except ValidationError as exc:
            logger.warning(
                "SRAM: skipped granting %s to %s: %s", role.name, user.uuid.hex, exc
            )

    _revoke(
        role,
        held.filter(source=grant_source(group)).exclude(user_id__in=member_ids),
        group,
    )


def delete_role(group: models.SramGroup) -> None:
    """Revoke every grant of the group's role, then delete the role."""
    role = group.role
    if role is None:
        return
    _revoke(
        role,
        UserRole.objects.filter(role=role, is_active=True),
        group,
        reason=f"SRAM {group.get_kind_display().lower()} {group.display_name} "
        "was deleted",
    )
    group.role = None
    group.save(update_fields=["role"])
    role.delete()


def rename_roles_for_customer(customer: Customer) -> None:
    for group in models.SramGroup.objects.filter(
        customer=customer, role__isnull=False
    ).select_related("role", "customer"):
        name = role_name(group)
        if _COLLISION_SUFFIX_RE.sub("", group.role.name) != name:
            group.role.name = ensure_unique_role_name(name, exclude_id=group.role_id)
            group.role.save(update_fields=["name"])
