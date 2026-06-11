"""SCIM 2.0 ``/Users`` endpoint (RFC 7644 §3.5).

Implements POST, GET (list + read), PUT, PATCH, DELETE. All attribute writes
route through ``waldur_auth_social.utils.update_user_attributes_from_source``
so the existing multi-source attribute-merge policy applies and provenance is
recorded in ``User.attribute_sources``.

Deactivation (``active=false`` or DELETE) is routed through
``remove_user_from_isd`` so it honours ``FEDERATED_IDENTITY_DEACTIVATION_POLICY``.
"""

from __future__ import annotations

from constance import config
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from waldur_auth_social.utils import (
    remove_user_from_isd,
    update_user_attributes_from_source,
)
from waldur_core.core.models import User
from waldur_core.users.scim.server.auth import (
    IsScimStaff,
    ScimBearerAuthentication,
    ScimFeatureEnabled,
)
from waldur_core.users.scim.server.exceptions import ScimError, scim_exception_handler
from waldur_core.users.scim.server.filters import (
    USER_FILTER_FIELDS,
)
from waldur_core.users.scim.server.filters import (
    parse as parse_filter,
)
from waldur_core.users.scim.server.mapping import (
    scim_to_waldur_payload,
    set_scim_external_id,
    waldur_to_scim_user,
)
from waldur_core.users.scim.server.pagination import list_response, parse_pagination
from waldur_core.users.scim.server.patch import (
    apply_user_patch,
    validate_patch_envelope,
)
from waldur_core.users.scim.server.renderers import (
    ScimJSONParser,
    ScimJSONParserPlain,
    ScimJSONRenderer,
)


class _UsersBaseView(APIView):
    renderer_classes = [ScimJSONRenderer]
    parser_classes = [ScimJSONParser, ScimJSONParserPlain]
    authentication_classes = [ScimBearerAuthentication]
    permission_classes = [ScimFeatureEnabled, IsScimStaff]
    schema = None

    def get_exception_handler(self):
        return scim_exception_handler


def _allowed_fields() -> set[str]:
    return set(config.SCIM_INBOUND_ALLOWED_ATTRIBUTES or [])


def _source() -> str:
    return config.SCIM_INBOUND_SOURCE_NAME or "scim:default"


def _user_location(request, user: User) -> str:
    try:
        return request.build_absolute_uri(f"/scim/v2/Users/{user.uuid.hex}")
    except Exception:  # pragma: no cover — build_absolute_uri is robust
        return f"/scim/v2/Users/{user.uuid.hex}"


def _serialize(user: User, request) -> dict:
    return waldur_to_scim_user(user, location=_user_location(request, user))


def _find_user(body: dict) -> User | None:
    """Lookup priority: externalId → userName → primary email.

    Uses ``all_objects`` — deactivated users must stay addressable so the IdM
    gets a 409 instead of a crash on re-create, and can reactivate them.
    """
    external_id = body.get("externalId")
    if external_id:
        candidate = User.all_objects.filter(
            attribute_sources__externalId__value=external_id
        ).first()
        if candidate:
            return candidate

    user_name = body.get("userName")
    if user_name:
        candidate = User.all_objects.filter(username__iexact=user_name).first()
        if candidate:
            return candidate

    if getattr(config, "OIDC_MATCHMAKING_BY_EMAIL", False):
        primary_email = _primary_email(body.get("emails"))
        if primary_email:
            matches = User.all_objects.filter(email__iexact=primary_email)
            if matches.count() == 1:
                return matches.first()
    return None


def _primary_email(emails) -> str | None:
    if not emails or not isinstance(emails, list):
        return None
    for entry in emails:
        if isinstance(entry, dict) and entry.get("primary"):
            return entry.get("value")
    if isinstance(emails[0], dict):
        return emails[0].get("value")
    return None


def _normalize_username(raw: str) -> str:
    """Waldur usernames must match [0-9a-z_.@+-]+. SCIM userNames may include
    other characters and uppercase — we lowercase and strip incompatible
    characters rather than rejecting, so common IdP values still flow through.
    """
    import re as _re

    cleaned = "".join(_re.findall(r"[0-9a-z_.@+\-]+", raw.lower()))
    if not cleaned:
        raise ScimError(
            400,
            f"userName {raw!r} contains no characters valid for a Waldur username.",
            scim_type="invalidValue",
        )
    return cleaned


@transaction.atomic
def _create_user(body: dict, request) -> User:
    raw_username = body.get("userName")
    if not raw_username:
        raise ScimError(400, "userName is required.", scim_type="invalidValue")
    username = _normalize_username(raw_username)

    if User.all_objects.filter(username=username).exists():
        raise ScimError(
            409,
            f"User with userName {username!r} already exists.",
            scim_type="uniqueness",
        )

    active = body.get("active")
    if isinstance(active, str):
        active = active.lower() == "true"
    user = User.objects.create(
        username=username,
        is_active=True if active is None else bool(active),
    )
    user.set_unusable_password()

    payload = scim_to_waldur_payload(body)
    update_user_attributes_from_source(
        user, payload, source=_source(), allowed_fields=_allowed_fields()
    )

    external_id = body.get("externalId")
    if external_id:
        set_scim_external_id(user, str(external_id), source=_source())
        user.save(update_fields=["attribute_sources"])

    user.refresh_from_db()
    return user


