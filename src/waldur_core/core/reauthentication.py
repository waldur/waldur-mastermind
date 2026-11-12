"""Step-up re-authentication before sensitive account changes.

Every user has one API token, and every sign-in returns the same key. So
nothing attached to the user or to the token -- ``last_login``, the token's
age -- tells the person who has just signed in from somebody holding a copy
of the token. The proof is instead a new secret, a grant, returned only to
whoever has just proved again that they can sign in. A guarded action needs
one, sent in the ``X-Reauthentication-Grant`` header; without it the request
gets a 401 challenge, modelled on RFC 9470, naming the methods the user can
re-authenticate with.

A grant is bound to the user who obtained it and to one action, is
single-use, and expires after ``REAUTHENTICATION_GRANT_LIFETIME_SECONDS``.
Staff acting on another user's account re-authenticate as themselves: the
grant always belongs to the user making the request.

Methods live with the sign-in they repeat: the password endpoint next to the
password login, the passkey ones next to the passkey ceremonies. A new method
adds a ``ReauthenticationMethod``, an endpoint that calls ``issue_grant``, and
an entry in ``METHODS`` saying which users can use it.
"""

from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiParameter
from rest_framework import exceptions, status
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.configuration import config
from waldur_core.core.auth_utils import is_delegated
from waldur_core.core.enums import ReauthenticationAction, ReauthenticationMethod
from waldur_core.core.exceptions import ThrottledWithRetryAfter
from waldur_core.core.models import ReauthenticationGrant, User
from waldur_core.core.serializers import (
    ReauthenticationChallengeSerializer,
    ReauthenticationThrottledSerializer,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.passkeys import policy as passkey_policy
from waldur_core.passkeys import services as passkey_services
from waldur_core.passkeys.models import PasskeyCredential

GRANT_HEADER = "X-Reauthentication-Grant"
GRANT_META_KEY = "HTTP_X_REAUTHENTICATION_GRANT"
CHALLENGE_CODE = "reauthentication_required"
# RFC 9470 names the error; the scheme is the one the API token uses.
CHALLENGE_AUTH_HEADER = 'Token error="insufficient_user_authentication"'


# For the schema of a guarded endpoint: the header it takes, and its 401.
GRANT_PARAMETER = OpenApiParameter(
    GRANT_HEADER,
    type=str,
    location=OpenApiParameter.HEADER,
    required=False,
    description=(
        "A re-authentication grant for this change, from "
        "/api/reauthentication/password/ or /api/reauthentication/passkey/. "
        "Without a valid one, a change that needs it is refused with a 401 "
        "challenge."
    ),
)
CHALLENGE_RESPONSES = {401: ReauthenticationChallengeSerializer}
# For the schema of a grant endpoint.
THROTTLED_RESPONSES = {429: ReauthenticationThrottledSerializer}

THROTTLE_SCOPE = "reauthentication"


def _password_sign_in_needs_a_passkey(user: User) -> bool:
    # The second factor the password login asks for, and the passkey a staff
    # member under enforcement has but has not used. Re-authenticating with the
    # password alone would be less than such a user's sign-in; a grant to
    # register a passkey would then remove the second factor for good.
    if passkey_services.user_requires_mfa(user):
        return True
    # Only a passkey that can be asserted here counts, as for the passkey
    # method: one registered under another RP ID leaves the password as all
    # this user can sign in with.
    return (
        passkey_policy.is_enforced_for(user)
        and PasskeyCredential.objects.filter(
            user_id=user.pk, is_active=True, rp_id=passkey_policy.get_rp_id()
        ).exists()
    )


def _can_use_password(user: User) -> bool:
    # The same conditions the password login checks: local sign-in is
    # enabled, the account has a password it could sign in with, and the
    # password alone completes that sign-in.
    methods = settings.WALDUR_CORE.get("AUTHENTICATION_METHODS") or []
    return (
        "LOCAL_SIGNIN" in methods
        and user.has_usable_password()
        and not _password_sign_in_needs_a_passkey(user)
    )


def _can_use_passkey(user: User) -> bool:
    # A credential registered under another RP ID cannot be asserted here.
    return (
        passkey_policy.is_enabled()
        and PasskeyCredential.objects.filter(
            user_id=user.pk, is_active=True, rp_id=passkey_policy.get_rp_id()
        ).exists()
    )


# Each method, with whether a user can re-authenticate with it.
METHODS: dict[str, Callable[[User], bool]] = {
    ReauthenticationMethod.PASSWORD: _can_use_password,
    ReauthenticationMethod.PASSKEY: _can_use_passkey,
}


def available_methods(user: User) -> list[str]:
    return [str(method) for method, usable in METHODS.items() if usable(user)]


def is_method_available(user: User, method: str) -> bool:
    return METHODS[method](user)


def grant_lifetime() -> timedelta:
    return timedelta(seconds=config.REAUTHENTICATION_GRANT_LIFETIME_SECONDS)


def is_impersonated(user) -> bool:
    # ImpersonationAuthentication sets the impersonator on the user it returns.
    return getattr(user, "impersonator", None) is not None


class ReauthenticationRequired(exceptions.APIException):
    """The step-up challenge.

    Not ``NotAuthenticated``: DRF turns that into a 403 for views whose
    authenticator has no header of its own, and this one has its own header.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = CHALLENGE_CODE
    auth_header = CHALLENGE_AUTH_HEADER

    def __init__(self, action, methods):
        if methods:
            detail = _("Confirm your identity to make this change.")
        else:
            detail = _(
                "This change requires you to confirm your identity, which "
                "your account cannot do. Please contact support."
            )
        super().__init__(
            {
                "detail": detail,
                "code": CHALLENGE_CODE,
                "action": str(action),
                "methods": list(methods),
            }
        )


class ReauthenticationThrottled(ThrottledWithRetryAfter):
    """A 429 from an endpoint that issues grants."""


class ThrottledGrantEndpointMixin:
    """The per-user rate limit of the endpoints that issue grants."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = THROTTLE_SCOPE

    def throttled(self, request, wait):
        if wait is None:
            # DRF cannot always tell how long is left; the whole period is the
            # most it can be.
            _requests, wait = ScopedRateThrottle().parse_rate(
                ScopedRateThrottle.THROTTLE_RATES[THROTTLE_SCOPE]
            )
        raise ReauthenticationThrottled(wait)


def _event_context(user, action, **extra):
    return {"affected_user": user, "reauthentication_action": str(action), **extra}


def emit_granted(user, action, method):
    event_logger.emit(
        "User {affected_user_username} confirmed their identity with "
        "{reauthentication_method} for {reauthentication_action}.",
        event_type=EventType.REAUTHENTICATION_GRANTED,
        event_context=_event_context(user, action, reauthentication_method=str(method)),
        scopes=[user],
    )


def emit_failed(user, action, method):
    event_logger.emit(
        "User {affected_user_username} failed to confirm their identity with "
        "{reauthentication_method} for {reauthentication_action}.",
        event_type=EventType.REAUTHENTICATION_FAILED,
        event_context=_event_context(user, action, reauthentication_method=str(method)),
        scopes=[user],
    )


def emit_refused(user, action, reason):
    event_logger.emit(
        "Action {reauthentication_action} was refused for user "
        "{affected_user_username}: "
        "{reauthentication_refusal}.",
        event_type=EventType.REAUTHENTICATION_REFUSED,
        event_context=_event_context(user, action, reauthentication_refusal=reason),
        scopes=[user],
    )


def emit_used(user, action, method):
    event_logger.emit(
        "User {affected_user_username} used a re-authentication grant for "
        "{reauthentication_action}.",
        event_type=EventType.REAUTHENTICATION_GRANT_USED,
        event_context=_event_context(user, action, reauthentication_method=str(method)),
        scopes=[user],
    )


def emit_waived(user, action):
    event_logger.emit(
        "User {affected_user_username} made {reauthentication_action} without "
        "re-authentication: they have no method to confirm their identity with.",
        event_type=EventType.REAUTHENTICATION_WAIVED,
        event_context=_event_context(user, action),
        scopes=[user],
    )


def refuse_delegated(request, action):
    """Refuse a guarded action, or a grant, to a request that does not come
    from the user's own sign-in.

    A staff member impersonating the user would prove who they are, not the
    user, so there is nothing they could do to pass. A personal access token
    or an OIDC access token acts for the user in a script or for another
    application, and must not reach these settings at all. 403 rather than
    the challenge.
    """
    if is_impersonated(request.user):
        emit_refused(request.user, action, "the request impersonates the user")
        raise exceptions.PermissionDenied(
            _("This change cannot be made while impersonating a user.")
        )
    if is_delegated(request):
        emit_refused(request.user, action, "the request uses a delegated credential")
        raise exceptions.PermissionDenied(
            _(
                "This change cannot be made with a personal access token or "
                "another application's token. Sign in to make it."
            )
        )


def issue_grant(user, action, method):
    """Record a re-authentication and return the grant and its secret."""
    grant, secret = ReauthenticationGrant.issue(user, action, method, grant_lifetime())
    emit_granted(user, action, method)
    return grant, secret


def _take_grant(user, action, secret):
    if not secret:
        return None
    # The conditions are part of the locking query, so of two requests with
    # the same grant the second one, once the first commits, finds no row.
    grant = (
        ReauthenticationGrant.objects.select_for_update()
        .filter(
            user_id=user.pk,
            action=action,
            secret_hash=ReauthenticationGrant.hash_secret(secret),
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if grant is not None:
        grant.used_at = timezone.now()
        grant.save(update_fields=["used_at"])
    return grant


# The actions a user with no method to re-authenticate with may still make, on
# their own account and from their own sign-in, until sign-in through an
# identity provider can re-authenticate. Changing the email still needs the
# code sent to the new address.
WAIVABLE_ACTIONS = (ReauthenticationAction.CHANGE_EMAIL,)


def _signs_in_only_through_an_identity_provider(user: User) -> bool:
    """Whether the account never had a local credential to re-authenticate with.

    The waiver is for such accounts only. One that has no method because its
    password was removed, or because its passkeys cannot be asserted here, is
    not waived: a token holder could otherwise take a method away and then
    pass without one.
    """
    return (
        user.registration_method not in ("", "default")
        and not user.has_usable_password()
        and not PasskeyCredential.objects.filter(user_id=user.pk).exists()
    )


@contextmanager
def guarded(
    request, action: ReauthenticationAction, waive_without_methods: bool = False
):
    """Run the body of a guarded action, using up the grant the request carries.

    The grant is locked and marked used in the transaction the body runs in,
    so an action that fails leaves it unused. Without a valid grant the body
    does not run: the request gets the challenge, and the refusal is recorded
    outside that transaction so that it stays recorded.

    ``waive_without_methods``, for the caller's own account: a user who has
    no method at all, making one of ``WAIVABLE_ACTIONS``, goes through without
    a grant, and that is recorded. Delegated and impersonated requests are
    refused before this applies.
    """
    user = request.user
    refuse_delegated(request, action)
    secret = request.META.get(GRANT_META_KEY, "")
    with transaction.atomic():
        grant = _take_grant(user, action, secret)
        if grant is not None:
            yield grant
            emit_used(user, action, grant.method)
            return
    methods = available_methods(user)
    if (
        not methods
        and waive_without_methods
        and action in WAIVABLE_ACTIONS
        and _signs_in_only_through_an_identity_provider(user)
    ):
        with transaction.atomic():
            yield None
            emit_waived(user, action)
        return
    if not methods:
        reason = "the user has no method to confirm their identity with"
    elif secret:
        reason = "the re-authentication grant is not valid for it"
    else:
        reason = "no re-authentication grant was given"
    emit_refused(user, action, reason)
    raise ReauthenticationRequired(action, methods)
