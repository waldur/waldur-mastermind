import functools
import logging
import mimetypes
from typing import Any
from urllib.parse import urlencode

import requests
import reversion
from constance import config
from django.conf import settings
from django.contrib import auth
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.db import connection, connections
from django.db.models import ForeignKey, ProtectedError
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from drf_spectacular.utils import OpenApiExample, extend_schema
from packaging import version
from rest_framework import exceptions, generics, mixins, serializers, status, viewsets
from rest_framework import mixins as rf_mixins
from rest_framework import permissions as rf_permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView
from rest_framework.views import exception_handler as rf_exception_handler

from waldur_auth_social.models import IdentityProvider
from waldur_core import __version__
from waldur_core.core import WaldurExtension, models, permissions
from waldur_core.core.authentication import (
    OIDC_AUTHENTICATION_METHODS,
    AuthenticationMethod,
    get_authentication_method,
    refresh_token,
    set_authentication_method,
)
from waldur_core.core.exceptions import ExtensionDisabled, IncorrectStateException
from waldur_core.core.features import FEATURES
from waldur_core.core.logos import DEFAULT_LOGOS, LOGO_MAP
from waldur_core.core.metadata import WaldurConfiguration
from waldur_core.core.metadata_schemas import (
    EventMetadataResponseSerializer,
    FeatureMetadataResponseSerializer,
    PermissionMetadataResponseSerializer,
    SettingsMetadataResponseSerializer,
)
from waldur_core.core.mixins import ensure_atomic_transaction
from waldur_core.core.serializers import (
    CeleryStatsResponseSerializer,
    ConstanceSettingsSerializer,
    CoreAuthTokenSerializer,
    EmptySerializer,
    LogoutSerializer,
    ObtainAuthTokenSerializer,
    QuerySerializer,
    TableSizeSerializer,
    VersionSerializer,
)
from waldur_core.core.utils import format_homeport_link
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.event_logger import get_event_groups
from waldur_core.permissions.enums import (
    CREATE_PERMISSIONS,
    PERMISSION_DESCRIPTION,
    PermissionEnum,
    RoleEnum,
)
from waldur_core.structure.permissions import IsStaffOrSupportUser

logger = logging.getLogger(__name__)


def validate_authentication_method(method):
    def wrapper(view_func):
        @functools.wraps(view_func)
        def wrapped_view(*args, **kwargs):
            if method not in settings.WALDUR_CORE["AUTHENTICATION_METHODS"]:
                message = (
                    "Authentication method is disabled. "
                    "Please use another authentication method or contact staff."
                )
                return JsonResponse(
                    status=status.HTTP_401_UNAUTHORIZED, data={"detail": message}
                )
            return view_func(*args, **kwargs)

        return wrapped_view

    return wrapper


