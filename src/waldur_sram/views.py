"""SCIM 2.0 endpoints for SRAM, mounted at ``/scim/v2/sram/``.

SRAM (SURFscz/SBS) provisions a service by pushing Users and Groups, and
periodically sweeps: it lists everything the service returns and DELETEs what
it does not recognise. These endpoints therefore only ever list, read, update
and delete objects SRAM itself provisioned (``SramUser`` / ``SramGroup``), and
return ``meta.location`` relative to the SRAM base URL because SBS requests
``{scim_url}{meta.location}``.

Attribute writes reuse the generic inbound SCIM code, so the attribute-source
policy, ``SCIM_INBOUND_ALLOWED_ATTRIBUTES`` and ``SCIM_INBOUND_SSH_KEYS_ENABLED``
apply unchanged.
"""

from __future__ import annotations

import logging

from constance import config
from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_auth_social.utils import remove_user_from_isd
from waldur_core.core.models import User
from waldur_core.users.scim.server import matching
from waldur_core.users.scim.server.auth import (
    IsScimStaff,
    ScimBearerAuthentication,
    ScimFeatureEnabled,
)
from waldur_core.users.scim.server.exceptions import ScimError, scim_exception_handler
from waldur_core.users.scim.server.filters import FilterField
from waldur_core.users.scim.server.filters import parse as parse_filter
from waldur_core.users.scim.server.pagination import list_response, parse_pagination
from waldur_core.users.scim.server.renderers import (
    ScimJSONParser,
    ScimJSONParserPlain,
    ScimJSONRenderer,
)
from waldur_core.users.scim.server.users_view import (
    create_user,
    scim_source,
    update_user,
)

from . import mapping, models, organizations

logger = logging.getLogger(__name__)

USER_FILTER_FIELDS: dict[str, FilterField] = {
    "externalid": FilterField("sram_user__external_id", case_insensitive=False),
    "username": FilterField("username"),
    "emails": FilterField("email"),
    "emails.value": FilterField("email"),
    "active": FilterField("is_active", case_insensitive=False, boolean=True),
}

GROUP_FILTER_FIELDS: dict[str, FilterField] = {
    "externalid": FilterField("external_id", case_insensitive=False),
    "displayname": FilterField("display_name"),
}


class SramIntegrationEnabled(BasePermission):
    message = (
        "SRAM provisioning is disabled (set Constance SRAM_INTEGRATION_ENABLED=True)."
    )

    def has_permission(self, request, view):
        return bool(config.SRAM_INTEGRATION_ENABLED)


class SramBaseView(APIView):
    renderer_classes = [ScimJSONRenderer]
    parser_classes = [ScimJSONParser, ScimJSONParserPlain]
    authentication_classes = [ScimBearerAuthentication]
    permission_classes = [ScimFeatureEnabled, SramIntegrationEnabled, IsScimStaff]
    schema = None

    def get_exception_handler(self):
        return scim_exception_handler


def _json_body(request) -> dict:
    body = request.data
    if not isinstance(body, dict):
        raise ScimError(400, "Request body must be a JSON object.")
    return body


def _required_external_id(body: dict) -> str:
    external_id = str(body.get("externalId") or "").strip()
    if not external_id:
        raise ScimError(400, "externalId is required.", scim_type="invalidValue")
    return external_id


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def _sram_users():
    # all_objects: SRAM must keep seeing (and be able to reactivate) the users it
    # deactivated.
    return User.all_objects.filter(sram_user__isnull=False).select_related("sram_user")


def _get_sram_user_or_404(uuid_hex: str) -> User:
    try:
        return _sram_users().get(uuid=uuid_hex)
    except (User.DoesNotExist, ValueError):
        raise ScimError(404, f"User {uuid_hex!r} not found.")


def _match_existing_user(body: dict) -> User | None:
    """An account SRAM may adopt: it matches, and is neither privileged nor linked.

    Matching follows ``SCIM_USER_MATCH_WALDUR_ATTRIBUTE`` /
    ``SCIM_USER_MATCH_SCIM_ATTRIBUTE``; the default compares ``userName`` with
    the username.
    """
    candidate = matching.find_matching_user(body)
    if candidate is None:
        return None
    user_name = candidate.username
    if candidate.is_staff or candidate.is_support:
        raise ScimError(
            409,
            f"User {user_name!r} is a privileged local account and cannot be "
            "provisioned by SRAM.",
            scim_type="uniqueness",
        )
    if hasattr(candidate, "sram_user"):
        raise ScimError(
            409,
            f"User {user_name!r} is already linked to another SRAM identity.",
            scim_type="uniqueness",
        )
    return candidate


