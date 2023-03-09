import functools
import logging
import mimetypes
from urllib.parse import urlencode

import reversion
from constance import config
from django.conf import settings
from django.contrib import auth
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db.models import ProtectedError
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from rest_framework import exceptions
from rest_framework import mixins as rf_mixins
from rest_framework import permissions as rf_permissions
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as rf_exception_handler

from waldur_core import __version__
from waldur_core.core import (
    WALDUR_DISABLED_EXTENSIONS,
    WaldurExtension,
    models,
    permissions,
)
from waldur_core.core.exceptions import ExtensionDisabled, IncorrectStateException
from waldur_core.core.features import FEATURES
from waldur_core.core.metadata import WaldurConfiguration
from waldur_core.core.mixins import ReviewMixin, ensure_atomic_transaction
from waldur_core.core.serializers import AuthTokenSerializer, ReviewCommentSerializer
from waldur_core.core.utils import format_homeport_link
from waldur_core.core.validators import StateValidator
from waldur_core.logging.loggers import event_logger

logger = logging.getLogger(__name__)


def validate_authentication_method(method):
    def wrapper(view_func):
        @functools.wraps(view_func)
        def wrapped_view(*args, **kwargs):
            if method not in settings.WALDUR_CORE['AUTHENTICATION_METHODS']:
                message = (
                    'Authentication method is disabled. '
                    'Please use another authentication method or contact staff.'
                )
                return JsonResponse(
                    status=status.HTTP_401_UNAUTHORIZED, data={'detail': message}
                )
            return view_func(*args, **kwargs)

        return wrapped_view

    return wrapper


class RefreshTokenMixin:
    """
    This mixin is used in both password and social auth (implemented via plugin).
    Mixin allows to create new token if it does not exist yet or if it has already expired.
    Token is refreshed if it has not expired yet.
    """

    def refresh_token(self, user):
        token, created = Token.objects.get_or_create(user=user)

        if user.token_lifetime:
            lifetime = timezone.timedelta(seconds=user.token_lifetime)

            if token.created < timezone.now() - lifetime:
                token.delete()
                token = Token.objects.create(user=user)
                created = True

        if not created:
            token.created = timezone.now()
            token.save(update_fields=['created'])

        return token


