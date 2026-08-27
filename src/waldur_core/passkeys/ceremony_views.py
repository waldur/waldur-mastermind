"""The six ceremony endpoints.

Three pairs, each begin/finish:

* registration — authenticated, enrols a new credential
* sign-in      — anonymous, usernameless, issues a token on success
* second factor — anonymous but gated on a pending handle from the login view

The security-relevant ordering lives here: **nothing issues a token, touches
``last_login`` or emits a login event until an assertion has actually
verified**.
"""

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from waldur_core.core.authentication import AuthenticationMethod, refresh_token
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.passkeys import policy, services
from waldur_core.passkeys.enums import CeremonyKind
from waldur_core.passkeys.models import PasskeyCeremony
from waldur_core.passkeys.serializers import (
    PasskeyAssertionFinishSerializer,
    PasskeyCeremonyOptionsSerializer,
    PasskeyCredentialSerializer,
    PasskeyMfaBeginSerializer,
    PasskeyRegistrationFinishSerializer,
    PasskeyTokenSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _client_ip(request):
    return request.META.get("REMOTE_ADDR")


class BasePasskeyView(APIView):
    """Shared gating: passkeys must be enabled, and the flow must be enabled."""

    throttle_classes = [ScopedRateThrottle]
    required_flow = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not self._flow_enabled():
            # 404 rather than 403: an endpoint that is switched off should not
            # advertise that it exists.
            from rest_framework.exceptions import NotFound

            raise NotFound(_("Passkey authentication is not enabled."))

    def _flow_enabled(self):
        if self.required_flow == "signin":
            return policy.is_signin_enabled()
        if self.required_flow == "mfa":
            return policy.is_mfa_enabled()
        return policy.is_enabled()

    def get_ceremony(self, uuid, kind):
        """Fetch a ceremony by handle, or fail the way an unknown one does.

        Deliberately does not distinguish "no such ceremony" from "wrong kind"
        or "already used" — all three are a dead handle to the caller, and
        telling them apart would leak whether a handle was ever real.
        """
        from rest_framework.exceptions import ValidationError

        ceremony = PasskeyCeremony.objects.filter(uuid=uuid, kind=kind).first()
        if ceremony is None or not ceremony.is_usable:
            raise ValidationError(_("This passkey ceremony is no longer valid."))
        return ceremony

    def handle_passkey_error(self, error):
        from rest_framework.exceptions import ValidationError

        raise ValidationError(str(error))


class PasskeyRegistrationBeginView(BasePasskeyView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "passkey_registration"

    @extend_schema(
        summary="Begin passkey registration",
        request=None,
        responses={200: PasskeyCeremonyOptionsSerializer},
    )
    def post(self, request):
        ceremony, options = services.start_registration(request.user)
        return Response({"ceremony": ceremony.uuid, "options": options})


class PasskeyRegistrationFinishView(BasePasskeyView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "passkey_registration"

    @extend_schema(
        summary="Finish passkey registration",
        request=PasskeyRegistrationFinishSerializer,
        responses={201: PasskeyCredentialSerializer},
    )
    def post(self, request):
        serializer = PasskeyRegistrationFinishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ceremony = self.get_ceremony(data["ceremony"], CeremonyKind.REGISTRATION)
        # A registration ceremony belongs to whoever opened it. Without this,
        # one user could finish another's enrolment against their own key.
        if ceremony.user_id != request.user.id:
            self.handle_passkey_error(_("This ceremony belongs to another user."))

        try:
            credential = services.finish_registration(
                ceremony,
                data["credential"],
                data["name"],
                ip_address=_client_ip(request),
            )
        except services.PasskeyError as e:
            self.handle_passkey_error(e)

        event_logger.emit(
            "Passkey {passkey_name} has been registered for user "
            "{affected_user_username}.",
            event_type=EventType.PASSKEY_REGISTERED,
            event_context={
                "affected_user": request.user,
                "passkey_name": credential.name,
            },
            scopes=[request.user],
        )
        return Response(
            PasskeyCredentialSerializer(credential).data,
            status=status.HTTP_201_CREATED,
        )


class PasskeySigninBeginView(BasePasskeyView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "passkey_signin"
    required_flow = "signin"

    @extend_schema(
        summary="Begin passwordless passkey sign-in",
        request=None,
        responses={200: PasskeyCeremonyOptionsSerializer},
    )
    def post(self, request):
        ceremony, options = services.start_signin()
        return Response({"ceremony": ceremony.uuid, "options": options})


class PasskeySigninFinishView(BasePasskeyView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "passkey_signin"
    required_flow = "signin"

    @extend_schema(
        summary="Finish passwordless passkey sign-in",
        request=PasskeyAssertionFinishSerializer,
        responses={200: PasskeyTokenSerializer},
    )
    def post(self, request):
        serializer = PasskeyAssertionFinishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ceremony = self.get_ceremony(data["ceremony"], CeremonyKind.SIGNIN)
        try:
            credential = services.finish_assertion(
                ceremony, data["credential"], ip_address=_client_ip(request)
            )
        except services.PasskeyError as e:
            emit_authentication_failed(None, request)
            self.handle_passkey_error(e)

        user = credential.user
        if not user.is_active:
            return Response(
                data={"detail": _("User account is disabled.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response({"token": issue_token(user, request).key})


class PasskeyMfaBeginView(BasePasskeyView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "passkey_signin"
    required_flow = "mfa"

    @extend_schema(
        summary="Begin the passkey second factor",
        request=PasskeyMfaBeginSerializer,
        responses={200: PasskeyCeremonyOptionsSerializer},
    )
    def post(self, request):
        serializer = PasskeyMfaBeginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ceremony = self.get_ceremony(
            serializer.validated_data["ceremony"], CeremonyKind.MFA
        )
        return Response(
            {"ceremony": ceremony.uuid, "options": services.build_mfa_options(ceremony)}
        )


class PasskeyMfaFinishView(BasePasskeyView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "passkey_signin"
    required_flow = "mfa"

    @extend_schema(
        summary="Finish the passkey second factor",
        request=PasskeyAssertionFinishSerializer,
        responses={200: PasskeyTokenSerializer},
    )
    def post(self, request):
        serializer = PasskeyAssertionFinishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ceremony = self.get_ceremony(data["ceremony"], CeremonyKind.MFA)
        user = ceremony.user
        try:
            services.finish_assertion(
                ceremony, data["credential"], ip_address=_client_ip(request)
            )
        except services.PasskeyError as e:
            emit_authentication_failed(user, request)
            self.handle_passkey_error(e)

        if not user.is_active:
            return Response(
                data={"detail": _("User account is disabled.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response({"token": issue_token(user, request).key})


def issue_token(user, request):
    """Complete a login that a passkey has just satisfied.

    Everything that marks a session as begun happens **here**, after
    verification — never before it. In the password-only path
    ``refresh_token()`` runs immediately after the password check and bumps
    ``token.created``, which means a correct password alone extends the life
    of an existing, possibly attacker-held token. Keeping all of it behind
    verification is what makes "passkey required" a property of the session
    rather than a property of the UI.
    """
    token = refresh_token(user)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    from waldur_core.core.authentication import set_authentication_method

    try:
        set_authentication_method(request, AuthenticationMethod.PASSKEY)
    except AttributeError:
        # The SPA does not carry a session cookie, and helm allows the API and
        # the portal to live on different hostnames, so there may be no session
        # to write to. That is not a failure of the login.
        logger.debug("No session available to record the authentication method.")

    event_logger.emit(
        "User {user_username} with full name {user_full_name} "
        "authenticated successfully with a passkey.",
        event_type=EventType.PASSKEY_AUTHENTICATION_SUCCEEDED,
        event_context={"user": user, "request": request},
        scopes=[user],
    )
    return token


def emit_authentication_failed(user, request):
    context = {"request": request}
    if user is not None:
        context["affected_user"] = user
    event_logger.emit(
        "Passkey authentication failed.",
        event_type=EventType.PASSKEY_AUTHENTICATION_FAILED,
        event_context=context,
        scopes=[user] if user is not None else [],
    )