def _without_active(scim_body: dict) -> tuple[dict, bool | None]:
    """Split SRAM's ``active`` flag from the attributes.

    SRAM sends ``active: false`` for a suspended user, which it may lift again.
    The generic SCIM path treats that like a removal and clears every attribute
    the source owns, so the next sweep sees a changed user and re-sends it. A
    suspension only deactivates the account here; removal is a DELETE.
    """
    body = dict(scim_body)
    active = body.pop("active", None)
    if isinstance(active, str):
        active = active.lower() == "true"
    return body, active


def _apply_active(user: User, active: bool | None) -> None:
    if active is None or active == user.is_active:
        return
    user.is_active = active
    user.deactivation_reason = "" if active else "Suspended in SRAM"
    user._change_source = scim_source()
    user.save(update_fields=["is_active", "deactivation_reason"])


@extend_schema(exclude=True)
class UsersListView(SramBaseView):
    def get(self, request):
        qs = _sram_users().order_by("id")
        filter_expr = request.query_params.get("filter")
        if filter_expr:
            qs = qs.filter(parse_filter(filter_expr, USER_FILTER_FIELDS))
        total = qs.count()
        start, count = parse_pagination(request)
        page = list(qs[start - 1 : start - 1 + count])
        return Response(
            list_response(
                [mapping.render_user(user, user.sram_user) for user in page],
                total=total,
                start_index=start,
                count=count,
            )
        )

    def post(self, request):
        body = _json_body(request)
        external_id = _required_external_id(body)
        if models.SramUser.objects.filter(external_id=external_id).exists():
            raise ScimError(
                409,
                f"User with externalId {external_id!r} already exists.",
                scim_type="uniqueness",
            )
        scim_body, active = _without_active(mapping.sram_user_to_scim_body(body))
        try:
            with transaction.atomic():
                user = _match_existing_user(body)
                if user is None:
                    user = create_user(scim_body, request)
                else:
                    logger.info(
                        "SRAM: linking existing user %s to %s",
                        user.username,
                        external_id,
                    )
                    user = update_user(
                        user, scim_body, full_replace=True, check_username=False
                    )
                    if active is None:
                        active = True
                _apply_active(user, active)
                sram_user = models.SramUser.objects.create(
                    user=user, external_id=external_id, payload=body
                )
        except IntegrityError:
            raise ScimError(
                409,
                f"User with externalId {external_id!r} already exists.",
                scim_type="uniqueness",
            )
        response = Response(
            mapping.render_user(user, sram_user), status=status.HTTP_201_CREATED
        )
        response["Location"] = mapping.user_location(user)
        return response


