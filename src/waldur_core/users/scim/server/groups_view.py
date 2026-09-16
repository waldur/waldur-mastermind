"""SCIM 2.0 ``/Groups`` endpoint.

Groups are *virtual* in Waldur — there's no DB row per SCIM Group. A Group
corresponds to the set of users holding a given ``(scope, role)`` pair, where
``scope`` is a Customer or Project and ``role`` is the matching ``Role`` row.

The SCIM Group ``displayName`` doubles as the resource ``id`` and is parsed by
``group_resolver`` into the underlying entities. Group listing enumerates only
groups that have at least one active member, so a deployment with thousands of
customers and projects doesn't surface a cartesian-product catalogue.
"""

from __future__ import annotations

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import add_user, delete_user, validate_role_grant
from waldur_core.structure.models import Customer, Project
from waldur_core.users.scim.server.auth import (
    IsScimStaff,
    ScimBearerAuthentication,
    ScimFeatureEnabled,
)
from waldur_core.users.scim.server.exceptions import (
    ScimError,
    scim_exception_handler,
)
from waldur_core.users.scim.server.group_resolver import (
    ResolvedGroup,
    parse_group_id,
    resolve,
)
from waldur_core.users.scim.server.pagination import list_response, parse_pagination
from waldur_core.users.scim.server.patch import (
    apply_group_patch,
    validate_patch_envelope,
)
from waldur_core.users.scim.server.renderers import (
    ScimJSONParser,
    ScimJSONParserPlain,
    ScimJSONRenderer,
)

CORE_GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:Group"

logger = logging.getLogger(__name__)


def _require_grantable_role(resolved: ResolvedGroup) -> None:
    """Refuse provisioning into a group whose role cannot be granted at all.

    Deactivating a role is how an administrator takes it out of circulation:
    ``validate_role_grant`` refuses it for REST grants, invitation accept,
    permission requests and OpenPortal awards, and both the invitation
    serializers and homeport hide it from their role pickers. Group sync has to
    agree, or an identity provider keeps assigning a role nobody can assign or
    review by hand.

    This is checked per request rather than per member because it is a property
    of the group, not of anyone in it: every member would be rejected for the
    same reason, and answering 200 with an empty member list would tell the
    provider it succeeded. Callers invoke it only when an operation actually
    adds members — reading a group, removing members and deleting it stay
    available for a deactivated role, since deactivation gates new grants and
    must never block deprovisioning.

    A request that both adds and removes is refused whole, because a PATCH is
    all or nothing: the addition cannot be satisfied, so the removal is rolled
    back with it rather than leaving the provider a partially applied sync it
    was told had failed. Deprovisioning such a group still works through a
    remove-only PATCH or a DELETE.
    """
    if not resolved.role.is_active:
        raise ScimError(
            400,
            (
                f"Role {resolved.role.name!r} is not active and cannot be "
                "assigned; enable the role before provisioning this group."
            ),
            scim_type="invalidValue",
        )


def _grant_membership(resolved, user, request_user) -> None:
    """Grant the group's role, applying the same checks as every other path.

    ``validate_role_grant`` is what the REST grant endpoint, ``Invitation.accept``
    and ``PermissionRequest.approve`` run; going through it here keeps group sync
    from being the one way into a role assignment the rest of Waldur refuses.
    (``add_user`` enforces only the org-scoping policy.)

    A member the policy rejects — a concealed or org-unavailable role, a scope
    whose email/affiliation restrictions the user does not match, a second role
    where ``INVITATION_DISABLE_MULTIPLE_ROLES`` forbids one, a second project
    manager — is skipped and logged rather than aborting the whole group sync:
    one member's rejection must not fail provisioning for every other member.
    The group-wide reasons are raised by :func:`_require_grantable_role` before
    we get here.
    """
    try:
        validate_role_grant(resolved.scope, user, resolved.role)
        add_user(resolved.scope, user, resolved.role, created_by=request_user)
    except ValidationError as exc:
        logger.warning(
            "SCIM: skipped granting role %s to user %s on %s: %s",
            resolved.role,
            user.uuid.hex,
            resolved.scope,
            exc,
        )


class _GroupsBaseView(APIView):
    renderer_classes = [ScimJSONRenderer]
    parser_classes = [ScimJSONParser, ScimJSONParserPlain]
    authentication_classes = [ScimBearerAuthentication]
    permission_classes = [ScimFeatureEnabled, IsScimStaff]
    schema = None

    def get_exception_handler(self):
        return scim_exception_handler


