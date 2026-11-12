# The pysaml2 client/metadata/config machinery (djangosaml2.conf, saml2.client,
# saml2.metadata, saml2.response, djangosaml2.utils/views and waldur_auth_saml2.utils)
# transitively imports `xmlschema`, a ~13 MB XSD validator, at module load. This
# module is pulled in during URLconf resolution in *every* process (API and Celery),
# so those imports are deferred into the request-handling methods below to keep the
# validator out of startup memory. See the "Lazy imports for heavy optional backends"
# section of CLAUDE.md.
import base64
import hashlib
import ipaddress
import logging
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import auth
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djangosaml2.cache import IdentityCache, OutstandingQueriesCache, StateCache
from djangosaml2.signals import post_authenticated
from rest_framework.authtoken.models import Token
from rest_framework.generics import GenericAPIView, ListAPIView
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT, md
from saml2.xmldsig import DIGEST_SHA1, SIG_RSA_SHA1
from six import text_type

from waldur_core.core import access
from waldur_core.core.authentication import (
    AuthenticationMethod,
    refresh_token,
    set_authentication_method,
)
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.views import (
    login_completed,
    login_failed,
    logout_completed,
    logout_failed,
    validate_authentication_method,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from . import filters, models, serializers

logger = logging.getLogger(__name__)

validate_saml2 = validate_authentication_method("SAML2")

# pysaml2 accepts a request issued up to a day either side of now, so a request
# seen once is remembered for longer than that.
LOGOUT_REQUEST_REPLAY_WINDOW = 3 * 24 * 60 * 60


def metadata(request, config_loader_path=None, valid_for=None):
    """Returns an XML with the SAML 2.0 metadata for this
    SP as configured in the settings.py file.
    """
    from djangosaml2.conf import get_config
    from saml2.metadata import do_extensions, entity_descriptor

    conf = get_config(config_loader_path, request)
    metadata = entity_descriptor(conf)
    if conf.extensions:
        if metadata.extensions is None:
            metadata.extensions = md.Extensions()

        for key, val in conf.extensions.items():
            _ext = do_extensions(key, val)
            if _ext:
                for _e in _ext:
                    metadata.extensions.add_extension_element(_e)

    return HttpResponse(
        content=text_type(metadata).encode("utf-8"),
        content_type="text/xml; charset=utf8",
    )


HOST_SOURCE = re.compile(r"[A-Za-z0-9.-]+")

# pysaml2 submits its HTTP-POST binding form from <body onload=...>.
POST_BINDING_SUBMIT = "document.forms[0].submit()"
POST_BINDING_SUBMIT_HASH = base64.b64encode(
    hashlib.sha256(POST_BINDING_SUBMIT.encode()).digest()
).decode()


def _plain_http_origin(url):
    """The origin of a plain-http URL as a CSP source, or None if it is not one
    that can be written as a source."""
    try:
        target = urlsplit(url)
        port = target.port
    except ValueError:
        return None
    if target.scheme != "http" or not target.hostname:
        return None
    host = target.hostname
    if ":" in host:
        try:
            host = f"[{ipaddress.IPv6Address(host)}]"
        except ValueError:
            return None
    elif not HOST_SOURCE.fullmatch(host):
        return None
    return f"http://{host}:{port}" if port else f"http://{host}"


def post_binding_policy(http_info):
    """The Content-Security-Policy of a page that posts a SAML message.

    pysaml2 builds the page and submits its form from an inline event handler.
    Only that handler may run: the form's target comes from the identity
    provider's metadata and is not escaped, so a handler injected through it
    must stay blocked. The form may post to any https origin, since Chrome
    also applies form-action to the redirects an identity provider answers
    with during a logout, which may lead to other origins. A plain-http
    target, as test identity providers use, is allowed by its exact origin.
    """
    form_action = ["https:"]
    plain_http_origin = _plain_http_origin(http_info.get("url") or "")
    if plain_http_origin:
        form_action.append(plain_http_origin)
    return (
        "default-src 'none'; "
        f"script-src 'unsafe-hashes' 'sha256-{POST_BINDING_SUBMIT_HASH}'; "
        f"form-action {' '.join(form_action)}; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


class BaseSaml2View(GenericAPIView):
    throttle_classes = ()
    permission_classes = ()
    authentication_classes = ()
    # The SAML2 sign-in and logout, for anonymous callers; they sign nobody in
    # as anybody but the subject of a SAML message. A login signs in the user
    # of a response the identity provider signed, into the session of the
    # browser that posts it. With ALLOW_UNSOLICITED (the default) that need
    # not be the browser that started the login, so another site can post a
    # signed response of its own account and sign a visitor into it (#1082).
    # A logout ends the browser session only for a SAML exchange that concerns
    # it: our own request answered, or a request the identity provider of the
    # session signed for its subject, accepted once (#1071).
    access_declarations = {"get": [access.public()], "post": [access.public()]}


class Saml2LoginView(BaseSaml2View):
    """
    SAML Authorization endpoint

    This view receives authorization requests from users and
    redirects them to corresponding IdP authorization page.
    The "metadata" has to be set in SAML_CONFIG in settings.py
    """

    serializer_class = serializers.Saml2LoginSerializer

    @validate_saml2
    def post(self, request):
        from djangosaml2.conf import get_config
        from djangosaml2.utils import get_location
        from saml2.client import Saml2Client

        from . import utils

        if not self.request.user.is_anonymous:
            error_message = _("This endpoint is for anonymous users only.")
            return JsonResponse({"error_message": error_message}, status=400)

        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        idp = serializer.validated_data.get("idp")

        conf = get_config(request=request)

        # ensure our selected binding is supported by the IDP
        supported_bindings = utils.get_idp_sso_supported_bindings(idp, config=conf)
        default_binding = settings.WALDUR_AUTH_SAML2.get("DEFAULT_BINDING")

        if default_binding in supported_bindings:
            binding = default_binding
        elif BINDING_HTTP_POST in supported_bindings:
            binding = BINDING_HTTP_POST
        elif BINDING_HTTP_REDIRECT in supported_bindings:
            binding = BINDING_HTTP_REDIRECT
        else:
            error_message = _("Identity provider does not support available bindings.")
            return JsonResponse({"error_message": error_message}, status=400)

        client = Saml2Client(conf)

        kwargs = {}
        sign_requests = getattr(conf, "_sp_authn_requests_signed", False)
        if sign_requests:
            signature_algorithm = (
                settings.WALDUR_AUTH_SAML2.get("SIGNATURE_ALGORITHM") or SIG_RSA_SHA1
            )
            digest_algorithm = (
                settings.WALDUR_AUTH_SAML2.get("DIGEST_ALGORITHM") or DIGEST_SHA1
            )

            kwargs["sign"] = True
            kwargs["sigalg"] = signature_algorithm
            kwargs["digest_alg"] = digest_algorithm

        nameid_format = settings.WALDUR_AUTH_SAML2.get("NAMEID_FORMAT")
        if nameid_format or nameid_format == "":  # "" is a valid setting in pysaml2
            kwargs["nameid_format"] = nameid_format

        if binding == BINDING_HTTP_REDIRECT:
            session_id, result = client.prepare_for_authenticate(
                entityid=idp, binding=binding, **kwargs
            )

            data = {
                "binding": "redirect",
                "url": get_location(result),
            }
        elif binding == BINDING_HTTP_POST:
            try:
                location = client.sso_location(idp, binding)
            except TypeError:
                error_message = _("Invalid identity provider specified.")
                return JsonResponse({"error_message": error_message}, status=400)

            session_id, request_xml = client.create_authn_request(
                location, binding=binding, **kwargs
            )
            data = {
                "binding": "post",
                "url": location,
                "request": str(base64.b64encode(request_xml.encode("UTF-8")), "utf-8"),
            }

        # save session_id
        oq_cache = OutstandingQueriesCache(request.session)
        oq_cache.set(session_id, "")

        return JsonResponse(data)


class Saml2LoginCompleteView(BaseSaml2View):
    """
    SAML Authorization Response endpoint

    The IdP will send its response to this view, which
    will process it with pysaml2 help and log the user
    in using the custom Authorization backend
    djangosaml2.backends.Saml2Backend that should be
    enabled in the settings.py
    """

    serializer_class = serializers.Saml2LoginCompleteSerializer

    def login_failed(self, reason):
        event_logger.emit_failed_login(
            "SAML2 authentication failed: {reason}",
            event_type=EventType.AUTH_LOGIN_FAILED_WITH_SAML2,
            event_context={"reason": str(reason)},
            identity=str(reason),
        )
        return login_failed(reason)

    @validate_saml2
    def post(self, request):
        from djangosaml2.conf import get_config
        from djangosaml2.utils import get_custom_setting
        from djangosaml2.views import _set_subject_id
        from saml2.client import Saml2Client
        from saml2.response import StatusRequestDenied

        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        attribute_mapping = get_custom_setting(
            "SAML_ATTRIBUTE_MAPPING",
            {
                "uid": ("username",),
                "eduPersonScopedAffiliation": ("_process_saml2_affiliations",),
            },
        )
        create_unknown_user = get_custom_setting("SAML_CREATE_UNKNOWN_USER", True)

        conf = get_config(request=request)
        client = Saml2Client(conf, identity_cache=IdentityCache(request.session))

        oq_cache = OutstandingQueriesCache(request.session)
        outstanding_queries = oq_cache.outstanding_queries()

        xmlstr = serializer.validated_data["SAMLResponse"]

        # process the authentication response
        try:
            response = client.parse_authn_request_response(
                xmlstr, BINDING_HTTP_POST, outstanding_queries
            )
        except StatusRequestDenied:
            return self.login_failed(
                _(
                    "Authentication request has been denied by identity provider. "
                    "Please check your credentials."
                )
            )
        except Exception as e:
            logger.error("SAML response parsing failed %s", e)
            return self.login_failed(_("SAML2 response has errors."))

        if response is None:
            logger.error("SAML response is None")
            return self.login_failed(
                _("SAML response has errors. Please check the logs")
            )

        if response.assertion is None:
            logger.error("SAML response assertion is None")
            return self.login_failed(
                _("SAML response has errors. Please check the logs")
            )

        session_id = response.session_id()
        oq_cache.delete(session_id)

        # authenticate the remote user
        session_info = response.session_info()

        if callable(attribute_mapping):
            attribute_mapping = attribute_mapping()
        if callable(create_unknown_user):
            create_unknown_user = create_unknown_user()

        try:
            user = auth.authenticate(
                request=request,  # AxesBackend requires request for authentication
                session_info=session_info,
                attribute_mapping=attribute_mapping,
                create_unknown_user=create_unknown_user,
            )
        except ValidationError as e:
            return self.login_failed(e.message)
        if user is None:
            return self.login_failed(_("SAML2 authentication failed."))

        registration_method = settings.WALDUR_AUTH_SAML2.get("NAME", "saml2")
        if user.registration_method != registration_method:
            user.registration_method = registration_method
            user.save(update_fields=["registration_method"])

        # required for validating SAML2 logout requests
        auth.login(request, user)
        _set_subject_id(request.session, session_info["name_id"])
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        logger.debug("User %s authenticated via SSO.", user)

        logger.debug("Sending the post_authenticated signal")
        post_authenticated.send_robust(sender=user, session_info=session_info)
        token = refresh_token(user)

        logger.info(
            "Authenticated with SAML token. Returning token for successful login of user %s",
            user,
        )
        event_logger.emit(
            "User {user_username} with full name {user_full_name} logged in successfully with SAML2.",
            event_type=EventType.AUTH_LOGGED_IN_WITH_SAML2,
            event_context={"user": user, "request": request},
            scopes=[user],
        )
        set_authentication_method(request, AuthenticationMethod.SAML2, user)
        return login_completed(token, "saml2")


class Saml2LogoutView(BaseSaml2View):
    """
    SAML Logout endpoint

    This view redirects users to corresponding IdP page for the logout.
    """

    serializer_class = EmptySerializer

    @validate_saml2
    def get(self, request):
        from djangosaml2.conf import get_config
        from djangosaml2.utils import get_location
        from djangosaml2.views import _get_subject_id
        from saml2.client import Saml2Client

        state = StateCache(request.session)
        conf = get_config(request=request)

        client = Saml2Client(
            conf, state_cache=state, identity_cache=IdentityCache(request.session)
        )
        subject_id = _get_subject_id(request.session)
        if subject_id is None:
            return logout_failed(_("Remote SAML2 logout has failed."))

        # The portal's logout (POST /api-auth/logout/) ends the session before
        # it hands out this URL. A browser still logged in did not come from
        # there: a link on another site must not log it out of the IdP.
        if not auth.get_user(request).is_anonymous:
            return logout_failed(_("Remote SAML2 logout has failed."))

        try:
            result = client.global_logout(subject_id)
        except KeyError:
            return logout_failed(_("You are not logged in any IdP/AA."))

        state.sync()
        if not result:
            return logout_failed(_("You are not logged in any IdP/AA."))

        # Logout is supported only from 1 IdP
        binding, http_info = list(result.values())[0]
        return HttpResponseRedirect(get_location(http_info))


class Saml2LogoutCompleteView(BaseSaml2View):
    """
    SAML Logout Response endpoint

    The IdP sends its logout response, or its own logout request, to this
    view, which ends the browser session. A signed logout request from the
    IdP also revokes the user's API token; for a logout started in the
    portal, the portal's logout has already revoked it.
    """

    serializer_class = serializers.Saml2LogoutCompleteSerializer

    @validate_saml2
    def get(self, request):
        """
        For IdPs which send GET requests
        """
        serializer = self.serializer_class(data=request.GET)
        serializer.is_valid(raise_exception=True)
        return self.logout(request, serializer.validated_data, BINDING_HTTP_REDIRECT)

    @validate_saml2
    def post(self, request):
        """
        For IdPs which send POST requests
        """
        serializer = self.serializer_class(data=request.POST)
        serializer.is_valid(raise_exception=True)
        return self.logout(request, serializer.validated_data, BINDING_HTTP_POST)

    def logout(self, request, data, binding):
        from djangosaml2.conf import get_config
        from djangosaml2.utils import get_location
        from djangosaml2.views import _get_subject_id
        from saml2.client import Saml2Client

        conf = get_config(request=request)
        # pysaml2 requires a signed request only when the IdP role wants
        # signed authentication requests; that flag applies to every request
        # it parses, logout requests included. Set on this configuration only,
        # it does not reach the metadata, which describes the SP role.
        signatures_required = settings.WALDUR_AUTH_SAML2.get(
            "WANT_LOGOUT_REQUESTS_SIGNED", True
        )
        conf.setattr("idp", "want_authn_requests_signed", signatures_required)

        state = StateCache(request.session)
        client = Saml2Client(
            conf, state_cache=state, identity_cache=IdentityCache(request.session)
        )
        # What the redirect binding signs besides the request itself.
        signed_with = {
            "relay_state": data.get("RelayState"),
            "sigalg": data.get("SigAlg"),
            "signature": data.get("Signature"),
        }

        # The session is ended only for a SAML exchange that concerns it: a link
        # to this endpoint must not be enough to log anybody out.
        confirmed = False
        revoke_tokens = False
        subject_id = _get_subject_id(request.session)
        try:
            if "SAMLResponse" in data:
                # Logout started by us: the answer must be to the request we sent.
                logout_response = client.parse_logout_request_response(
                    data["SAMLResponse"], binding
                )
                confirmed = (
                    subject_id is not None
                    and logout_response is not None
                    and logout_response.in_response_to in state
                )
                http_response = logout_completed()
            elif subject_id is None:
                # Logout started by IdP, for a browser without a SAML2 login.
                http_response = logout_completed()
            else:
                # Logout started by IdP: the IdP the session logged in with
                # must ask for it, naming the subject of this session.
                logout_request = client.parse_logout_request(
                    data["SAMLRequest"], binding, **signed_with
                )
                confirmed = (
                    logout_request.message.name_id == subject_id
                    and logout_request.sender()
                    in client.users.issuers_of_info(subject_id)
                    # A request travels in a URL or a form, so it may be seen
                    # by others; it is accepted only once.
                    and cache.add(
                        f"saml2-logout-request:{logout_request.sender()}:"
                        f"{logout_request.message.id}",
                        True,
                        timeout=LOGOUT_REQUEST_REPLAY_WINDOW,
                    )
                )
                if confirmed:
                    # Only a signed request may end what the portal and the
                    # user's scripts are logged in with.
                    revoke_tokens = signatures_required
                    http_info = client.handle_logout_request(
                        data["SAMLRequest"], subject_id, binding, **signed_with
                    )
                    http_response = self.reply(http_info, get_location)
                else:
                    # Not answered: handling it would also drop the SAML
                    # identity of this session, which its own logout needs.
                    http_response = logout_failed(
                        _("SAML2 logout request is not for this session.")
                    )
        except Exception as e:
            logger.warning("SAML2 logout message could not be processed: %s", e)
            if confirmed:
                # The identity provider did ask to log this session out.
                self.end_session(request, revoke_tokens)
            return logout_failed(_("SAML2 logout message has errors."))

        state.sync()
        if confirmed:
            self.end_session(request, revoke_tokens)
        return http_response

    @staticmethod
    def reply(http_info, get_location):
        # The redirect binding answers with a Location; the POST binding with
        # a page that posts the response to the identity provider.
        if dict(http_info["headers"]).get("Location"):
            return HttpResponseRedirect(get_location(http_info))
        response = HttpResponse(http_info["data"], content_type="text/html")
        response["Content-Security-Policy"] = post_binding_policy(http_info)
        return response

    def end_session(self, request, revoke_tokens=False):
        # SAML views authenticate nobody, so request.user is always anonymous;
        # the user is the one of the Django session that the SAML login started.
        user = auth.get_user(request)
        if not user.is_anonymous:
            if revoke_tokens:
                Token.objects.filter(user=user).delete()
            request.user = user  # carried by the user_logged_out signal
        auth.logout(request)
        if not user.is_anonymous:
            event_logger.emit(
                "User {user_username} with full name {user_full_name} logged out successfully with SAML2.",
                event_type=EventType.AUTH_LOGGED_OUT_WITH_SAML2,
                event_context={"user": user},
                scopes=[user],
            )


class Saml2ProviderView(ListAPIView):
    throttle_classes = ()
    permission_classes = ()
    # The login page lists the identity providers to choose from: their name
    # and URL only.
    access_declarations = {"get": [access.public()]}
    serializer_class = serializers.Saml2ProviderSerializer
    queryset = models.IdentityProvider.objects.all()
    filterset_class = filters.IdentityProviderFilter