@extend_schema(exclude=True)
class UserDetailView(SramBaseView):
    def get(self, request, uuid_hex):
        user = _get_sram_user_or_404(uuid_hex)
        return Response(mapping.render_user(user, user.sram_user))

    def put(self, request, uuid_hex):
        user = _get_sram_user_or_404(uuid_hex)
        body = _json_body(request)
        external_id = _required_external_id(body)
        sram_user = user.sram_user
        if (
            external_id != sram_user.external_id
            and models.SramUser.objects.filter(external_id=external_id).exists()
        ):
            raise ScimError(
                409,
                f"externalId {external_id!r} belongs to another user.",
                scim_type="uniqueness",
            )
        scim_body, active = _without_active(mapping.sram_user_to_scim_body(body))
        with transaction.atomic():
            user = update_user(user, scim_body, full_replace=True, check_username=False)
            _apply_active(user, active)
            sram_user.external_id = external_id
            sram_user.payload = body
            sram_user.save(update_fields=["external_id", "payload", "modified"])
        return Response(mapping.render_user(user, sram_user))

    def patch(self, request, uuid_hex):
        _get_sram_user_or_404(uuid_hex)
        raise ScimError(
            400,
            "PATCH is not supported for SRAM users; send the full resource with PUT.",
            scim_type="invalidSyntax",
        )

    def delete(self, request, uuid_hex):
        user = _get_sram_user_or_404(uuid_hex)
        with transaction.atomic():
            remove_user_from_isd(user, source=scim_source())
            models.SramUser.objects.filter(user=user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def _get_group_or_404(uuid_hex: str) -> models.SramGroup:
    try:
        return models.SramGroup.objects.get(uuid=uuid_hex)
    except (models.SramGroup.DoesNotExist, ValueError):
        raise ScimError(404, f"Group {uuid_hex!r} not found.")


def _resolve_members(body: dict) -> list[User]:
    """Members are the user ids this service returned to SRAM.

    Unknown ids are skipped with a warning: SBS provisions members one by one,
    and a member it failed to provision must not block the whole group.
    """
    ids = mapping.member_ids(body)
    if not ids:
        return []
    users = list(_sram_users().filter(uuid__in=_valid_hex(ids)))
    found = {user.uuid.hex for user in users}
    missing = [member for member in ids if member not in found]
    if missing:
        logger.warning(
            "SRAM group %s references unknown users: %s",
            body.get("externalId"),
            missing,
        )
    return users


def _valid_hex(values: list[str]) -> list[str]:
    result = []
    for value in values:
        candidate = value.replace("-", "").lower()
        if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
            result.append(candidate)
    return result


def apply_group(group: models.SramGroup, body: dict) -> models.SramGroup:
    fields = mapping.group_fields(body)
    if not fields["display_name"]:
        raise ScimError(400, "displayName is required.", scim_type="invalidValue")
    for name, value in fields.items():
        setattr(group, name, value)
    group.customer = organizations.resolve_customer(group.organisation_short_name)
    group.payload = body
    group.save()
    group.members.set(_resolve_members(body))
    return group


@extend_schema(exclude=True)
class GroupsListView(SramBaseView):
    def get(self, request):
        qs = models.SramGroup.objects.order_by("id")
        filter_expr = request.query_params.get("filter")
        if filter_expr:
            qs = qs.filter(parse_filter(filter_expr, GROUP_FILTER_FIELDS))
        total = qs.count()
        start, count = parse_pagination(request)
        page = list(qs[start - 1 : start - 1 + count])
        return Response(
            list_response(
                [mapping.render_group(group) for group in page],
                total=total,
                start_index=start,
                count=count,
            )
        )

    def post(self, request):
        body = _json_body(request)
        external_id = _required_external_id(body)
        if models.SramGroup.objects.filter(external_id=external_id).exists():
            raise ScimError(
                409,
                f"Group with externalId {external_id!r} already exists.",
                scim_type="uniqueness",
            )
        try:
            with transaction.atomic():
                group = apply_group(models.SramGroup(external_id=external_id), body)
        except IntegrityError:
            raise ScimError(
                409,
                f"Group with externalId {external_id!r} already exists.",
                scim_type="uniqueness",
            )
        response = Response(mapping.render_group(group), status=status.HTTP_201_CREATED)
        response["Location"] = mapping.group_location(group)
        return response


@extend_schema(exclude=True)
class GroupDetailView(SramBaseView):
    def get(self, request, uuid_hex):
        return Response(mapping.render_group(_get_group_or_404(uuid_hex)))

    def put(self, request, uuid_hex):
        body = _json_body(request)
        external_id = _required_external_id(body)
        with transaction.atomic():
            group = models.SramGroup.objects.select_for_update().get(
                pk=_get_group_or_404(uuid_hex).pk
            )
            if (
                external_id != group.external_id
                and models.SramGroup.objects.filter(external_id=external_id).exists()
            ):
                raise ScimError(
                    409,
                    f"externalId {external_id!r} belongs to another group.",
                    scim_type="uniqueness",
                )
            group.external_id = external_id
            group = apply_group(group, body)
        return Response(mapping.render_group(group))

    def patch(self, request, uuid_hex):
        _get_group_or_404(uuid_hex)
        raise ScimError(
            400,
            "PATCH is not supported for SRAM groups; send the full resource with PUT.",
            scim_type="invalidSyntax",
        )

    def delete(self, request, uuid_hex):
        group = _get_group_or_404(uuid_hex)
        with transaction.atomic():
            group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