class ObtainAuthToken(APIView):
    """
    API view loosely based on DRF's default ObtainAuthToken,
    but with the responses formats and status codes aligned with BasicAuthentication behavior.
    """

    throttle_classes = ()
    permission_classes = ()
    serializer_class = ObtainAuthTokenSerializer

    @extend_schema(
        summary="Obtain authentication token",
        description="Authenticates a user with username and password and returns an authentication token.",
        request=ObtainAuthTokenSerializer,
        responses={
            200: CoreAuthTokenSerializer,
            401: None,
        },
        examples=[
            OpenApiExample(
                "Valid request",
                summary="Valid request example",
                description="Example of a valid request to obtain an authentication token.",
                value={"username": "alice", "password": "$ecr3t"},
                request_only=True,
            ),
            OpenApiExample(
                "Success response",
                summary="Success response example",
                description="Example of a successful response with the authentication token.",
                value={"token": "c84d653b9ec92c6cbac41c706593e66f567a7fa4"},
                response_only=True,
                status_codes=[200],
            ),
            OpenApiExample(
                "Field validation failure response",
                summary="Field validation failure response example",
                description="Example of a response when field validation fails.",
                value={"password": ["This field is required."]},
                response_only=True,
                status_codes=[401],
            ),
            OpenApiExample(
                "Invalid credentials failure response",
                summary="Invalid credentials failure response example",
                description="Example of a response when invalid credentials are provided.",
                value={"detail": "Invalid username/password"},
                response_only=True,
                status_codes=[401],
            ),
        ],
    )
    @validate_authentication_method("LOCAL_SIGNIN")
    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data["username"]

        source_ip = request.META.get("REMOTE_ADDR")
        auth_failure_key = f"LOGIN_FAILURES_OF_{username}_AT_{source_ip}"
        auth_failures = cache.get(auth_failure_key) or 0
        lockout_time_in_mins = 10

        if auth_failures >= 4:
            logger.debug(
                "Not returning auth token: "
                f"username {username} from {source_ip} is locked out"
            )
            return Response(
                data={
                    "detail": _("Username is locked out. Try in %s minutes.")
                    % lockout_time_in_mins
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        user = auth.authenticate(
            request=request,
            username=username,
            password=serializer.validated_data["password"],
        )

        if not user:
            logger.debug("Not returning auth token: user %s does not exist", username)
            cache.set(auth_failure_key, auth_failures + 1, lockout_time_in_mins * 60)
            event_logger.emit(
                "User {username} failed to authenticate with username and password.",
                event_type=EventType.AUTH_LOGIN_FAILED_WITH_USERNAME,
                event_context={"username": username},
                scopes=[],
            )

            return Response(
                data={"detail": _("Invalid username/password.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        else:
            cache.delete(auth_failure_key)

        if not user.is_active:
            logger.debug("Not returning auth token: user %s is disabled", username)
            return Response(
                data={"detail": _("User account is disabled.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token = refresh_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        set_authentication_method(request, AuthenticationMethod.LOCAL)

        logger.debug("Returning token for successful login of user %s", user)

        event_logger.emit(
            "User {user_username} with full name {user_full_name} "
            "authenticated successfully with username and password.",
            event_type=EventType.AUTH_LOGGED_IN_WITH_USERNAME,
            event_context={"user": user, "request": request},
            scopes=[user],
        )

        return Response({"token": token.key})


class LogoutView(generics.GenericAPIView):
    permission_classes = (rf_permissions.IsAuthenticated,)
    filter_backends = []

    @extend_schema(
        summary="Log out",
        description="Logs out the current user by deleting their authentication token. If single logout (SLO) is supported for the current authentication method (e.g., SAML2 or OIDC), this endpoint may return a logout URL to which the user should be redirected to complete the logout process on the identity provider side.",
        request=None,
        responses={
            200: LogoutSerializer,
            204: None,
        },
    )
    def post(self, request, format=None):
        request.user.auth_token.delete()
        event_logger.emit(
            "User {user_username} with full name {user_full_name} logged out.",
            event_type=EventType.AUTH_LOGGED_OUT,
            event_context={"user": request.user, "request": request},
            scopes=[request.user],
        )
        logout_url = None
        authentication_method = get_authentication_method(request)
        if authentication_method in OIDC_AUTHENTICATION_METHODS:
            try:
                idp = IdentityProvider.objects.get(provider=authentication_method)
            except IdentityProvider.DoesNotExist:
                pass
            else:
                if idp.logout_url and idp.enable_post_logout_redirect:
                    params = {
                        "post_logout_redirect_uri": format_homeport_link("login/"),
                        "client_id": idp.client_id,
                    }
                    logout_url = f"{idp.logout_url}?{urlencode(params)}"
        elif (
            authentication_method == AuthenticationMethod.SAML2
            and settings.WALDUR_AUTH_SAML2.get("ENABLE_SINGLE_LOGOUT")
        ):
            logout_url = reverse("saml2-logout", request=request)
        if logout_url:
            return Response(status=status.HTTP_200_OK, data={"logout_url": logout_url})
        else:
            return Response(status=status.HTTP_204_NO_CONTENT)


# noinspection PyProtectedMember
def exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        if len(exc.protected_objects) == 1:
            dependent_meta = list(exc.protected_objects)[0]._meta

            try:
                # This exception should be raised from a viewset
                instance_meta = context["view"].get_queryset().model._meta
            except (AttributeError, KeyError):
                # Fallback, when instance being deleted cannot be inferred
                instance_name = "object"
            else:
                instance_name = str(instance_meta.verbose_name)

            detail = _(
                "Cannot delete {instance_name} with existing {dependant_objects}"
            ).format(
                instance_name=instance_name,
                dependant_objects=str(dependent_meta.verbose_name_plural),
            )

            # We substitute exception here to get consistent representation
            # for both ProtectError and manually raised IncorrectStateException
            exc = IncorrectStateException(detail=detail)

    return rf_exception_handler(exc, context)


class ProtectedViewSet(
    rf_mixins.CreateModelMixin,
    rf_mixins.RetrieveModelMixin,
    rf_mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """All default operations except update and delete"""

    pass


class ActionsViewSet(viewsets.ModelViewSet):
    """
    Treats all endpoint actions in the same way.

    1. Allow to define separate serializers for each action:

        def action(self, request, *args, **kwargs):
            serializer = self.get_serializer(...)
            ...

        action_serializer_class = ActionSerializer

    2. Allow to define validators for detail actions:

        def state_is_ok(obj):
            if obj.state != 'ok':
                raise IncorrectStateException('Instance should be in state OK.')

        @decorators.action(detail=True, )
        def action(self, request, *args, **kwargs):
            ...

        action_validators = [state_is_ok]

    3. Allow to define permissions checks for all actions or each action
       separately. Check ActionPermissionsBackend for more details.

    4. To avoid dancing around mixins - allow disabling actions:

        class MyView(ActionsViewSet):
            disabled_actions = ['create']  # error 405 will be returned on POST request
    """

    disabled_actions = []
    permission_classes = (rf_permissions.IsAuthenticated, permissions.ActionsPermission)

    @ensure_atomic_transaction
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_serializer_class(self):
        default_serializer_class = super().get_serializer_class()
        if self.action is None:
            return default_serializer_class
        return getattr(
            self, self.action + "_serializer_class", default_serializer_class
        )

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if (
            self.action is None
        ):  # disable all checks if user tries to reach unsupported action
            return
        # check if action is allowed
        if self.action in getattr(self, "disabled_actions", []):
            raise exceptions.MethodNotAllowed(method=request.method)
        # skip validation for OPTIONS requests
        if self.action == "metadata":
            return
        self.validate_object_action(self.action)
        self.reset_filterset_class(self.action)

    def validate_object_action(self, action_name, obj=None):
        """Execute validation for actions that are related to particular object"""
        action_method = getattr(self, action_name)
        if not getattr(action_method, "detail", False) and action_name not in (
            "update",
            "partial_update",
            "destroy",
        ):
            # DRF does not add flag 'detail' to update and delete actions, however they execute operation with
            # particular object. We need to enable validation for them too.
            return
        validators = getattr(self, action_name + "_validators", [])
        for validator in validators:
            validator(obj or self.get_object())

    def reset_filterset_class(self, action_name):
        action_method = getattr(self, action_name)

        if getattr(action_method, "detail", False):
            self.filterset_class = None


class ReadOnlyActionsViewSet(ActionsViewSet):
    disabled_actions = ["create", "update", "partial_update", "destroy"]


def get_feature_values():
    feature_values = {
        feature.key: feature.value for feature in models.Feature.objects.all()
    }
    return {
        section["key"]: {
            feature["key"]: feature_values.get(
                f"{section['key']}.{feature['key']}", False
            )
            for feature in section["items"]
        }
        for section in FEATURES
    }


def get_constance_plugin_settings(request, plugin, fields):
    plugin_settings = {}
    if request:
        for field in fields:
            if f"{plugin}_{field}" in settings.PUBLIC_CONSTANCE_SETTINGS:
                plugin_settings[field] = getattr(config, f"{plugin}_{field}")
    return plugin_settings


def get_public_settings(request=None):
    cached_settings = cache.get("API_CONFIGURATION")
    if cached_settings:
        return cached_settings
    public_settings = {}

    public_settings["FEATURES"] = get_feature_values()

    try:
        keys = WaldurConfiguration().Meta.public_settings
    except AttributeError:
        pass
    else:
        for s in keys:
            public_settings[s] = getattr(settings, s, None)

    for settings_name, section in WaldurConfiguration().__fields__.items():
        type_ = section.type_
        try:
            keys = type_.Meta.public_settings
        except AttributeError:
            continue
        extension_settings = getattr(settings, settings_name, None)
        if not extension_settings:
            continue
        public_settings[settings_name] = {}
        for s in keys:
            try:
                public_settings[settings_name][s] = extension_settings[s]
            except KeyError:
                pass

    # Processing other extensions
    for ext in WaldurExtension.get_extensions():
        settings_name = [x for x in dir(ext.Settings) if x.startswith("WALDUR_")]
        if not settings_name:
            continue

        settings_name = settings_name[0]
        extension_settings = getattr(settings, settings_name, None)
        if extension_settings and extension_settings.get("ENABLED", True):
            public_settings[settings_name] = {}

            for s in ext.get_public_settings():
                try:
                    public_settings[settings_name][s] = extension_settings[s]
                except KeyError:
                    pass

            for s, v in ext.get_dynamic_settings().items():
                public_settings[settings_name][s] = v

    from constance.admin import get_values

    constance_settings = {}
    if request:
        for key in get_values():
            if key in settings.PUBLIC_CONSTANCE_SETTINGS and not key.startswith(
                "WALDUR_"
            ):
                constance_settings[key] = get_values()[key]
    if public_settings.get("WALDUR_CORE"):
        if request:
            for key, val in LOGO_MAP.items():
                if constance_settings.get(key) or key in DEFAULT_LOGOS:
                    constance_settings[key] = request.build_absolute_uri("/" + val)
        public_settings["WALDUR_CORE"].update(constance_settings)
        provider_name = public_settings["WALDUR_CORE"].get("DEFAULT_IDP")
        if provider_name:
            try:
                provider = IdentityProvider.objects.get(
                    provider=provider_name, is_active=True
                )
            except IdentityProvider.DoesNotExist:
                public_settings["WALDUR_CORE"]["DEFAULT_IDP"] = None
            else:
                public_settings["WALDUR_CORE"]["DEFAULT_IDP"] = {
                    "provider": provider.provider,
                    "client_id": provider.client_id,
                    "auth_url": provider.auth_url,
                }
    public_settings["WALDUR_SUPPORT"] = get_constance_plugin_settings(
        request,
        "WALDUR_SUPPORT",
        ["ENABLED", "DISPLAY_REQUEST_TYPE", "ACTIVE_BACKEND_TYPE"],
    )
    cache.set(
        "API_CONFIGURATION", public_settings, None
    )  # Cache invalidation is handled explicitly
    return public_settings


@extend_schema(
    summary="Get public configuration",
    description="Returns a dictionary of public settings for the Waldur deployment. This includes feature flags, authentication methods, and other configuration details that are safe to expose to any user.",
    request=None,
    responses={status.HTTP_200_OK: dict},
)
@api_view(["GET"])
@permission_classes((rf_permissions.AllowAny,))
def configuration_detail(request):
    return Response(get_public_settings(request))


def _parse_bracket_notation(data):
    """
    Parse bracket notation keys from multipart form data into nested dicts.

    Converts keys like 'LOGIN_LOGO_MULTILINGUAL[de]' into
    {'LOGIN_LOGO_MULTILINGUAL': {'de': value}}.

    For JSON data (regular dicts), this function preserves the original structure
    including lists and nested objects.
    """
    import re

    # Check if data has bracket notation keys that need parsing
    bracket_pattern = re.compile(r"^(\w+)\[(\w+)\]$")
    has_bracket_keys = any(bracket_pattern.match(str(key)) for key in data.keys())

    # If no bracket notation keys exist, return data as-is to preserve lists/dicts
    if not has_bracket_keys:
        # For JSON data, use dict() to get a clean copy
        # For QueryDict/files, iterate to preserve file objects
        if hasattr(data, "getlist"):
            # It's a QueryDict, iterate to preserve structure
            result = {}
            for key in data.keys():
                values = data.getlist(key)
                result[key] = values if len(values) > 1 else data[key]
            return result
        else:
            # Regular dict (JSON), return as-is to preserve lists/nested structures
            return dict(data)

    # Build result dict by iterating over items to preserve file objects
    # Using dict(data) on QueryDict doesn't properly handle file uploads
    result = {key: value for key, value in data.items()}

    keys_to_remove = []
    nested_values = {}

    for key, value in data.items():
        match = bracket_pattern.match(key)
        if match:
            base_key, nested_key = match.groups()
            if base_key not in nested_values:
                nested_values[base_key] = {}
            nested_values[base_key][nested_key] = value
            keys_to_remove.append(key)

    for key in keys_to_remove:
        result.pop(key, None)

    result.update(nested_values)
    return result


@extend_schema(
    methods=["GET"],
    summary="Get all overridable settings",
    description="Returns all settings that can be overridden in the database via the Constance backend. Requires admin permissions.",
    request=None,
    responses={status.HTTP_200_OK: ConstanceSettingsSerializer},
)
@extend_schema(
    methods=["POST"],
    summary="Update overridable settings",
    description="Updates one or more settings in the database via the Constance backend. Requires admin permissions.",
    request=ConstanceSettingsSerializer,
    responses={status.HTTP_200_OK: None},
    examples=[
        OpenApiExample(
            "Enable marketplace and set a custom title",
            summary="An example of updating multiple settings at once.",
            value={
                "WALDUR_MARKETPLACE_ENABLED": True,
                "SITE_NAME": "My Custom Cloud Portal",
            },
            request_only=True,
        )
    ],
)
@api_view(["POST", "GET"])
@permission_classes((rf_permissions.IsAdminUser,))
def override_db_settings(request):
    if request.method == "GET":
        from constance.admin import get_values

        return Response(get_values())

    # Parse bracket notation keys for nested dict fields (e.g., multilingual images)
    data = _parse_bracket_notation(request.data)

    serializer = ConstanceSettingsSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    cache.delete("API_CONFIGURATION")
    return Response(status=status.HTTP_200_OK)


@extend_schema(
    summary="Update feature flags",
    description="Allows administrators to enable or disable specific feature flags in the system. The request should be a dictionary where keys are feature sections and values are dictionaries of feature keys and their boolean state.",
    request=dict,
    responses={status.HTTP_200_OK: str},
    examples=[
        OpenApiExample(
            "Update user and marketplace features",
            summary="Example of enabling SSH keys for users and concealing prices in the marketplace.",
            value={"user": {"ssh_keys": True}, "marketplace": {"conceal_prices": True}},
            request_only=True,
        ),
        OpenApiExample(
            "Successful response",
            summary="Example response after updating two features.",
            value="2 features are updated.",
            response_only=True,
            status_codes=[200],
        ),
    ],
)
@api_view(["POST"])
@permission_classes((rf_permissions.IsAdminUser,))
def feature_values(request):
    if not isinstance(request.data, dict):
        return Response(
            data="Dictionary is expected.", status=status.HTTP_400_BAD_REQUEST
        )
    updated = 0
    for section in FEATURES:
        for feature in section["items"]:
            feature_value = request.data.get(section["key"], {}).get(feature["key"])
            if feature_value is not None:
                models.Feature.objects.update_or_create(
                    key=f"{section['key']}.{feature['key']}",
                    defaults=dict(value=feature_value),
                )
                updated += 1
    if updated:
        cache.delete("API_CONFIGURATION")
    return Response(data=f"{updated} features are updated.", status=status.HTTP_200_OK)


def redirect_with(url_template, **kwargs):
    params = urlencode(kwargs)
    url = f"{url_template}?{params}"
    return HttpResponseRedirect(url)


def login_completed(token, method="default"):
    url = format_homeport_link(
        "login_completed/{token}/{method}/", token=token, method=method
    )
    return HttpResponseRedirect(url)


def login_failed(message):
    url_template = format_homeport_link("login_failed/")
    return redirect_with(url_template, message=message)


def logout_completed():
    return HttpResponseRedirect(format_homeport_link("logout_completed/"))


def logout_failed(message):
    url_template = format_homeport_link("logout_failed/")
    return redirect_with(url_template, message=message)


class CheckExtensionMixin:
    """Raise exception if extension is disabled"""

    extension_name = NotImplemented

    def initial(self, request, *args, **kwargs):
        conf = getattr(settings, self.extension_name, None)
        if not conf or not conf["ENABLED"]:
            raise ExtensionDisabled()
        return super().initial(request, *args, **kwargs)


class ConstanceCheckExtensionMixin:
    """Raise exception if extension is disabled"""

    extension_name = NotImplemented

    def initial(self, request, *args, **kwargs):
        from constance import config

        conf = getattr(config, f"{self.extension_name}_ENABLED", None)
        if not conf:
            raise ExtensionDisabled()
        return super().initial(request, *args, **kwargs)


class ExtraContextTemplateView(TemplateView):
    extra_context = None

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        if self.extra_context:
            extra = {}
            for key, value in self.extra_context.items():
                if callable(value):
                    value = value()
                extra[key] = value
            context.update(extra)
        return context


class CreateReversionMixin:
    """Mixin to automatically create revision tracking for create operations."""

    def perform_create(self, serializer):
        with reversion.create_revision():
            super().perform_update(serializer)
            reversion.set_user(self.request.user)
            reversion.set_comment("Created via REST API")


class UpdateReversionMixin:
    """Mixin to automatically create revision tracking for update operations."""

    def perform_update(self, serializer):
        with reversion.create_revision():
            super().perform_update(serializer)
            reversion.set_user(self.request.user)
            reversion.set_comment("Updated via REST API")


class CeleryStatsViewSet(generics.GenericAPIView):
    """
    API endpoint for monitoring Celery worker status and task queues.

    This endpoint provides real-time information about Celery workers,
    including currently executing tasks, scheduled tasks, and worker statistics.
    """

    permission_classes = [rf_permissions.IsAuthenticated, permissions.IsSupport]
    filter_backends = []
    serializer_class = EmptySerializer

    @extend_schema(
        summary="Get Celery worker statistics",
        description="""Provides a comprehensive snapshot of all Celery workers' status.

This endpoint returns detailed information about:
- **active**: Tasks currently being executed by workers
- **scheduled**: Tasks scheduled for future execution (with ETA)
- **reserved**: Tasks received by workers but not yet started
- **revoked**: Task IDs that have been cancelled/revoked
- **query_task**: Results of task queries (if any)
- **stats**: Detailed worker statistics including uptime, pool info, and broker connection

Each field is a dictionary where keys are worker names (e.g., 'celery@hostname').
If no workers are available, fields will be `null`.

Requires support user permissions.""",
        responses={status.HTTP_200_OK: CeleryStatsResponseSerializer},
        examples=[
            OpenApiExample(
                "Celery stats with active workers",
                summary="Response when Celery workers are running",
                description="Example showing active workers with tasks in various states.",
                value={
                    "active": {
                        "celery@worker1": [
                            {
                                "id": "e3a95109-acd5-4f5a-b4c4-65f2f4e5d123",
                                "name": "waldur_core.tasks.send_email",
                                "args": ["user@example.com"],
                                "kwargs": {"subject": "Welcome"},
                                "type": "waldur_core.tasks.send_email",
                                "hostname": "celery@worker1",
                                "time_start": 1703952000.123456,
                                "acknowledged": True,
                                "worker_pid": 12345,
                            }
                        ]
                    },
                    "scheduled": {"celery@worker1": []},
                    "reserved": {"celery@worker1": []},
                    "revoked": {"celery@worker1": []},
                    "query_task": None,
                    "stats": {
                        "celery@worker1": {
                            "broker": {
                                "hostname": "redis",
                                "port": 6379,
                                "transport": "redis",
                                "virtual_host": "0",
                            },
                            "clock": "12345",
                            "uptime": 86400,
                            "pid": 1234,
                            "pool": {
                                "max_concurrency": 4,
                                "processes": [12345, 12346, 12347, 12348],
                            },
                            "prefetch_count": 16,
                            "total": {
                                "waldur_core.tasks.send_email": 150,
                                "waldur_core.tasks.cleanup": 42,
                            },
                        }
                    },
                },
                response_only=True,
                status_codes=[200],
            ),
            OpenApiExample(
                "No workers available",
                summary="Response when no Celery workers are running",
                description="All fields are null when workers are offline or unreachable.",
                value={
                    "active": None,
                    "scheduled": None,
                    "reserved": None,
                    "revoked": None,
                    "query_task": None,
                    "stats": None,
                },
                response_only=True,
                status_codes=[200],
            ),
        ],
    )
    def get(self, request, *args, **kwargs):
        """
        Retrieve current Celery worker statistics and task information.

        Returns a snapshot of all registered Celery workers, their active tasks,
        scheduled tasks, reserved tasks, revoked task IDs, and detailed statistics.
        """
        from waldur_core.server.celeryconf import app

        inspect = app.control.inspect()
        data = {
            "active": inspect.active(),
            "scheduled": inspect.scheduled(),
            "reserved": inspect.reserved(),
            "revoked": inspect.revoked(),
            "query_task": inspect.query_task(),
            "stats": inspect.stats(),
        }
        return Response(
            data,
            status=status.HTTP_200_OK,
        )


SQL_QUERY = """
SELECT
    relname AS table_name,
    pg_total_relation_size(relid) AS total_size,
    pg_relation_size(relid) AS data_size,
    (pg_total_relation_size(relid) - pg_relation_size(relid)) AS external_size
FROM
    pg_catalog.pg_statio_user_tables
ORDER BY
    pg_total_relation_size(relid) DESC,
    pg_relation_size(relid) DESC
LIMIT 10;
"""


def dictfetchall(cursor):
    """
    Return all rows from a cursor as a dict.
    Assume the column names are unique.
    """
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


class DatabaseStatsViewSet(APIView):
    permission_classes = [rf_permissions.IsAuthenticated, permissions.IsSupport]

    @extend_schema(
        summary="Get database table statistics",
        description="Retrieves statistics about the database, including the top 10 largest tables by total size. This information is useful for monitoring and maintenance. Requires support user permissions.",
        request=None,
        responses=TableSizeSerializer(many=True),
    )
    def get(self, request, *args, **kwargs):
        data = []
        with connection.cursor() as cursor:
            cursor.execute(SQL_QUERY)
            data = dictfetchall(cursor)

        return Response(data, status=status.HTTP_200_OK)


class QueryViewSet(generics.GenericAPIView):
    permission_classes = [rf_permissions.IsAuthenticated, permissions.IsSupport]
    serializer_class = QuerySerializer

    @extend_schema(
        summary="Execute read-only SQL query",
        description="Execute a given SQL query against a read-only database replica. This is a powerful tool for diagnostics and reporting, but should be used with caution. Requires support user permissions.",
        request=QuerySerializer,
        responses={
            200: list[Any],
            400: None,
        },
        examples=[
            OpenApiExample(
                "List all customers",
                summary="Example of a query to retrieve all customers.",
                value={
                    "query": "SELECT uuid, name, country FROM structure_customer LIMIT 10;"
                },
                request_only=True,
            ),
            OpenApiExample(
                "Successful response",
                summary="Example response for a successful query.",
                value=[
                    ["a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6", "Customer A", "US"],
                    ["b2c3d4e5-f6a7-b8c9-d0e1-f2a3b4c5d6a7", "Customer B", "DE"],
                ],
                response_only=True,
                status_codes=[200],
            ),
            OpenApiExample(
                "Error response",
                summary="Example response for a failed query.",
                value={"error": 'syntax error at or near "SELEC"'},
                response_only=True,
                status_codes=[400],
            ),
        ],
    )
    def post(self, request, *args, **kwargs):
        data = []
        query = request.data.get("query")
        if not query:
            return Response(
                {"error": "Query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "readonly" not in settings.DATABASES:
            return Response(
                {"error": "Unable to establish database connection"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        logger.info(
            f"User {user.full_name} / {user.uuid} executing query '{query}'",
        )

        try:
            with connections["readonly"].cursor() as cursor:
                cursor.execute(query)
                data = cursor.fetchall()
                return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(exclude=True)
@api_view(["GET"])
@permission_classes((rf_permissions.AllowAny,))
def get_whitelabeling_logo(request, logo_type, default_image=None):
    file_name = None
    language = request.query_params.get("language")

    # Check for language-specific version (only for LOGIN_LOGO)
    if language and logo_type == "LOGIN_LOGO":
        multilingual = getattr(config, "LOGIN_LOGO_MULTILINGUAL", None) or {}
        file_name = multilingual.get(language)

    # Fallback to default setting
    if not file_name:
        file_name = getattr(config, logo_type)

    if file_name:
        try:
            content_type, encoding = mimetypes.guess_type(file_name)
            return FileResponse(
                default_storage.open(file_name), content_type=content_type
            )
        except FileNotFoundError:
            logger.error(f"Custom logo file not found: {file_name}")
    if default_image:
        try:
            content_type, encoding = mimetypes.guess_type(default_image)
            with open(default_image, "rb") as f:
                return HttpResponse(f.read(), content_type=content_type)
        except FileNotFoundError:
            logger.error(f"Default logo file not found: {default_image}")
    # Return 404 if we couldn't serve either custom or default
    return Response(
        {"error": f"{logo_type} not found"}, status=status.HTTP_404_NOT_FOUND
    )


def get_latest_github_tag(timeout=5):
    """
    Fetch the latest tag from GitHub with caching.
    Cache timeout is 1 hour to avoid hitting GitHub API too frequently.
    """
    cache_key = "waldur_latest_github_tag"
    cached_version = cache.get(cache_key)
    if cached_version:
        return cached_version

    try:
        response = requests.get(
            "https://api.github.com/repos/waldur/waldur-docs/git/refs/tags",
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch latest GitHub tag: {str(e)}")
        return None

    try:
        tags = response.json()
    except requests.JSONDecodeError:
        logger.error("Failed to decode JSON response from GitHub")
        return None

    # Get the last tag (most recent one)
    try:
        tags.sort(key=lambda x: version.Version(x["ref"].split("/")[-1]))
        latest_tag = tags[-1]["ref"].replace("refs/tags/", "")

    except (TypeError, KeyError, version.InvalidVersion) as e:
        logger.error("Error processing GitHub tags: %s", str(e))
        return None

    # Cache for 1 hour
    cache.set(cache_key, latest_tag, timeout=3600)
    return latest_tag


@extend_schema(
    summary="Get application version",
    description="Retrieves the current installed version of the application and the latest available version from GitHub (if available). Requires staff or support user permissions.",
    request=None,
    responses=VersionSerializer,
)
@api_view(["GET"])
@permission_classes(
    (
        rf_permissions.IsAuthenticated,
        IsStaffOrSupportUser,
    )
)
def version_detail(request):
    """Retrieve version of the application"""

    response_data = {
        "version": __version__,
    }

    latest_version = get_latest_github_tag()
    if latest_version:
        response_data["latest_version"] = latest_version
    serializer = VersionSerializer(response_data)
    return Response(serializer.data)


class ActionMethodMixin:
    """Implements helper methods for viewset when use separate
    nested endpoints for create/edit relations objects.
    Example:

    @decorators.action(detail=True, methods=["get", "post"])
    def offerings(self, request, uuid=None):
        return self.action_list_method("requestedoffering_set")(self, request, uuid)

    offerings_serializer_class = serializers.RequestedOfferingSerializer

    def offering_detail(self, request, uuid=None, obj_uuid=None):
        return self.action_detail_method(
            "requestedoffering_set",
            delete_validators=[],
            update_validators=[
                core_validators.StateValidator(
                    RequestedOfferingStates.REQUESTED
                )
            ],
        )(self, request, uuid, obj_uuid)

    offering_detail_serializer_class = serializers.RequestedOfferingSerializer

    """

    @staticmethod
    def action_list_method(set_name, additional_validators=None):
        additional_validators = additional_validators or []

        def func(self, request, uuid=None):
            obj = self.get_object()
            method = self.request.method
            parent_obj_name = None

            if method == "POST":
                data = self.request.data.copy()
                serializer_class = self.get_serializer_class()
                model = self.get_serializer_class().Meta.model
                parent_model = obj.__class__
                fields = [
                    f
                    for f in model._meta.fields
                    if f.__class__ == ForeignKey and f.related_model == parent_model
                ]

                if fields:
                    parent_obj_name = fields[0].name
                    data[parent_obj_name] = obj.pk

                serializer = serializer_class(
                    context=self.get_serializer_context(), data=data
                )

                if parent_obj_name:
                    serializer.fields[parent_obj_name] = (
                        serializers.PrimaryKeyRelatedField(
                            write_only=True, queryset=parent_model.objects.all()
                        )
                    )

                serializer.is_valid(raise_exception=True)

                for validator in additional_validators:
                    getattr(serializer, validator)(obj)

                serializer.save()

                return Response(
                    serializer.data,
                    status=status.HTTP_201_CREATED,
                )

            return Response(
                self.get_serializer(
                    getattr(obj, set_name),
                    context=self.get_serializer_context(),
                    many=True,
                ).data,
                status=status.HTTP_200_OK,
            )

        return func

    @staticmethod
    def action_detail_method(set_name, delete_validators=None, update_validators=None):
        delete_validators = delete_validators or []
        update_validators = update_validators or []

        def func(self, request, uuid=None, obj_uuid=None):
            method = self.request.method

            try:
                obj = getattr(self.get_object(), set_name).get(uuid=obj_uuid)

                if method == "DELETE":
                    [validator(obj) for validator in delete_validators]
                    obj.delete()
                    return Response(status=status.HTTP_204_NO_CONTENT)

                if method in ["PUT", "PATCH"]:
                    [validator(obj) for validator in update_validators]

                    serializer = self.get_serializer(
                        obj,
                        context=self.get_serializer_context(),
                        data=self.request.data,
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    return Response(serializer.data, status=status.HTTP_200_OK)

                serializer = self.get_serializer(
                    obj, context=self.get_serializer_context()
                )
                return Response(serializer.data, status=status.HTTP_200_OK)
            except ObjectDoesNotExist:
                return Response(status=status.HTTP_404_NOT_FOUND)

        return func


ORIGINAL_LIST_MODEL_MIXIN_LIST = mixins.ListModelMixin.list


# Mixin to optimize HEAD requests for DRF views bypassing serializer processing
def optimized_head_list(cls, request, *args, **kwargs):
    # HEAD requests are mapped to list by default router
    if request.method != "HEAD":
        return ORIGINAL_LIST_MODEL_MIXIN_LIST(cls, request, *args, **kwargs)
    paginator = cls.pagination_class()
    queryset = cls.filter_queryset(cls.get_queryset())
    paginator.paginate_queryset(queryset, request)
    # Paginator expects serialized data, but we don't need it, thus empty list
    return paginator.get_paginated_response([])


# Monkey-patching
mixins.ListModelMixin.list = optimized_head_list  # type: ignore


class PermissionMetadataView(APIView):
    """Provides permission metadata including roles, permissions, and descriptions."""

    permission_classes = []
    authentication_classes = []

    @extend_schema(
        summary="Get permission metadata",
        description="Retrieves metadata about roles, permissions, and their descriptions. This endpoint is publicly accessible and provides data needed for UI components, such as role selection dropdowns and permission management interfaces.",
        responses={200: PermissionMetadataResponseSerializer},
    )
    def get(self, request):
        return Response(
            {
                "roles": {
                    key: value.value for key, value in RoleEnum._member_map_.items()
                },
                "permissions": {
                    key: value.value
                    for key, value in PermissionEnum._member_map_.items()
                },
                "permission_map": {
                    key: value.value for key, value in CREATE_PERMISSIONS.items()
                },
                "permission_descriptions": PERMISSION_DESCRIPTION,
            }
        )


class EventMetadataView(APIView):
    """Provides event metadata grouped by event categories."""

    permission_classes = []
    authentication_classes = []

    @extend_schema(
        summary="Get event metadata",
        description="Retrieves metadata for all available event types, grouped by categories. This endpoint is publicly accessible and is useful for building UIs for event filtering or webhook configuration.",
        responses={200: EventMetadataResponseSerializer},
    )
    def get(self, request):
        return Response({"event_groups": get_event_groups()})


class FeatureMetadataView(APIView):
    """Provides feature toggle metadata including enums and descriptions."""

    permission_classes = []
    authentication_classes = []

    @extend_schema(
        summary="Get feature flag metadata",
        description="Retrieves metadata for all available feature flags, including their keys, descriptions, and grouping sections. This endpoint is publicly accessible and helps UIs to dynamically render feature-related settings.",
        responses={200: FeatureMetadataResponseSerializer},
    )
    def get(self, request):
        feature_enums = {}
        for section in FEATURES:
            section_key = section["key"]
            section_enums = {}
            for feature in section["items"]:
                enum_key = feature["key"]
                enum_value = f"{section_key}.{enum_key}"
                section_enums[enum_key] = enum_value
            feature_enums[section_key] = section_enums

        return Response({"features": FEATURES, "feature_enums": feature_enums})


class SettingsMetadataView(APIView):
    """Provides settings metadata from Django Constance configuration."""

    permission_classes = []
    authentication_classes = []

    @extend_schema(
        summary="Get overridable settings metadata",
        description="Retrieves metadata for all settings that can be configured via the Constance backend. This includes setting keys, human-readable descriptions, default values, and types. This endpoint is publicly accessible.",
        responses={200: SettingsMetadataResponseSerializer},
    )
    def get(self, request):
        settings_data = []
        for title, keys in settings.CONSTANCE_CONFIG_FIELDSETS.items():
            section = {"description": title, "items": []}

            for key in keys:
                if key in settings.CONSTANCE_CONFIG:
                    default = settings.CONSTANCE_CONFIG[key][0]
                    description = settings.CONSTANCE_CONFIG[key][1].replace("'", "\\'")
                    value_type = (
                        len(settings.CONSTANCE_CONFIG[key]) == 3
                        and settings.CONSTANCE_CONFIG[key][2]
                        or None
                    )

                    if isinstance(default, str):
                        formatted_default = default
                    elif default is True:
                        formatted_default = True
                    elif default is False:
                        formatted_default = False
                    else:
                        formatted_default = default

                    if value_type:
                        formatted_type = value_type
                    elif isinstance(default, str):
                        formatted_type = "string"
                    elif isinstance(default, bool):
                        formatted_type = "boolean"
                    elif isinstance(default, int):
                        formatted_type = "integer"
                    else:
                        formatted_type = "string"

                    section["items"].append(
                        {
                            "key": key,
                            "description": description,
                            "default": formatted_default,
                            "type": formatted_type,
                        }
                    )

            settings_data.append(section)

        return Response({"settings": settings_data})