def _location(request, group_id: str) -> str:
    try:
        return request.build_absolute_uri(f"/scim/v2/Groups/{group_id}")
    except Exception:  # pragma: no cover
        return f"/scim/v2/Groups/{group_id}"


def _members_qs(resolved: ResolvedGroup):
    """Active ``UserRole`` rows for a (scope, role) pair."""
    content_type = ContentType.objects.get_for_model(type(resolved.scope))
    return UserRole.objects.filter(
        content_type=content_type,
        object_id=resolved.scope.id,
        role=resolved.role,
        is_active=True,
    ).select_related("user")


def _serialize_group(resolved: ResolvedGroup, request) -> dict:
    members = []
    for role_row in _members_qs(resolved):
        user = role_row.user
        members.append(
            {
                "value": user.uuid.hex,
                "display": user.username,
                "$ref": request.build_absolute_uri(f"/scim/v2/Users/{user.uuid.hex}"),
            }
        )
    return {
        "schemas": [CORE_GROUP_URN],
        "id": resolved.display_name,
        "displayName": resolved.display_name,
        "meta": {
            "resourceType": "Group",
            "location": _location(request, resolved.display_name),
        },
        "members": members,
    }


def _enumerate_active_groups():
    """Yield ``ResolvedGroup`` for every (scope, role) pair with active members.

    We expose a Group only when at least one member exists, to avoid listing
    every possible (customer × role) and (project × role) combination.
    """
    customer_ct = ContentType.objects.get_for_model(Customer)
    project_ct = ContentType.objects.get_for_model(Project)

    active_keys = (
        UserRole.objects.filter(
            is_active=True, content_type__in=[customer_ct, project_ct]
        )
        .values("content_type_id", "object_id", "role_id")
        .distinct()
    )

    customer_map = {c.id: c for c in Customer.objects.all()}
    project_map = {p.id: p for p in Project.objects.all()}

    from waldur_core.permissions.models import Role

    role_map = {r.id: r for r in Role.objects.all()}

    for key in active_keys:
        if key["content_type_id"] == customer_ct.id:
            scope = customer_map.get(key["object_id"])
            scope_kind = "customer"
        elif key["content_type_id"] == project_ct.id:
            scope = project_map.get(key["object_id"])
            scope_kind = "project"
        else:  # pragma: no cover — filtered above
            continue
        role = role_map.get(key["role_id"])
        if scope is None or role is None:
            continue
        display = f"waldur:{scope_kind}:{scope.uuid.hex}:{role.name.lower()}"
        yield ResolvedGroup(display_name=display, scope=scope, role=role)


def _sync_members(resolved: ResolvedGroup, member_ids: list[str], request_user) -> None:
    """Drive add/remove operations against the current member set."""
    if not member_ids:
        return
    users = list(User.objects.filter(uuid__in=member_ids))
    found_ids = {u.uuid.hex for u in users}
    missing = [m for m in member_ids if m not in found_ids]
    if missing:
        raise ScimError(
            400,
            f"Group members reference unknown user uuids: {missing}",
            scim_type="invalidValue",
        )
    existing_ids = set(_members_qs(resolved).values_list("user_id", flat=True))
    to_add = [user for user in users if user.id not in existing_ids]
    if not to_add:
        return
    _require_grantable_role(resolved)
    for user in to_add:
        _grant_membership(resolved, user, request_user)


def _replace_members(
    resolved: ResolvedGroup, member_ids: list[str], request_user
) -> None:
    target_ids = set(member_ids)
    if target_ids:
        users = list(User.objects.filter(uuid__in=target_ids))
        found_ids = {u.uuid.hex for u in users}
        missing = target_ids - found_ids
        if missing:
            raise ScimError(
                400,
                f"Group members reference unknown user uuids: {sorted(missing)}",
                scim_type="invalidValue",
            )
        target_user_objs = {u.uuid.hex: u for u in users}
    else:
        target_user_objs = {}

    current = list(_members_qs(resolved))
    current_user_map = {row.user.uuid.hex: row.user for row in current}

    to_add = [
        user
        for uuid_hex, user in target_user_objs.items()
        if uuid_hex not in current_user_map
    ]
    # Checked before the first removal, not between the two loops: a replace
    # that only drops members stays available for a deactivated role, but one
    # that would also add members must not revoke anyone before failing.
    if to_add:
        _require_grantable_role(resolved)

    for uuid_hex, user in current_user_map.items():
        if uuid_hex not in target_user_objs:
            delete_user(
                resolved.scope,
                user,
                resolved.role,
                current_user=request_user,
                reason="SCIM Group membership replace",
            )
    for user in to_add:
        _grant_membership(resolved, user, request_user)


