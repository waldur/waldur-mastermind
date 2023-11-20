import logging
from functools import partial

from dbtemplates.models import Template
from dbtemplates.utils.cache import remove_cached_template
from django.conf import settings as django_settings
from django.contrib import auth
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.db.models import Q
from django.db.utils import DataError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rf_filters
from rest_framework import mixins
from rest_framework import permissions as rf_permissions
from rest_framework import serializers as rf_serializers
from rest_framework import status, viewsets
from rest_framework.authtoken import models as authtoken_models
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_auth_social.models import ProviderChoices
from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import signals as core_signals
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.utils import is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import count_users
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters, models, permissions, serializers, utils
from waldur_core.structure.managers import (
    count_customer_users,
    filter_customer_permissions,
    filter_project_permissions,
    filter_queryset_for_user,
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.permissions import _has_owner_access

logger = logging.getLogger(__name__)

User = auth.get_user_model()


class CustomerViewSet(UserRoleMixin, core_mixins.EagerLoadMixin, viewsets.ModelViewSet):
    queryset = models.Customer.objects.all().order_by('name')
    serializer_class = serializers.CustomerSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.GenericUserFilter,
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        rf_filters.OrderingFilter,
        filters.OwnedByCurrentUserFilterBackend,
        filters.AccountingStartDateFilter,
        filters.ExternalCustomerFilterBackend,
    )
    ordering_fields = (
        'abbreviation',
        'accounting_start_date',
        'agreement_number',
        'contact_details',
        'created',
        'name',
        'native_name',
        'registration_code',
    )
    filterset_class = filters.CustomerFilter

    def list(self, request, *args, **kwargs):
        """
        To get a list of customers, run GET against */api/customers/* as authenticated user. Note that a user can
        only see connected customers:

        - customers that the user owns
        - customers that have a project where user has a role

        Staff also can filter customers by user UUID, for example /api/customers/?user_uuid=<UUID>

        Staff also can filter customers by exists accounting_start_date, for example:

        The first category:
        /api/customers/?accounting_is_running=True
            has accounting_start_date empty (i.e. accounting starts at once)
            has accounting_start_date in the past (i.e. has already started).

        Those that are not in the first:
        /api/customers/?accounting_is_running=False # exists accounting_start_date

        """
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        Optional `field` query parameter (can be list) allows to limit what fields are returned.
        For example, given request /api/customers/<uuid>/?field=uuid&field=name you get response like this:

        .. code-block:: javascript

            {
                "uuid": "90bcfe38b0124c9bbdadd617b5d739f5",
                "name": "Ministry of Bells"
            }
        """
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """
        A new customer can only be created:

         - by users with staff privilege (is_staff=True);
         - by any user if OWNER_CAN_MANAGE_CUSTOMER is set to True;

        If user who has created new organization is not staff, he is granted owner permission.

        Example of a valid request:

        .. code-block:: http

            POST /api/customers/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "Customer A",
                "native_name": "Customer A",
                "abbreviation": "CA",
                "contact_details": "Luhamaa 28, 10128 Tallinn",
            }
        """
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Deletion of a customer is done through sending a **DELETE** request to the customer instance URI. Please note,
        that if a customer has connected projects, deletion request will fail with 409 response code.

        Valid request example (token is user specific):

        .. code-block:: http

            DELETE /api/customers/6c9b01c251c24174a6691a1f894fae31/ HTTP/1.1
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com
        """
        return super().destroy(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.action == 'users':
            return serializers.CustomerUserSerializer
        return super().get_serializer_class()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action == 'users':
            context['customer'] = self.get_object()
        return context

    def check_customer_permissions(self, customer=None):
        if self.request.user.is_staff:
            return

        if not django_settings.WALDUR_CORE.get('OWNER_CAN_MANAGE_CUSTOMER'):
            raise PermissionDenied()

        if not customer:
            return

        if not customer.has_user(self.request.user, models.CustomerRole.OWNER):
            raise PermissionDenied()

    def perform_create(self, serializer):
        self.check_customer_permissions()
        customer = serializer.save()
        if not self.request.user.is_staff:
            customer.add_user(
                self.request.user, models.CustomerRole.OWNER, self.request.user
            )

        if django_settings.WALDUR_CORE.get(
            'CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION', False
        ):
            project = models.Project(
                name=_('First project'),
                description=_('First project we have created for you'),
                customer=customer,
            )
            project.save()

    def perform_update(self, serializer):
        self.check_customer_permissions(serializer.instance)
        utils.check_customer_blocked_or_archived(serializer.instance)
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        self.check_customer_permissions(instance)
        utils.check_customer_blocked_or_archived(instance)

        core_signals.pre_delete_validate.send(
            sender=models.Customer, instance=instance, user=self.request.user
        )

        return super().perform_destroy(instance)

    @action(
        detail=True,
        filter_backends=[filters.GenericRoleFilter],
    )
    def users(self, request, uuid=None):
        """A list of users connected to the customer."""
        customer = self.get_object()
        user = request.user
        queryset = customer.get_users()

        if not (
            _has_owner_access(user, customer)
            or user.is_support
            or customer.has_user(user, models.CustomerRole.SUPPORT)
        ):
            return Response(status=status.HTTP_403_FORBIDDEN)

        # we need to handle filtration manually because we want to filter only customer users, not customers.
        name_filter_backend = filters.UserConcatenatedNameOrderingBackend()
        queryset = name_filter_backend.filter_queryset(request, queryset, self)
        roles_filter_backend = filters.UserRolesFilter()
        queryset = roles_filter_backend.filter_queryset(request, queryset, self)
        queryset = self.paginate_queryset(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)


class ProjectTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.ProjectType.objects.all()
    serializer_class = serializers.ProjectTypeSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectTypeFilter


class ProjectViewSet(
    UserRoleMixin, core_mixins.EagerLoadMixin, core_views.ActionsViewSet
):
    queryset = models.Project.available_objects.all().order_by('name')
    serializer_class = serializers.ProjectSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.GenericUserFilter,
        filters.ProjectEstimatedCostFilter,
        filters.GenericRoleFilter,
        filters.CustomerAccountingStartDateFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.ProjectFilter
    partial_update_validators = [utils.check_customer_blocked_or_archived]
    destroy_validators = [
        utils.check_customer_blocked_or_archived,
        utils.project_is_empty,
    ]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action == 'users':
            context['project'] = self.get_object()
        return context

    def list(self, request, *args, **kwargs):
        """
        To get a list of projects, run **GET** against */api/projects/* as authenticated user.
        Here you can also check actual value for project quotas and project usage

        Note that a user can only see connected projects:

        - projects that the user owns as a customer
        - projects where user has any role

        Supported logic filters:

        - ?can_manage - return a list of projects where current user is manager or a customer owner;
        - ?can_admin - return a list of projects where current user is admin;
        """
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        Optional `field` query parameter (can be list) allows to limit what fields are returned.
        For example, given request /api/projects/<uuid>/?field=uuid&field=name you get response like this:

        .. code-block:: javascript

            {
                "uuid": "90bcfe38b0124c9bbdadd617b5d739f5",
                "name": "Default"
            }
        """
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """
        A new project can be created by users with staff privilege (is_staff=True) or customer owners.
        Project resource quota is optional. Example of a valid request:

        .. code-block:: http

            POST /api/projects/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "Project A",
                "customer": "http://example.com/api/customers/6c9b01c251c24174a6691a1f894fae31/",
            }
        """
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        Deletion of a project is done through sending a **DELETE** request to the project instance URI.
        Please note, that if a project has connected instances, deletion request will fail with 409 response code.

        Valid request example (token is user specific):

        .. code-block:: http

            DELETE /api/projects/6c9b01c251c24174a6691a1f894fae31/ HTTP/1.1
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com
        """
        return super().destroy(request, *args, **kwargs)

    def can_create_project_with(self, customer):
        user = self.request.user

        if user.is_staff:
            return True

        if customer.has_user(user, models.CustomerRole.OWNER):
            return True

        return False

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        can_manage = self.request.query_params.get('can_manage', None)
        if can_manage is not None:
            connected_customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)
            connected_projects = get_connected_projects(user, RoleEnum.PROJECT_MANAGER)
            queryset = queryset.filter(
                Q(customer__in=connected_customers) | Q(id__in=connected_projects)
            ).distinct()

        can_admin = self.request.query_params.get('can_admin', None)

        if can_admin is not None:
            connected_projects = get_connected_projects(user, RoleEnum.PROJECT_ADMIN)
            queryset = queryset.filter(id__in=connected_projects)

        return queryset

    def perform_create(self, serializer):
        customer = serializer.validated_data['customer']

        if not self.can_create_project_with(customer):
            raise PermissionDenied()

        utils.check_customer_blocked_or_archived(customer)

        super().perform_create(serializer)

    @action(detail=True, filter_backends=[filters.GenericRoleFilter])
    def users(self, request, uuid=None):
        """A list of users connected to the project"""
        project = self.get_object()
        queryset = project.get_users()
        # we need to handle filtration manually because we want to filter only project users, not projects.
        filter_backend = filters.UserConcatenatedNameOrderingBackend()
        queryset = filter_backend.filter_queryset(request, queryset, self)
        queryset = self.paginate_queryset(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)

    users_serializer_class = serializers.ProjectUserSerializer

    @action(detail=True, methods=['post'])
    def move_project(self, request, uuid=None):
        project = self.get_object()
        serializer = self.get_serializer(project, data=request.data)
        serializer.is_valid(raise_exception=True)

        customer = serializer.validated_data['customer']

        utils.move_project(project, customer, request.user)
        serialized_project = serializers.ProjectSerializer(
            project, context={'request': self.request}
        )

        return Response(serialized_project.data, status=status.HTTP_200_OK)

    move_project_serializer_class = serializers.MoveProjectSerializer
    move_project_permissions = [permissions.is_staff]

    @action(detail=False, methods=['get'])
    def oecd_codes(self, request):
        return Response(
            [
                {'value': value, 'label': label}
                for (value, label) in models.Project.OECD_FOS_2007_CODES
            ]
        )


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = serializers.UserSerializer
    lookup_field = 'uuid'
    permission_classes = (
        rf_permissions.IsAuthenticated,
        permissions.IsAdminOrOwner,
    )
    filter_backends = (
        filters.CustomerUserFilter,
        filters.ProjectUserFilter,
        filters.UserFilterBackend,
        DjangoFilterBackend,
    )
    filterset_class = filters.UserFilter

    def list(self, request, *args, **kwargs):
        """
        User list is available to all authenticated users. To get a list,
        issue authenticated **GET** request against */api/users/*.

        User list supports several filters. All filters are set in HTTP query section.
        Field filters are listed below. All of the filters apart from ?organization are
        using case insensitive partial matching.

        Several custom filters are supported:

        - ?current - filters out user making a request. Useful for getting information about a currently logged in user.
        - ?civil_number=XXX - filters out users with a specified civil number
        - ?is_active=True|False - show only active (non-active) users

        The user can be created either through automated process on login with SAML token, or through a REST call by a user
        with staff privilege.

        Example of a creation request is below.

        .. code-block:: http

            POST /api/users/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "username": "sample-user",
                "full_name": "full name",
                "native_name": "taisnimi",
                "job_title": "senior cleaning manager",
                "email": "example@example.com",
                "civil_number": "12121212",
                "phone_number": "",
                "description": "",
                "organization": "",
            }

        NB! Username field is case-insensitive. So "John" and "john" will be treated as the same user.
        """
        if request.user.is_identity_manager and not (
            request.user.is_staff or request.user.is_support
        ):
            return Response(
                _('Identity manager is not allowed to list users.'),
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().list(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def change_email(self, request, uuid=None):
        user = self.get_object()

        idp_protected_fields = utils.get_identity_provider_fields(
            user.registration_method
        )

        if 'email' in idp_protected_fields:
            raise ValidationError(
                {
                    'detail': _(
                        'The registration method does not allow direct email modification.'
                    )
                }
            )

        serializer = serializers.UserEmailChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        try:
            user.create_request_for_update_email(email)
        except django_exceptions.ValidationError as error:
            raise ValidationError(error.message_dict)

        return Response(
            {'detail': _('The change email request has been successfully created.')},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def cancel_change_email(self, request, uuid=None):
        user = self.get_object()
        count = core_models.ChangeEmailRequest.objects.filter(user=user).delete()[0]

        if count:
            msg = _('The change email request has been successfully deleted.')
        else:
            msg = _('The change email request has not been found.')

        return Response({'detail': msg}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def confirm_email(self, request):
        code = request.data.get('code')
        if not code or not is_uuid_like(code):
            raise ValidationError(_('The confirmation code is required.'))

        change_request = get_object_or_404(core_models.ChangeEmailRequest, uuid=code)

        if (
            change_request.created + django_settings.WALDUR_CORE['EMAIL_CHANGE_MAX_AGE']
            < timezone.now()
        ):
            raise ValidationError(_('Request has expired.'))

        with transaction.atomic():
            change_request.user.email = change_request.email
            change_request.user.save(update_fields=['email'])
            core_models.ChangeEmailRequest.objects.filter(
                email=change_request.email
            ).delete()
        return Response(
            {'detail': _('Email has been successfully updated.')},
            status=status.HTTP_200_OK,
        )

    def check_permissions(self, request):
        if self.action == 'confirm_email':
            return
        super().check_permissions(request)

    @action(detail=False, methods=['get'])
    def me(self, request):
        serializer = self.get_serializer(request.user)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def pull_remote_user(self, request, uuid=None):
        user = self.get_object()
        if user.registration_method != ProviderChoices.EDUTEAMS:
            raise ValidationError(_('User is not managed by eduTEAMS.'))
        if not django_settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
            raise ValidationError(
                _('Remote eduTEAMS account synchronization extension is disabled.')
            )
        pull_remote_eduteams_user(user.username)
        return Response(status=status.HTTP_200_OK)


class ProjectPermissionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.ProjectPermissionSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectPermissionFilter

    def get_queryset(self):
        return filter_project_permissions(self.request.user)


class ProjectPermissionLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.ProjectPermissionLogSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectPermissionFilter

    def get_queryset(self):
        return filter_project_permissions(self.request.user, is_active=None)


class CustomerPermissionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.CustomerPermissionSerializer
    filterset_class = filters.CustomerPermissionFilter

    def get_queryset(self):
        return filter_customer_permissions(self.request.user)


class CustomerPermissionLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.CustomerPermissionLogSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CustomerPermissionFilter

    def get_queryset(self):
        return filter_customer_permissions(self.request.user, is_active=None)


class CustomerPermissionReviewViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = models.CustomerPermissionReview.objects.all()
    serializer_class = serializers.CustomerPermissionReviewSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.CustomerPermissionReviewFilter
    lookup_field = 'uuid'

    @action(detail=True, methods=['post'])
    def close(self, request, uuid=None):
        review: models.CustomerPermissionReview = self.get_object()
        if not review.is_pending:
            raise ValidationError(_('Review is already closed.'))
        review.close(request.user)
        return Response(status=status.HTTP_200_OK)


class SshKeyViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    SSH public keys are injected to VM instances during creation, so that holder of corresponding SSH private key can
    log in to that instance.
    SSH public keys are connected to user accounts, whereas the key may belong to one user only,
    and the user may have multiple SSH keys.
    Users can only access SSH keys connected to their accounts. Staff users can see all the accounts.
    Project administrators can select what SSH key will be injected into VM instance during instance provisioning.
    """

    queryset = core_models.SshPublicKey.objects.all()
    serializer_class = serializers.SshKeySerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SshKeyFilter

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset

        return queryset.filter(Q(user=self.request.user) | Q(is_shared=True))

    def perform_destroy(self, instance):
        if instance.is_shared and not self.request.user.is_staff:
            raise PermissionDenied(
                _('Only staff users are allowed to delete shared SSH public key.')
            )
        else:
            instance.delete()

    def list(self, request, *args, **kwargs):
        """
        To get a list of SSH keys, run **GET** against */api/keys/* as authenticated user.

        A new SSH key can be created by any active users. Example of a valid request:

        .. code-block:: http

            POST /api/keys/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "ssh_public_key1",
                "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDDURXDP5YhOQUYoDuTxJ84DuzqMJYJqJ8+SZT28
                               TtLm5yBDRLKAERqtlbH2gkrQ3US58gd2r8H9jAmQOydfvgwauxuJUE4eDpaMWupqquMYsYLB5f+vVGhdZbbzfc6DTQ2rY
                               dknWoMoArlG7MvRMA/xQ0ye1muTv+mYMipnd7Z+WH0uVArYI9QBpqC/gpZRRIouQ4VIQIVWGoT6M4Kat5ZBXEa9yP+9du
                               D2C05GX3gumoSAVyAcDHn/xgej9pYRXGha4l+LKkFdGwAoXdV1z79EG1+9ns7wXuqMJFHM2KDpxAizV0GkZcojISvDwuh
                               vEAFdOJcqjyyH4FOGYa8usP1 jhon@example.com",
            }
        """
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = self.request.user
        name = serializer.validated_data['name']

        if core_models.SshPublicKey.objects.filter(user=user, name=name).exists():
            raise rf_serializers.ValidationError(
                {'name': [_('This field must be unique.')]}
            )

        serializer.save(user=user)


class ServiceSettingsViewSet(
    core_mixins.EagerLoadMixin, core_views.ReadOnlyActionsViewSet
):
    queryset = models.ServiceSettings.objects.filter().order_by('pk')
    serializer_class = serializers.ServiceSettingsSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.ServiceSettingsScopeFilterBackend,
        rf_filters.OrderingFilter,
    )
    filterset_class = filters.ServiceSettingsFilter
    lookup_field = 'uuid'
    ordering_fields = (
        'type',
        'name',
        'state',
    )


class BaseCounterView(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []
    extra_counters = {}
    dynamic_counters = set()

    @classmethod
    def register_dynamic_counter(cls, func):
        cls.dynamic_counters.add(func)

    def get_counters(self):
        counters = self.get_fields()
        for name, func in self.extra_counters.items():
            counters[name] = partial(func, self.object)
        return counters

    def list(self, request, uuid=None):
        result = {}
        counters = self.get_counters()
        for field, func in counters.items():
            result[field] = func()
        for func in self.dynamic_counters:
            result.update(func(self.object))
        fields = request.query_params.getlist('fields')
        if fields:
            result = {k: v for k, v in result.items() if k in fields}
        return Response(result)

    def get_fields(self):
        raise NotImplementedError()

    @cached_property
    def object(self):
        return self.get_object()


class CustomerCountersView(BaseCounterView):
    """
    Count number of entities related to customer

    .. code-block:: javascript

        {
            "projects": 1,
            "users": 3
        }
    """

    lookup_field = 'uuid'
    extra_counters = {}
    dynamic_counters = set()

    def get_queryset(self):
        return filter_queryset_for_user(
            models.Customer.objects.all().only('pk', 'uuid'), self.request.user
        )

    def get_fields(self):
        return {
            'projects': self.get_projects,
            'users': self.get_users,
        }

    def get_users(self):
        return count_customer_users(self.object)

    def get_projects(self):
        qs = models.Project.available_objects.filter(customer=self.object).only('pk')
        qs = filter_queryset_for_user(qs, self.request.user)
        return qs.count()

    def _total_count(self, models):
        return sum(self._count_model(model) for model in models)

    def _count_model(self, model):
        qs = model.objects.filter(customer=self.object).only('pk')
        qs = filter_queryset_for_user(qs, self.request.user)
        return qs.count()


class ProjectCountersView(BaseCounterView):
    """
    Count number of entities related to project

    .. code-block:: javascript

        {
            "users": 0,
        }
    """

    lookup_field = 'uuid'
    extra_counters = {}
    dynamic_counters = set()

    def get_queryset(self):
        return filter_queryset_for_user(
            models.Project.objects.all().only('pk', 'uuid'), self.request.user
        )

    def get_fields(self):
        fields = {
            'users': self.get_users,
        }
        return fields

    def get_users(self):
        return count_users(self.object)

    def _total_count(self, models):
        return sum(self._count_model(model) for model in models)

    def _count_model(self, model):
        qs = model.objects.filter(project=self.object).only('pk')
        qs = filter_queryset_for_user(qs, self.request.user)
        return qs.count()


class BaseServicePropertyViewSet(viewsets.ReadOnlyModelViewSet):
    filterset_class = filters.BaseServicePropertyFilter


def check_resource_backend_id(resource):
    if not resource.backend_id:
        raise ValidationError(_('Resource does not have backend ID.'))


class ResourceViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    """Basic view set for all resource view sets."""

    lookup_field = 'uuid'
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)
    unsafe_methods_permissions = [permissions.is_administrator]
    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.BaseResource.States.OK)
    ]
    destroy_validators = [
        core_validators.StateValidator(
            models.BaseResource.States.OK, models.BaseResource.States.ERRED
        )
    ]

    @action(detail=True, methods=['post'])
    def pull(self, request, uuid=None):
        if self.pull_executor == NotImplemented:
            return Response(
                {'detail': _('Pull operation is not implemented.')},
                status=status.HTTP_409_CONFLICT,
            )
        self.pull_executor.execute(self.get_object())
        return Response(
            {'detail': _('Pull operation was successfully scheduled.')},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_executor = NotImplemented
    pull_validators = [
        core_validators.StateValidator(
            models.BaseResource.States.OK, models.BaseResource.States.ERRED
        ),
        check_resource_backend_id,
    ]

    @action(detail=True, methods=['post'])
    def unlink(self, request, uuid=None):
        """
        Delete resource from the database without scheduling operations on backend
        and without checking current state of the resource. It is intended to be used
        for removing resource stuck in transitioning state.
        """
        obj = self.get_object()
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    unlink_permissions = [permissions.is_staff]


class DivisionViewSet(core_views.ReadOnlyActionsViewSet):
    permission_classes = ()
    queryset = models.Division.objects.all().order_by('name')
    serializer_class = serializers.DivisionSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.DivisionFilter


class DivisionTypesViewSet(core_views.ReadOnlyActionsViewSet):
    permission_classes = ()
    queryset = models.DivisionType.objects.all().order_by('name')
    serializer_class = serializers.DivisionTypesSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.DivisionTypesFilter


class UserAgreementsViewSet(ActionsViewSet):
    serializer_class = serializers.UserAgreementSerializer
    permission_classes = (core_permissions.ActionsPermission,)
    unsafe_methods_permissions = [permissions.is_staff]
    lookup_field = 'uuid'

    def get_queryset(self):
        queryset = models.UserAgreement.objects.all()
        agreement_type = self.request.query_params.get('agreement_type')
        if agreement_type is not None:
            queryset = queryset.filter(agreement_type=agreement_type)
        return queryset


class NotificationViewSet(ActionsViewSet):
    queryset = core_models.Notification.objects.all().order_by('id')
    serializer_class = serializers.NotificationSerializer
    permission_classes = (rf_permissions.IsAdminUser,)
    filterset_class = filters.NotificationFilter
    lookup_field = 'uuid'

    @action(detail=True, methods=['post'])
    def enable(self, request, uuid=None):
        notification: core_models.Notification = self.get_object()
        message = f"The notification {notification.key} has been enabled"
        if not notification.enabled:
            notification.enabled = True
            notification.save()
            logger.info(message)
        return Response(
            {'detail': _(message)},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def disable(self, request, uuid=None):
        notification: core_models.Notification = self.get_object()
        message = f"The notification {notification.key} has been disabled"
        if notification.enabled:
            notification.enabled = False
            notification.save()
            logger.info(message)
        return Response(
            {'detail': _(message)},
            status=status.HTTP_200_OK,
        )


class NotificationTemplateViewSet(ActionsViewSet):
    queryset = core_models.NotificationTemplate.objects.all()
    serializer_class = serializers.NotificationTemplateDetailSerializers
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.NotificationTemplateFilter

    @action(detail=True, methods=['post'])
    def override(self, request, uuid=None):
        template = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_content = serializer.validated_data['content']
        name = template.path
        message = f"The template {name} has been overridden"
        try:
            template_dbtemplates = Template.objects.get(name=name)
            template_dbtemplates.content = new_content
            template_dbtemplates.save()
            remove_cached_template(template_dbtemplates)
        except Template.DoesNotExist:
            raise NotFound('A template %s does not exist.' % name)
        logger.info(message)
        return Response({'detail': _(message)}, status=status.HTTP_200_OK)

    override_serializer_class = serializers.NotificationTemplateUpdateSerializers
    override_permissions = [permissions.is_staff]


class AuthTokenViewSet(ActionsViewSet):
    serializer_class = serializers.AuthTokenSerializers
    lookup_field = 'user_id'
    filter_backends = []
    disabled_actions = ['create', 'update', 'partial_update']
    permission_classes = (core_permissions.IsStaff,)

    def get_queryset(self):
        query = (
            'SELECT * FROM "authtoken_token" '
            'INNER JOIN "core_user" ON ("authtoken_token"."user_id" = "core_user"."id") '
            'WHERE (("authtoken_token"."created" >= '
            'NOW() - INTERVAL \'1 SECOND\' * "core_user"."token_lifetime")'
            ' OR "core_user"."token_lifetime" IS NULL)'
        )
        queryset = authtoken_models.Token.objects.raw(query)

        def get(user_id):
            try:
                users = list(
                    authtoken_models.Token.objects.raw(
                        query + ' AND "authtoken_token"."user_id" = %s', [user_id]
                    )
                )
            except DataError:
                raise authtoken_models.Token.DoesNotExist()

            if len(users):
                return users[0]
            else:
                raise authtoken_models.Token.DoesNotExist()

        queryset.get = get

        return queryset