def _update_user(user: User, body: dict, *, full_replace: bool) -> User:
    """Apply a PUT-style full replace to an existing user.

    Mutability rules:
    - ``userName`` is immutable post-creation; attempting to change it returns 400.
    - ``active=false`` triggers ``remove_user_from_isd``.
    """
    submitted = body.get("userName")
    if submitted is not None:
        normalized = _normalize_username(submitted)
        if normalized != user.username:
            raise ScimError(
                400,
                "Changing 'userName' after creation is not supported.",
                scim_type="mutability",
            )

    payload = scim_to_waldur_payload(body)
    update_user_attributes_from_source(
        user, payload, source=_source(), allowed_fields=_allowed_fields()
    )

    if "externalId" in body:
        external_id = body["externalId"]
        if external_id:
            set_scim_external_id(user, str(external_id), source=_source())
            user.save(update_fields=["attribute_sources"])

    active = body.get("active")
    if isinstance(active, str):
        active = active.lower() == "true"
    if active is False:
        remove_user_from_isd(user, source=_source())
    elif active is True and not user.is_active:
        # Reactivation: clear deactivation reason and flip flag.
        user.is_active = True
        user.deactivation_reason = ""
        user._change_source = _source()
        user.save(update_fields=["is_active", "deactivation_reason"])

    user.refresh_from_db()
    return user


@extend_schema(exclude=True)
class UsersListView(_UsersBaseView):
    """``/scim/v2/Users`` — list + create."""

    def get(self, request):
        # all_objects: deactivated users must remain visible to the IdM
        # (active=false filtering, reactivation) — see RFC 7644 §3.4.2.
        qs = User.all_objects.all().order_by("id")
        filter_expr = request.query_params.get("filter")
        if filter_expr:
            qs = qs.filter(parse_filter(filter_expr, USER_FILTER_FIELDS))

        total = qs.count()
        start, count = parse_pagination(request)
        page = list(qs[start - 1 : start - 1 + count])
        return Response(
            list_response(
                [_serialize(user, request) for user in page],
                total=total,
                start_index=start,
                count=count,
            )
        )

    def post(self, request):
        body = request.data
        if not isinstance(body, dict):
            raise ScimError(400, "Request body must be a JSON object.")
        existing = _find_user(body)
        if existing:
            raise ScimError(
                409,
                f"User already exists (uuid={existing.uuid.hex}).",
                scim_type="uniqueness",
            )
        user = _create_user(body, request)
        response = Response(_serialize(user, request), status=status.HTTP_201_CREATED)
        response["Location"] = _user_location(request, user)
        return response


def _get_user_or_404(uuid_hex: str) -> User:
    try:
        # all_objects: a deactivated user is still a SCIM resource — the IdM
        # reads it as active=false and may PATCH it back to active=true.
        return User.all_objects.get(uuid=uuid_hex)
    except (User.DoesNotExist, ValueError):
        raise ScimError(404, f"User {uuid_hex!r} not found.")


@extend_schema(exclude=True)
class UserDetailView(_UsersBaseView):
    """``/scim/v2/Users/<uuid>`` — read / replace / patch / delete."""

    def get(self, request, uuid_hex):
        user = _get_user_or_404(uuid_hex)
        return Response(_serialize(user, request))

    def put(self, request, uuid_hex):
        user = _get_user_or_404(uuid_hex)
        body = request.data
        if not isinstance(body, dict):
            raise ScimError(400, "Request body must be a JSON object.")
        with transaction.atomic():
            user = _update_user(user, body, full_replace=True)
        return Response(_serialize(user, request))

    def patch(self, request, uuid_hex):
        user = _get_user_or_404(uuid_hex)
        operations = validate_patch_envelope(request.data or {})
        patch_result = apply_user_patch(operations)
        with transaction.atomic():
            if patch_result.attributes:
                update_user_attributes_from_source(
                    user,
                    patch_result.attributes,
                    source=_source(),
                    allowed_fields=_allowed_fields(),
                )
            if patch_result.set_external_id is not None:
                if patch_result.set_external_id:
                    set_scim_external_id(
                        user, patch_result.set_external_id, source=_source()
                    )
                else:
                    sources = dict(user.attribute_sources or {})
                    sources.pop("externalId", None)
                    user.attribute_sources = sources
                user.save(update_fields=["attribute_sources"])
            if patch_result.set_active is not None:
                if patch_result.set_active is False:
                    remove_user_from_isd(user, source=_source())
                else:
                    if not user.is_active:
                        user.is_active = True
                        user.deactivation_reason = ""
                        user._change_source = _source()
                        user.save(update_fields=["is_active", "deactivation_reason"])
        user.refresh_from_db()
        return Response(_serialize(user, request))

    def delete(self, request, uuid_hex):
        user = _get_user_or_404(uuid_hex)
        # Waldur uses soft-delete: route through remove_user_from_isd so the
        # deactivation policy applies and attribute provenance is preserved.
        with transaction.atomic():
            remove_user_from_isd(user, source=_source())
        return Response(status=status.HTTP_204_NO_CONTENT)
