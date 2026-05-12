"""SCIM Group ↔ Waldur (scope, role) translation.

Waldur has no first-class "Group" model: authorisation is expressed as
(``Role``, scope) pairs in ``UserRole``. To map SCIM Group provisioning onto
this, we adopt the naming convention:

    waldur:<scope>:<uuid>:<role-name>

Examples::

    waldur:customer:f1b3...:owner          # grant Customer Owner on a given customer
    waldur:project:af3b...:project_manager # grant Project Manager on a given project

This is self-describing — IdP administrators can construct group names from
Waldur's URL structure without needing a separate mapping API. Names that
don't match the pattern, reference unknown scopes, or unknown roles return a
SCIM 400 ``invalidValue`` so misconfigured clients fail loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.db.models import Model

from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer, Project
from waldur_core.users.scim.server.exceptions import ScimError

_GROUP_DISPLAY_NAME_RE = re.compile(
    r"^waldur:(?P<scope>customer|project):(?P<uuid>[0-9a-f]{32}):(?P<role>[a-z0-9_\-\.]+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedGroup:
    """Result of parsing a SCIM Group displayName.

    ``scope`` is a Customer or Project model instance; ``role`` is the matching
    Role (scoped to the same content type). ``group_id`` is a stable identifier
    derived from the displayName — used as the SCIM resource ``id``.
    """

    display_name: str
    scope: Model
    role: Role

    @property
    def group_id(self) -> str:
        return self.display_name


def resolve(display_name: str) -> ResolvedGroup:
    """Parse a SCIM Group displayName into ``(scope, role)``.

    Raises ``ScimError(400, ..., "invalidValue")`` on any failure with an
    explanatory ``detail`` so IdP administrators can correct the group name.
    """
    if not display_name:
        raise ScimError(
            400, "Group 'displayName' is required.", scim_type="invalidValue"
        )

    match = _GROUP_DISPLAY_NAME_RE.match(display_name)
    if not match:
        raise ScimError(
            400,
            (
                "Group 'displayName' must match "
                "'waldur:<customer|project>:<uuid>:<role>'."
            ),
            scim_type="invalidValue",
        )

    scope_kind = match.group("scope").lower()
    uuid = match.group("uuid").lower()
    role_name = match.group("role")

    scope_model = Customer if scope_kind == "customer" else Project
    try:
        scope_obj = scope_model.objects.get(uuid=uuid)
    except scope_model.DoesNotExist:
        raise ScimError(
            404,
            f"{scope_kind.capitalize()} with uuid {uuid!r} not found.",
            scim_type="invalidValue",
        )

    content_type = ContentType.objects.get_for_model(scope_model)
    role_qs = Role.objects.filter(content_type=content_type, name__iexact=role_name)
    role = role_qs.first()
    if role is None:
        raise ScimError(
            400,
            (
                f"Role {role_name!r} is not defined for {scope_kind}; "
                "create the role before assigning it."
            ),
            scim_type="invalidValue",
        )

    return ResolvedGroup(display_name=display_name, scope=scope_obj, role=role)


def render_group_id(display_name: str) -> str:
    """Group SCIM ``id`` is the displayName itself.

    Groups are virtual (no DB row), so the displayName uniquely identifies
    them across reads/writes.
    """
    return display_name


def parse_group_id(group_id: str) -> ResolvedGroup:
    """Inverse of :func:`render_group_id`: decode the SCIM ``id`` back."""
    return resolve(group_id)
