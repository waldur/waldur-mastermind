"""Passkey step for the Django admin.

The admin authenticates with its own login form and a Django session, which
no WebAuthn ceremony guards. Rather than closing the admin outright when
enforcement is on, this adds the missing step: password first, then an
assertion, then the session is marked verified.

The flow deliberately reuses Django's own machinery rather than fighting it:

1. the password form logs the user in as usual, so ``request.user`` is set;
2. ``CustomAdminSite.has_permission`` refuses while the session is unverified,
   which makes ``admin_view`` redirect to the admin login URL;
3. ``CustomAdminSite.login`` sees an authenticated-but-unverified user and
   sends them here instead of showing the form again.

That last hop is what stops the "log in, get bounced back to the login page,
try again" loop that an unexplained ``has_permission`` failure would produce.

Verification is recorded on the **session**, not the account — the same
distinction the API side makes. It is separate from
``PasskeyVerifiedSession``, which keys off a DRF token; the admin has no
token, only a cookie.
"""

import json
import logging

from django.contrib import auth
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.passkeys import policy, services
from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import PasskeyCeremony, PasskeyCredential

logger = logging.getLogger(__name__)

SESSION_KEY = "passkey_admin_verified_at"
CEREMONY_SESSION_KEY = "passkey_admin_ceremony"


def is_admin_session_verified(request) -> bool:
    """Whether this admin session has satisfied a passkey."""
    session = getattr(request, "session", None)
    if session is None:
        return False
    return bool(session.get(SESSION_KEY))


def mark_admin_session_verified(request):
    request.session[SESSION_KEY] = timezone.now().isoformat()
    # Django only persists a session it believes changed.
    request.session.modified = True


def user_can_satisfy_passkey(user) -> bool:
    """Whether this account holds a credential it could assert with.

    Someone with none cannot enrol from the admin — enrolment lives in the
    portal — so they are told that rather than shown a prompt that must fail.
    """
    return PasskeyCredential.objects.filter(user=user, is_active=True).exists()


def challenge_view(request):
    """The page that runs the assertion."""
    if not request.user.is_authenticated:
        return HttpResponseRedirect(reverse("admin:login"))

    if not policy.is_enforced_for(request.user):
        return HttpResponseRedirect(reverse("admin:index"))

    return render(
        request,
        "passkeys/admin_passkey.html",
        {
            "title": _("Passkey required"),
            "has_credential": user_can_satisfy_passkey(request.user),
            "next": request.GET.get("next") or reverse("admin:index"),
        },
    )


@require_POST
def options_view(request):
    """Issue assertion options for the signed-in admin user."""
    if not request.user.is_authenticated or not policy.is_enforced_for(request.user):
        return JsonResponse({"detail": "Not applicable."}, status=403)

    if not user_can_satisfy_passkey(request.user):
        return JsonResponse({"detail": "No passkey registered."}, status=400)

    ceremony = services.create_mfa_ceremony(request.user)
    # The handle lives in the session rather than the response body: the admin
    # already has a cookie, so there is no reason to hand the browser a
    # reference it could be tricked into replaying from elsewhere.
    request.session[CEREMONY_SESSION_KEY] = str(ceremony.uuid)
    return JsonResponse({"options": services.build_mfa_options(ceremony)})


@require_POST
def verify_view(request):
    """Verify the assertion and mark the session."""
    if not request.user.is_authenticated or not policy.is_enforced_for(request.user):
        return JsonResponse({"detail": "Not applicable."}, status=403)

    handle = request.session.get(CEREMONY_SESSION_KEY)
    if not handle:
        return JsonResponse({"detail": "No ceremony in progress."}, status=400)

    ceremony = PasskeyCeremony.objects.filter(
        uuid=handle, kind=CeremonyKind.MFA, user=request.user
    ).first()
    if ceremony is None or not ceremony.is_usable:
        return JsonResponse(
            {"detail": "This challenge is no longer valid."}, status=400
        )

    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"detail": "Malformed response."}, status=400)

    try:
        services.finish_assertion(
            ceremony,
            payload.get("credential"),
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except services.PasskeyError as e:
        event_logger.emit(
            "Passkey authentication failed for the admin site.",
            event_type=EventType.PASSKEY_AUTHENTICATION_FAILED,
            event_context={"affected_user": request.user, "request": request},
            scopes=[request.user],
        )
        return JsonResponse({"detail": str(e)}, status=400)

    request.session.pop(CEREMONY_SESSION_KEY, None)

    # Rotate the session key before marking it: the identifier issued for the
    # password half must not be the one that carries the verified flag, or a
    # session fixed before the second factor would inherit it. auth.login()
    # cycles the key, which is why the flag is written afterwards.
    auth.login(
        request,
        request.user,
        backend=request.session.get(auth.BACKEND_SESSION_KEY),
    )
    mark_admin_session_verified(request)

    event_logger.emit(
        "User {user_username} with full name {user_full_name} "
        "satisfied a passkey for the admin site.",
        event_type=EventType.PASSKEY_AUTHENTICATION_SUCCEEDED,
        event_context={"user": request.user, "request": request},
        scopes=[request.user],
    )
    return JsonResponse({"status": "ok"})