def _remove_members(
    resolved: ResolvedGroup, member_ids: list[str], request_user
) -> None:
    if not member_ids:
        return
    users = User.objects.filter(uuid__in=member_ids)
    user_map = {u.uuid.hex: u for u in users}
    for uuid_hex in member_ids:
        user = user_map.get(uuid_hex)
        if user is None:
            continue
        delete_user(
            resolved.scope,
            user,
            resolved.role,
            current_user=request_user,
            reason="SCIM Group membership remove",
        )


def _displayname_filter(filter_expr: str) -> str | None:
    """Extract a literal ``displayName eq "<value>"`` from a filter, if present."""
    import re

    match = re.match(
        r'^\s*displayname\s+eq\s+"([^"]+)"\s*$', filter_expr, re.IGNORECASE
    )
    return match.group(1) if match else None


@extend_schema(exclude=True)
class GroupsListView(_GroupsBaseView):
    def get(self, request):
        filter_expr = request.query_params.get("filter")
        if filter_expr:
            display_name = _displayname_filter(filter_expr)
            if display_name is None:
                raise ScimError(
                    400,
                    "Group listing supports only 'displayName eq \"...\"' filter.",
                    scim_type="invalidFilter",
                )
            try:
                resolved = resolve(display_name)
            except ScimError:
                # Filter is well-formed but the group doesn't exist (yet).
                return Response(list_response([], total=0, start_index=1, count=0))
            if not _members_qs(resolved).exists():
                return Response(list_response([], total=0, start_index=1, count=0))
            return Response(
                list_response(
                    [_serialize_group(resolved, request)],
                    total=1,
                    start_index=1,
                    count=1,
                )
            )

        groups = list(_enumerate_active_groups())
        total = len(groups)
        start, count = parse_pagination(request)
        page = groups[start - 1 : start - 1 + count]
        return Response(
            list_response(
                [_serialize_group(g, request) for g in page],
                total=total,
                start_index=start,
                count=count,
            )
        )

    def post(self, request):
        body = request.data
        if not isinstance(body, dict):
            raise ScimError(400, "Request body must be a JSON object.")
        display_name = body.get("displayName") or body.get("id")
        resolved = resolve(display_name)
        member_ids = [
            m.get("value")
            for m in (body.get("members") or [])
            if isinstance(m, dict) and m.get("value")
        ]
        with transaction.atomic():
            _sync_members(resolved, member_ids, request.user)
        response = Response(
            _serialize_group(resolved, request), status=status.HTTP_201_CREATED
        )
        response["Location"] = _location(request, resolved.display_name)
        return response


@extend_schema(exclude=True)
class GroupDetailView(_GroupsBaseView):
    def get(self, request, group_id):
        resolved = parse_group_id(group_id)
        return Response(_serialize_group(resolved, request))

    def put(self, request, group_id):
        resolved = parse_group_id(group_id)
        body = request.data if isinstance(request.data, dict) else {}
        member_ids = [
            m.get("value")
            for m in (body.get("members") or [])
            if isinstance(m, dict) and m.get("value")
        ]
        with transaction.atomic():
            _replace_members(resolved, member_ids, request.user)
        return Response(_serialize_group(resolved, request))

    def patch(self, request, group_id):
        resolved = parse_group_id(group_id)
        operations = validate_patch_envelope(request.data or {})
        patch_result = apply_group_patch(operations)
        with transaction.atomic():
            if patch_result.replace_member_ids is not None:
                _replace_members(
                    resolved, patch_result.replace_member_ids, request.user
                )
            # Removals before additions. A provider sends a handover as one
            # PATCH that drops the outgoing holder and adds the incoming one,
            # and `apply_group_patch` collapses the operations into buckets, so
            # their original order is already gone. Evaluating the additions
            # first means the grant is validated while the outgoing holder is
            # still in place — under ONLY_ONE_PROJECT_MANAGER that rejects the
            # incoming manager, the removal then goes through regardless, and
            # the project is left with none.
            if patch_result.remove_member_ids:
                _remove_members(resolved, patch_result.remove_member_ids, request.user)
            if patch_result.add_member_ids:
                _sync_members(resolved, patch_result.add_member_ids, request.user)
        return Response(_serialize_group(resolved, request))

    def delete(self, request, group_id):
        resolved = parse_group_id(group_id)
        with transaction.atomic():
            for role_row in list(_members_qs(resolved)):
                delete_user(
                    resolved.scope,
                    role_row.user,
                    resolved.role,
                    current_user=request.user,
                    reason="SCIM Group deletion",
                )
        return Response(status=status.HTTP_204_NO_CONTENT)