class ObtainAuthToken(RefreshTokenMixin, APIView):
    """
    Api view loosely based on DRF's default ObtainAuthToken,
    but with the responses formats and status codes aligned with BasicAuthentication behavior.

    Valid request example:

    .. code-block:: http

        POST /api-auth/password/ HTTP/1.1
        Accept: application/json
        Content-Type: application/json
        Host: example.com

        {
            "username": "alice",
            "password": "$ecr3t"
        }

    Success response example:

    .. code-block:: http

        HTTP/1.0 200 OK
        Allow: POST, OPTIONS
        Content-Type: application/json
        Vary: Accept, Cookie

        {
            "token": "c84d653b9ec92c6cbac41c706593e66f567a7fa4"
        }

    Field validation failure response example:

    .. code-block:: http

        HTTP/1.0 401 UNAUTHORIZED
        Allow: POST, OPTIONS
        Content-Type: application/json

        {
            "password": ["This field is required."]
        }

    Invalid credentials failure response example:

    .. code-block:: http

        HTTP/1.0 401 UNAUTHORIZED
        Allow: POST, OPTIONS
        Content-Type: application/json

        {
            "detail": "Invalid username/password"
        }
    """

    throttle_classes = ()
    permission_classes = ()
    serializer_class = AuthTokenSerializer

    @validate_authentication_method('LOCAL_SIGNIN')
    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data['username']

        source_ip = request.META.get('REMOTE_ADDR')
        auth_failure_key = 'LOGIN_FAILURES_OF_%s_AT_%s' % (username, source_ip)
        auth_failures = cache.get(auth_failure_key) or 0
        lockout_time_in_mins = 10

        if auth_failures >= 4:
            logger.debug(
                'Not returning auth token: '
                'username %s from %s is locked out' % (username, source_ip)
            )
            return Response(
                data={
                    'detail': _('Username is locked out. Try in %s minutes.')
                    % lockout_time_in_mins
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        user = auth.authenticate(
            request=request,
            username=username,
            password=serializer.validated_data['password'],
        )

        if not user:
            logger.debug(
                'Not returning auth token: ' 'user %s does not exist', username
            )
            cache.set(auth_failure_key, auth_failures + 1, lockout_time_in_mins * 60)
            event_logger.auth.info(
                'User {username} failed to authenticate with username and password.',
                event_type='auth_login_failed_with_username',
                event_context={'username': username},
            )

            return Response(
                data={'detail': _('Invalid username/password.')},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        else:
            cache.delete(auth_failure_key)

        if not user.is_active:
            logger.debug('Not returning auth token: ' 'user %s is disabled', username)
            return Response(
                data={'detail': _('User account is disabled.')},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token = self.refresh_token(user)
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])

        logger.debug('Returning token for successful login of user %s', user)

        event_logger.auth.info(
            'User {user_username} with full name {user_full_name} '
            'authenticated successfully with username and password.',
            event_type='auth_logged_in_with_username',
            event_context={'user': user, 'request': request},
        )

        return Response({'token': token.key})


obtain_auth_token = ObtainAuthToken.as_view()


@api_view(['GET'])
@permission_classes((rf_permissions.AllowAny,))
def version_detail(request):
    """Retrieve version of the application"""

    return Response({'version': __version__})


# noinspection PyProtectedMember
def exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        if len(exc.protected_objects) == 1:
            dependent_meta = list(exc.protected_objects)[0]._meta

            try:
                # This exception should be raised from a viewset
                instance_meta = context['view'].get_queryset().model._meta
            except (AttributeError, KeyError):
                # Fallback, when instance being deleted cannot be inferred
                instance_name = 'object'
            else:
                instance_name = force_str(instance_meta.verbose_name)

            detail = _(
                'Cannot delete {instance_name} with existing {dependant_objects}'
            ).format(
                instance_name=instance_name,
                dependant_objects=force_str(dependent_meta.verbose_name_plural),
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
        return super(ActionsViewSet, self).dispatch(request, *args, **kwargs)

    def get_serializer_class(self):
        default_serializer_class = super(ActionsViewSet, self).get_serializer_class()
        if self.action is None:
            return default_serializer_class
        return getattr(
            self, self.action + '_serializer_class', default_serializer_class
        )

    def initial(self, request, *args, **kwargs):
        super(ActionsViewSet, self).initial(request, *args, **kwargs)
        if (
            self.action is None
        ):  # disable all checks if user tries to reach unsupported action
            return
        # check if action is allowed
        if self.action in getattr(self, 'disabled_actions', []):
            raise exceptions.MethodNotAllowed(method=request.method)
        self.validate_object_action(self.action)

    def validate_object_action(self, action_name, obj=None):
        """Execute validation for actions that are related to particular object"""
        action_method = getattr(self, action_name)
        if not getattr(action_method, 'detail', False) and action_name not in (
            'update',
            'partial_update',
            'destroy',
        ):
            # DRF does not add flag 'detail' to update and delete actions, however they execute operation with
            # particular object. We need to enable validation for them too.
            return
        validators = getattr(self, action_name + '_validators', [])
        for validator in validators:
            validator(obj or self.get_object())


class ReadOnlyActionsViewSet(ActionsViewSet):
    disabled_actions = ['create', 'update', 'partial_update', 'destroy']


def get_feature_values():
    feature_values = {
        feature.key: feature.value for feature in models.Feature.objects.all()
    }
    return {
        section['key']: {
            feature['key']: feature_values.get(
                f'{section["key"]}.{feature["key"]}', False
            )
            for feature in section['items']
        }
        for section in FEATURES
    }


def get_public_settings():
    cached_settings = cache.get('API_CONFIGURATION')
    if cached_settings:
        return cached_settings
    public_settings = {}

    public_settings['WALDUR_DISABLED_EXTENSIONS'] = WALDUR_DISABLED_EXTENSIONS
    public_settings['FEATURES'] = get_feature_values()

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

    # Processing a others extensions
    for ext in WaldurExtension.get_extensions():
        settings_name = [x for x in dir(ext.Settings) if x.startswith('WALDUR_')]
        if not settings_name:
            continue

        settings_name = settings_name[0]
        extension_settings = getattr(settings, settings_name, None)
        if extension_settings and extension_settings.get('ENABLED', True):
            public_settings[settings_name] = {}

            for s in ext.get_public_settings():
                try:
                    public_settings[settings_name][s] = extension_settings[s]
                except KeyError:
                    pass

            for s, v in ext.get_dynamic_settings().items():
                public_settings[settings_name][s] = v

    from constance.admin import get_values

    constance_settings = get_values()

    if public_settings.get('WALDUR_CORE'):
        public_settings['WALDUR_CORE'].update(constance_settings)

    cache.set(
        'API_CONFIGURATION', public_settings, None
    )  # Cache invalidation is handled explicitly
    return public_settings


@api_view(['GET'])
@permission_classes((rf_permissions.AllowAny,))
def configuration_detail(request):
    return Response(get_public_settings())


@api_view(['GET'])
@permission_classes((rf_permissions.AllowAny,))
def features_description(request):
    return Response(FEATURES)


@api_view(['POST'])
@permission_classes((rf_permissions.IsAdminUser,))
def feature_values(request):
    if not isinstance(request.data, dict):
        return Response(
            data='Dictionary is expected.', status=status.HTTP_400_BAD_REQUEST
        )
    updated = 0
    for section in FEATURES:
        for feature in section['items']:
            feature_value = request.data.get(section['key'], {}).get(feature['key'])
            if feature_value is not None:
                models.Feature.objects.update_or_create(
                    key=f'{section["key"]}.{feature["key"]}',
                    defaults=dict(value=feature_value),
                )
                updated += 1
    if updated:
        cache.delete('API_CONFIGURATION')
    return Response(data=f'{updated} features are updated.', status=status.HTTP_200_OK)


def redirect_with(url_template, **kwargs):
    params = urlencode(kwargs)
    url = '%s?%s' % (url_template, params)
    return HttpResponseRedirect(url)


def login_completed(token, method='default'):
    url = format_homeport_link(
        'login_completed/{token}/{method}/', token=token, method=method
    )
    return HttpResponseRedirect(url)


def login_failed(message):
    url_template = format_homeport_link('login_failed/')
    return redirect_with(url_template, message=message)


def logout_completed():
    return HttpResponseRedirect(format_homeport_link('logout_completed/'))


def logout_failed(message):
    url_template = format_homeport_link('logout_failed/')
    return redirect_with(url_template, message=message)


class CheckExtensionMixin:
    """Raise exception if extension is disabled"""

    extension_name = NotImplemented

    def initial(self, request, *args, **kwargs):
        conf = getattr(settings, self.extension_name, None)
        if not conf or not conf['ENABLED']:
            raise ExtensionDisabled()
        return super(CheckExtensionMixin, self).initial(request, *args, **kwargs)


class ExtraContextTemplateView(TemplateView):
    extra_context = None

    def get_context_data(self, *args, **kwargs):
        context = super(ExtraContextTemplateView, self).get_context_data(
            *args, **kwargs
        )
        if self.extra_context:
            context.update(self.extra_context)
        return context


class CreateReversionMixin:
    def perform_create(self, serializer):
        with reversion.create_revision():
            super(CreateReversionMixin, self).perform_update(serializer)
            reversion.set_user(self.request.user)
            reversion.set_comment('Created via REST API')


class UpdateReversionMixin:
    def perform_update(self, serializer):
        with reversion.create_revision():
            super(UpdateReversionMixin, self).perform_update(serializer)
            reversion.set_user(self.request.user)
            reversion.set_comment('Updated via REST API')


class ReviewViewSet(ActionsViewSet):
    disabled_actions = ['create', 'destroy', 'update', 'partial_update']
    lookup_field = 'uuid'

    @action(detail=True, methods=['post'])
    def approve(self, request, **kwargs):
        review_request = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get('comment')
        review_request.approve(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def reject(self, request, **kwargs):
        review_request = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get('comment')
        review_request.reject(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer
    approve_validators = reject_validators = [
        StateValidator(ReviewMixin.States.PENDING)
    ]


class CeleryStatsViewSet(APIView):
    permission_classes = [rf_permissions.IsAuthenticated, permissions.IsSupport]

    def get(self, request, *args, **kwargs):
        from waldur_core.server.celery import app

        inspect = app.control.inspect()
        data = {
            'active': inspect.active(),
            'scheduled': inspect.scheduled(),
            'reserved': inspect.reserved(),
            'revoked': inspect.revoked(),
            'query_task': inspect.query_task(),
            'stats': inspect.stats(),
        }
        return Response(
            data,
            status=status.HTTP_200_OK,
        )


@api_view(['GET'])
@permission_classes((rf_permissions.AllowAny,))
def get_whitelabeling_logo(request, logo_type, default_image=None):
    try:
        file_name = getattr(config, logo_type)
        content_type, encoding = mimetypes.guess_type(file_name)
        return FileResponse(default_storage.open(file_name), content_type=content_type)
    except NotImplementedError:  # storage cannot handle empty response
        if default_image:
            content_type, encoding = mimetypes.guess_type(default_image)
            image_data = open(default_image, "rb").read()
            return HttpResponse(image_data, content_type=content_type)
    return Response(
        {'error': f'{logo_type} not found'}, status=status.HTTP_404_NOT_FOUND
    )
