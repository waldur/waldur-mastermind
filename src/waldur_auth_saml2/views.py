import base64
import logging

from django.conf import settings
from django.contrib import auth
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djangosaml2.cache import IdentityCache, OutstandingQueriesCache, StateCache
from djangosaml2.conf import get_config
from djangosaml2.signals import post_authenticated
from djangosaml2.utils import get_custom_setting, get_location
from djangosaml2.views import _get_subject_id, _set_subject_id
from rest_framework.authtoken.models import Token
from rest_framework.generics import ListAPIView
from rest_framework.views import APIView
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT, md
from saml2.client import Saml2Client
from saml2.metadata import do_extensions, entity_descriptor
from saml2.response import StatusRequestDenied
from saml2.xmldsig import DIGEST_SHA1, SIG_RSA_SHA1
from six import text_type

from waldur_core.core.views import (
    RefreshTokenMixin,
    login_completed,
    login_failed,
    logout_completed,
    logout_failed,
    validate_authentication_method,
)

from . import filters, models, serializers, utils
from .log import event_logger

logger = logging.getLogger(__name__)

validate_saml2 = validate_authentication_method("SAML2")


def metadata(request, config_loader_path=None, valid_for=None):
    """Returns an XML with the SAML 2.0 metadata for this
    SP as configured in the settings.py file.
    """
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


class BaseSaml2View(APIView):
    throttle_classes = ()
    permission_classes = ()
    authentication_classes = ()


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


class Saml2LoginCompleteView(RefreshTokenMixin, BaseSaml2View):
    """
    SAML Authorization Response endpoint

    The IdP will send its response to this view, which
    will process it with pysaml2 help and log the user
    in using the custom Authorization backend
    djangosaml2.backends.Saml2Backend that should be
    enabled in the settings.py
    """

    serializer_class = serializers.Saml2LoginCompleteSerializer

    @validate_saml2
    def post(self, request):
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
        except Exception as e:
            if isinstance(e, StatusRequestDenied):
                return login_failed(
                    _(
                        "Authentication request has been denied by identity provider. "
                        "Please check your credentials."
                    )
                )
            logger.error("SAML response parsing failed %s" % e)
            return login_failed(_("SAML2 response has errors."))

        if response is None:
            logger.error("SAML response is None")
            return login_failed(_("SAML response has errors. Please check the logs"))

        if response.assertion is None:
            logger.error("SAML response assertion is None")
            return login_failed(_("SAML response has errors. Please check the logs"))

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
            return login_failed(e.message)
        if user is None:
            return login_failed(_("SAML2 authentication failed."))

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
        token = self.refresh_token(user)

        logger.info(
            "Authenticated with SAML token. Returning token for successful login of user %s",
            user,
        )
        event_logger.saml2_auth.info(
            "User {user_username} with full name {user_full_name} logged in successfully with SAML2.",
            event_type="auth_logged_in_with_saml2",
            event_context={"user": user, "request": request},
        )
        return login_completed(token.key, "saml2")


class Saml2LogoutView(BaseSaml2View):
    """
    SAML Logout endpoint

    This view redirects users to corresponding IdP page for the logout.
    """

    @validate_saml2
    def get(self, request):
        state = StateCache(request.session)
        conf = get_config(request=request)

        client = Saml2Client(
            conf, state_cache=state, identity_cache=IdentityCache(request.session)
        )
        subject_id = _get_subject_id(request.session)
        if subject_id is None:
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

    The IdP will send its response to this view, which
    will logout the user and remove authorization token.
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
        conf = get_config(request=request)

        state = StateCache(request.session)
        client = Saml2Client(
            conf, state_cache=state, identity_cache=IdentityCache(request.session)
        )

        if "SAMLResponse" in data:
            # Logout started by us
            client.parse_logout_request_response(data["SAMLResponse"], binding)
            http_response = logout_completed()
        else:
            # Logout started by IdP
            subject_id = _get_subject_id(request.session)
            if subject_id is None:
                http_response = logout_completed()
            else:
                http_info = client.handle_logout_request(
                    data["SAMLRequest"],
                    subject_id,
                    binding,
                    relay_state=data.get("RelayState", ""),
                )
                http_response = HttpResponseRedirect(get_location(http_info))

        state.sync()
        user = request.user
        if user.is_anonymous:
            return http_response
        Token.objects.get(user=user).delete()
        auth.logout(request)
        event_logger.saml2_auth.info(
            "User {user_username} with full name {user_full_name} logged out successfully with SAML2.",
            event_type="auth_logged_out_with_saml2",
            event_context={"user": user},
        )
        return http_response


class Saml2ProviderView(ListAPIView):
    throttle_classes = ()
    permission_classes = ()
    serializer_class = serializers.Saml2ProviderSerializer
    queryset = models.IdentityProvider.objects.all()
    filterset_class = filters.IdentityProviderFilter
