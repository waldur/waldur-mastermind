from __future__ import unicode_literals

import logging
import time
from collections import defaultdict
from functools import partial

from django.conf import settings as django_settings
from django.contrib import auth
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rf_filters
from rest_framework import mixins, views, viewsets, status
from rest_framework import permissions as rf_permissions
from rest_framework import serializers as rf_serializers
from rest_framework.decorators import detail_route, list_route
from rest_framework.exceptions import PermissionDenied, MethodNotAllowed, NotFound, APIException, ValidationError
from rest_framework.response import Response
from reversion.models import Version
import six

from waldur_core.core import managers as core_managers
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.utils import datetime_to_timestamp, sort_dict
from waldur_core.logging import models as logging_models
from waldur_core.logging.loggers import expand_alert_groups
from waldur_core.quotas.models import QuotaModelMixin, Quota
from waldur_core.structure import (
    SupportedServices, ServiceBackendError, ServiceBackendNotImplemented,
    filters, managers, models, permissions, serializers, utils)
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.metadata import ActionsMetadata
from waldur_core.structure.signals import resource_imported, structure_role_updated

logger = logging.getLogger(__name__)

User = auth.get_user_model()


class CustomerViewSet(core_mixins.EagerLoadMixin, viewsets.ModelViewSet):
    queryset = models.Customer.objects.all().order_by('name')
    serializer_class = serializers.CustomerSerializer
    lookup_field = 'uuid'
    filter_backends = (filters.GenericUserFilter,
                       filters.GenericRoleFilter,
                       DjangoFilterBackend,
                       rf_filters.OrderingFilter,
                       filters.AccountingStartDateFilter,
                       filters.ExternalCustomerFilterBackend,)
    ordering_fields = (
        'abbreviation',
        'accounting_start_date',
        'contact_details',
        'created',
        'name',
        'native_name',
        'registration_code',
    )
    filter_class = filters.CustomerFilter

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
        return super(CustomerViewSet, self).list(request, *args, **kwargs)

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
        return super(CustomerViewSet, self).retrieve(request, *args, **kwargs)

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
        return super(CustomerViewSet, self).create(request, *args, **kwargs)

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
        return super(CustomerViewSet, self).destroy(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.action == 'users':
            return serializers.CustomerUserSerializer
        return super(CustomerViewSet, self).get_serializer_class()

    def get_serializer_context(self):
        context = super(CustomerViewSet, self).get_serializer_context()
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
            customer.add_user(self.request.user, models.CustomerRole.OWNER, self.request.user)

        if django_settings.WALDUR_CORE.get('CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION', False):
            project = models.Project(
                name=_('First project'),
                description=_('First project we have created for you'),
                customer=customer,
            )
            project.save()

    def perform_update(self, serializer):
        self.check_customer_permissions(serializer.instance)
        utils.check_customer_blocked(serializer.instance)
        return super(CustomerViewSet, self).perform_update(serializer)

    def perform_destroy(self, instance):
        self.check_customer_permissions(instance)
        utils.check_customer_blocked(instance)

        core_signals.pre_delete_validate.send(
            sender=models.Customer,
            instance=instance,
            user=self.request.user
        )

        return super(CustomerViewSet, self).perform_destroy(instance)

    @detail_route(filter_backends=[filters.GenericRoleFilter])
    def users(self, request, uuid=None):
        """ A list of users connected to the customer. """
        customer = self.get_object()
        queryset = customer.get_users()
        # we need to handle filtration manually because we want to filter only customer users, not customers.
        filter_backend = filters.UserConcatenatedNameOrderingBackend()
        queryset = filter_backend.filter_queryset(request, queryset, self)
        queryset = self.paginate_queryset(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)


class ProjectTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.ProjectType.objects.all()
    serializer_class = serializers.ProjectTypeSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filter_class = filters.ProjectTypeFilter


class ProjectViewSet(core_mixins.EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Project.objects.all().order_by('name')
    serializer_class = serializers.ProjectSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.CustomerAccountingStartDateFilter,
    )
    filter_class = filters.ProjectFilter
    destroy_validators = partial_update_validators = [utils.check_customer_blocked]

    def get_serializer_context(self):
        context = super(ProjectViewSet, self).get_serializer_context()
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
        return super(ProjectViewSet, self).list(request, *args, **kwargs)

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
        return super(ProjectViewSet, self).retrieve(request, *args, **kwargs)

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
        return super(ProjectViewSet, self).create(request, *args, **kwargs)

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
        return super(ProjectViewSet, self).destroy(request, *args, **kwargs)

    def can_create_project_with(self, customer):
        user = self.request.user

        if user.is_staff:
            return True

        if customer.has_user(user, models.CustomerRole.OWNER):
            return True

        return False

    def get_queryset(self):
        user = self.request.user
        queryset = super(ProjectViewSet, self).get_queryset()

        can_manage = self.request.query_params.get('can_manage', None)
        if can_manage is not None:
            queryset = queryset.filter(
                Q(customer__permissions__user=user,
                  customer__permissions__role=models.CustomerRole.OWNER,
                  customer__permissions__is_active=True) |
                Q(permissions__user=user,
                  permissions__role=models.ProjectRole.MANAGER,
                  permissions__is_active=True)
            ).distinct()

        can_admin = self.request.query_params.get('can_admin', None)

        if can_admin is not None:
            queryset = queryset.filter(
                permissions__user=user,
                permissions__role=models.ProjectRole.ADMINISTRATOR,
                permissions__is_active=True
            )

        return queryset

    def perform_create(self, serializer):
        customer = serializer.validated_data['customer']

        if not self.can_create_project_with(customer):
            raise PermissionDenied()

        utils.check_customer_blocked(customer)

        customer.validate_quota_change({'nc_project_count': 1}, raise_exception=True)

        super(ProjectViewSet, self).perform_create(serializer)

    @detail_route(filter_backends=[filters.GenericRoleFilter])
    def users(self, request, uuid=None):
        """ A list of users connected to the project """
        project = self.get_object()
        queryset = project.get_users()
        # we need to handle filtration manually because we want to filter only project users, not projects.
        filter_backend = filters.UserConcatenatedNameOrderingBackend()
        queryset = filter_backend.filter_queryset(request, queryset, self)
        queryset = self.paginate_queryset(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)

    users_serializer_class = serializers.ProjectUserSerializer

    @detail_route(methods=['post'])
    def update_certifications(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        serialized_instance = serializers.ProjectSerializer(instance, context={'request': self.request})

        return Response(serialized_instance.data, status=status.HTTP_200_OK)

    update_certifications_serializer_class = serializers.ServiceCertificationsUpdateSerializer
    update_certifications_permissions = [permissions.is_owner]


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
    filter_class = filters.UserFilter

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
        return super(UserViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        User fields can be updated by account owner or user with staff privilege (is_staff=True).
        Following user fields can be updated:

        - organization (deprecated, use
          `organization plugin <http://waldur_core-organization.readthedocs.org/en/stable/>`_ instead)
        - full_name
        - native_name
        - job_title
        - phone_number
        - email

        Can be done by **PUT**ing a new data to the user URI, i.e. */api/users/<UUID>/* by staff user or account owner.
        Valid request example (token is user specific):

        .. code-block:: http

            PUT /api/users/e0c058d06864441fb4f1c40dee5dd4fd/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "email": "example@example.com",
                "organization": "Bells organization",
            }
        """
        return super(UserViewSet, self).retrieve(request, *args, **kwargs)

    @detail_route(methods=['post'])
    def password(self, request, uuid=None):
        """
        To change a user password, submit a **POST** request to the user's RPC URL, specifying new password
        by staff user or account owner.

        Password is expected to be at least 7 symbols long and contain at least one number
        and at least one lower or upper case.

        Example of a valid request:

        .. code-block:: http

            POST /api/users/e0c058d06864441fb4f1c40dee5dd4fd/password/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "password": "nQvqHzeP123",
            }
        """
        user = self.get_object()

        serializer = serializers.PasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_password = serializer.validated_data['password']
        user.set_password(new_password)
        user.save()

        return Response({'detail': _('Password has been successfully updated.')},
                        status=status.HTTP_200_OK)


class BasePermissionViewSet(viewsets.ModelViewSet):
    """
    This is a base class for both customer and project permissions.
    scope_field is required parameter, it should be either 'customer' or 'project'.
    quota_scope_field is optional parameter, it is used in order to validate quotas on permission creation.
    """

    scope_field = None
    quota_scope_field = None

    def perform_create(self, serializer):
        scope = serializer.validated_data[self.scope_field]
        role = serializer.validated_data['role']
        affected_user = serializer.validated_data['user']
        expiration_time = serializer.validated_data.get('expiration_time')

        if not scope.can_manage_role(self.request.user, role, expiration_time):
            raise PermissionDenied()

        utils.check_customer_blocked(scope)

        if self.quota_scope_field:
            quota_scope = getattr(scope, self.quota_scope_field)
        else:
            quota_scope = scope
        if not quota_scope.get_users().filter(pk=affected_user.pk).exists():
            quota_scope.validate_quota_change({'nc_user_count': 1}, raise_exception=True)

        super(BasePermissionViewSet, self).perform_create(serializer)

    def perform_update(self, serializer):
        permission = serializer.instance
        scope = getattr(permission, self.scope_field)
        role = permission.role

        utils.check_customer_blocked(scope)

        new_expiration_time = serializer.validated_data.get('expiration_time')
        old_expiration_time = permission.expiration_time
        if new_expiration_time == old_expiration_time:
            return

        if not scope.can_manage_role(self.request.user, role, new_expiration_time):
            raise PermissionDenied()

        serializer.save()
        structure_role_updated.send(
            sender=self.queryset.model,
            instance=permission,
            user=self.request.user,
        )

    def perform_destroy(self, instance):
        permission = instance
        scope = getattr(permission, self.scope_field)
        role = permission.role
        affected_user = permission.user
        expiration_time = permission.expiration_time

        if not scope.can_manage_role(self.request.user, role, expiration_time):
            raise PermissionDenied()

        utils.check_customer_blocked(scope)

        scope.remove_user(affected_user, role, removed_by=self.request.user)


class ProjectPermissionViewSet(BasePermissionViewSet):
    """
    - Projects are connected to customers, whereas the project may belong to one customer only,
      and the customer may have
      multiple projects.
    - Projects are connected to services, whereas the project may contain multiple services,
      and the service may belong to multiple projects.
    - Staff members can list all available projects of any customer and create new projects.
    - Customer owners can list all projects that belong to any of the customers they own.
      Customer owners can also create projects for the customers they own.
    - Project administrators can list all the projects they are administrators in.
    - Project managers can list all the projects they are managers in.
    """
    # See CustomerPermissionViewSet for implementation details.

    queryset = models.ProjectPermission.objects.filter(is_active=True).order_by('-created')
    serializer_class = serializers.ProjectPermissionSerializer
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend,)
    filter_class = filters.ProjectPermissionFilter
    scope_field = 'project'
    quota_scope_field = 'customer'

    def list(self, request, *args, **kwargs):
        """
        Project permissions expresses connection of user to a project.
        User may have either project manager or system administrator permission in the project.
        Use */api/project-permissions/* endpoint to maintain project permissions.

        Note that project permissions can be viewed and modified only by customer owners and staff users.

        To list all visible permissions, run a **GET** query against a list.
        Response will contain a list of project users and their brief data.

        To add a new user to the project, **POST** a new relationship to */api/project-permissions/* endpoint specifying
        project, user and the role of the user ('admin' or 'manager'):

        .. code-block:: http

            POST /api/project-permissions/ HTTP/1.1
            Accept: application/json
            Authorization: Token 95a688962bf68678fd4c8cec4d138ddd9493c93b
            Host: example.com

            {
                "project": "http://example.com/api/projects/6c9b01c251c24174a6691a1f894fae31/",
                "role": "manager",
                "user": "http://example.com/api/users/82cec6c8e0484e0ab1429412fe4194b7/"
            }
        """
        return super(ProjectPermissionViewSet, self).list(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        To remove a user from a project, delete corresponding connection (**url** field). Successful deletion
        will return status code 204.

        .. code-block:: http

            DELETE /api/project-permissions/42/ HTTP/1.1
            Authorization: Token 95a688962bf68678fd4c8cec4d138ddd9493c93b
            Host: example.com
        """
        return super(ProjectPermissionViewSet, self).destroy(request, *args, **kwargs)


class ProjectPermissionLogViewSet(mixins.RetrieveModelMixin,
                                  mixins.ListModelMixin,
                                  viewsets.GenericViewSet):
    queryset = models.ProjectPermission.objects.filter(is_active=None)
    serializer_class = serializers.ProjectPermissionLogSerializer
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend,)
    filter_class = filters.ProjectPermissionFilter


class CustomerPermissionViewSet(BasePermissionViewSet):
    """
    - Customers are connected to users through roles, whereas user may have role "customer owner".
    - Each customer may have multiple owners, and each user may own multiple customers.
    - Staff members can list all available customers and create new customers.
    - Customer owners can list all customers they own. Customer owners can also create new customers.
    - Project administrators can list all the customers that own any of the projects they are administrators in.
    - Project managers can list all the customers that own any of the projects they are managers in.
    """
    queryset = models.CustomerPermission.objects.filter(is_active=True).order_by('-created')
    serializer_class = serializers.CustomerPermissionSerializer
    filter_class = filters.CustomerPermissionFilter
    scope_field = 'customer'

    def get_queryset(self):
        queryset = super(CustomerPermissionViewSet, self).get_queryset()

        if not (self.request.user.is_staff or self.request.user.is_support):
            queryset = queryset.filter(
                Q(user=self.request.user, is_active=True) |
                Q(customer__projects__permissions__user=self.request.user, is_active=True) |
                Q(customer__permissions__user=self.request.user, is_active=True)
            ).distinct()

        return queryset

    def list(self, request, *args, **kwargs):
        """
        Each customer is associated with a group of users that represent customer owners. The link is maintained
        through **api/customer-permissions/** endpoint.

        To list all visible links, run a **GET** query against a list.
        Response will contain a list of customer owners and their brief data.

        To add a new user to the customer, **POST** a new relationship to **customer-permissions** endpoint:

        .. code-block:: http

            POST /api/customer-permissions/ HTTP/1.1
            Accept: application/json
            Authorization: Token 95a688962bf68678fd4c8cec4d138ddd9493c93b
            Host: example.com

            {
                "customer": "http://example.com/api/customers/6c9b01c251c24174a6691a1f894fae31/",
                "role": "owner",
                "user": "http://example.com/api/users/82cec6c8e0484e0ab1429412fe4194b7/"
            }
        """
        return super(CustomerPermissionViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        To remove a user from a customer owner group, delete corresponding connection (**url** field).
        Successful deletion will return status code 204.

        .. code-block:: http

            DELETE /api/customer-permissions/71/ HTTP/1.1
            Authorization: Token 95a688962bf68678fd4c8cec4d138ddd9493c93b
            Host: example.com
        """
        return super(CustomerPermissionViewSet, self).retrieve(request, *args, **kwargs)


class CustomerPermissionLogViewSet(mixins.RetrieveModelMixin,
                                   mixins.ListModelMixin,
                                   viewsets.GenericViewSet):
    queryset = models.CustomerPermission.objects.filter(is_active=None)
    serializer_class = serializers.CustomerPermissionLogSerializer
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend,)
    filter_class = filters.CustomerPermissionFilter


class CreationTimeStatsView(views.APIView):
    """
    Historical information about creation time of projects and customers.
    """

    def get(self, request, format=None):
        month = 60 * 60 * 24 * 30
        data = {
            'start_timestamp': request.query_params.get('from', int(time.time() - month)),
            'end_timestamp': request.query_params.get('to', int(time.time())),
            'segments_count': request.query_params.get('datapoints', 6),
            'model_name': request.query_params.get('type', 'customer'),
        }

        serializer = serializers.CreationTimeStatsSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        stats = serializer.get_stats(request.user)
        return Response(stats, status=status.HTTP_200_OK)

    def list(self, request, *args, **kwargs):
        """
        Available request parameters:

        - ?type=type_of_statistics_objects (required. Have to be from the list: 'customer', 'project')
        - ?from=timestamp (default: now - 30 days, for example: 1415910025)
        - ?to=timestamp (default: now, for example: 1415912625)
        - ?datapoints=how many data points have to be in answer (default: 6)

        Answer will be list of datapoints(dictionaries).
        Each datapoint will contain fields: 'to', 'from', 'value'.
        'Value' - count of objects, that were created between 'from' and 'to' dates.

        Example:

        .. code-block:: javascript

            [
                {"to": 471970877, "from": 1, "value": 5},
                {"to": 943941753, "from": 471970877, "value": 0},
                {"to": 1415912629, "from": 943941753, "value": 3}
            ]
        """
        return super(CreationTimeStatsView, self).list(request, *args, **kwargs)


class SshKeyViewSet(mixins.CreateModelMixin,
                    mixins.RetrieveModelMixin,
                    mixins.DestroyModelMixin,
                    mixins.ListModelMixin,
                    viewsets.GenericViewSet):
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
    filter_class = filters.SshKeyFilter

    def get_queryset(self):
        queryset = super(SshKeyViewSet, self).get_queryset()
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset

        return queryset.filter(Q(user=self.request.user) | Q(is_shared=True))

    def perform_destroy(self, instance):
        if instance.is_shared and not self.request.user.is_staff:
            raise PermissionDenied(_('Only staff users are allowed to delete shared SSH public key.'))
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
        return super(SshKeyViewSet, self).list(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = self.request.user
        name = serializer.validated_data['name']

        if core_models.SshPublicKey.objects.filter(user=user, name=name).exists():
            raise rf_serializers.ValidationError({'name': [_('This field must be unique.')]})

        serializer.save(user=user)


class ServiceSettingsViewSet(core_mixins.EagerLoadMixin,
                             core_views.ActionsViewSet):
    queryset = models.ServiceSettings.objects.filter().order_by('pk')
    serializer_class = serializers.ServiceSettingsSerializer
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend,
                       filters.ServiceSettingsScopeFilterBackend,
                       rf_filters.OrderingFilter)
    filter_class = filters.ServiceSettingsFilter
    lookup_field = 'uuid'
    ordering_fields = ('type', 'name', 'state',)
    disabled_actions = ['create', 'destroy']

    def list(self, request, *args, **kwargs):
        """
        To get a list of service settings, run **GET** against */api/service-settings/* as an authenticated user.
        Only settings owned by this user or shared settings will be listed.

        Supported filters are:

        - ?name=<text> - partial matching used for searching
        - ?type=<type> - choices: OpenStack, DigitalOcean, Amazon, JIRA, GitLab, Oracle
        - ?state=<state> - choices: New, Creation Scheduled, Creating, Sync Scheduled, Syncing, In Sync, Erred
        - ?shared=<bool> - allows to filter shared service settings
        """
        return super(ServiceSettingsViewSet, self).list(request, *args, **kwargs)

    def can_user_update_settings(request, view, obj=None):
        """ Only staff can update shared settings, otherwise user has to be an owner of the settings."""
        if obj is None:
            return

        # TODO [TM:3/21/17] clean it up after WAL-634. Clean up service settings update tests as well.
        if obj.customer and not obj.shared:
            return permissions.is_owner(request, view, obj)
        else:
            return permissions.is_staff(request, view, obj)

    def update(self, request, *args, **kwargs):
        """
        To update service settings, issue a **PUT** or **PATCH** to */api/service-settings/<uuid>/* as a customer owner.
        You are allowed to change name and credentials only.

        Example of a request:

        .. code-block:: http

            PATCH /api/service-settings/9079705c17d64e6aa0af2e619b0e0702/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "username": "admin",
                "password": "new_secret"
            }
        """
        return super(ServiceSettingsViewSet, self).update(request, *args, **kwargs)

    update_permissions = partial_update_permissions = [can_user_update_settings,
                                                       permissions.check_access_to_services_management]

    update_validators = partial_update_validators = [utils.check_customer_blocked]

    @detail_route()
    def stats(self, request, uuid=None):
        """
        This endpoint returns allocation of resources for current service setting.
        Answer is service-specific dictionary. Example output for OpenStack:

        * vcpu - maximum number of vCPUs (from hypervisors)
        * vcpu_quota - maximum number of vCPUs(from quotas)
        * vcpu_usage - current number of used vCPUs

        * ram - total size of memory for allocation (from hypervisors)
        * ram_quota - maximum number of memory (from quotas)
        * ram_usage - currently used memory size on all physical hosts

        * storage - total available disk space on all physical hosts (from hypervisors)
        * storage_quota - maximum number of storage (from quotas)
        * storage_usage - currently used storage on all physical hosts

        {
            'vcpu': 10,
            'vcpu_quota': 7,
            'vcpu_usage': 5,
            'ram': 1000,
            'ram_quota': 700,
            'ram_usage': 500,
            'storage': 10000,
            'storage_quota': 7000,
            'storage_usage': 5000
        }
        """

        service_settings = self.get_object()
        backend = service_settings.get_backend()

        try:
            stats = backend.get_stats()
        except ServiceBackendNotImplemented:
            stats = {}

        return Response(stats, status=status.HTTP_200_OK)

    @detail_route(methods=['post'])
    def update_certifications(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        serialized_instance = serializers.ServiceSettingsSerializer(instance, context={'request': self.request})

        return Response(serialized_instance.data, status=status.HTTP_200_OK)

    update_certifications_serializer_class = serializers.ServiceCertificationsUpdateSerializer
    update_certifications_permissions = [
        can_user_update_settings,
        permissions.check_access_to_services_management
    ]


class ServiceMetadataViewSet(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []

    def list(self, request):
        """
        To get a list of supported service types, run **GET** against */api/service-metadata/* as an authenticated user.
        Use an endpoint from the returned list in order to create new service.
        """
        return Response(SupportedServices.get_services_with_resources(request))


class ResourceSummaryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Use */api/resources/* to get a list of all the resources of any type that a user can see.
    """
    model = models.NewResource  # for permissions definition.
    serializer_class = serializers.SummaryResourceSerializer
    filter_backends = (filters.GenericRoleFilter, filters.ResourceSummaryFilterBackend, filters.TagsFilter)

    def get_queryset(self):
        resource_models = {k: v for k, v in SupportedServices.get_resource_models().items()}
        resource_models = self._filter_by_category(resource_models)
        resource_models = self._filter_by_types(resource_models)
        resource_models = self._filter_resources(resource_models)

        queryset = managers.ResourceSummaryQuerySet(resource_models.values())
        return serializers.SummaryResourceSerializer.eager_load(queryset, self.request)

    def _filter_by_types(self, resource_models):
        types = self.request.query_params.getlist('resource_type', None)
        if types:
            resource_models = {k: v for k, v in resource_models.items() if k in types}
        return resource_models

    def _filter_by_category(self, resource_models):
        choices = {
            'apps': models.ApplicationMixin.get_all_models(),
            'vms': models.VirtualMachine.get_all_models(),
            'private_clouds': models.PrivateCloud.get_all_models(),
            'storages': models.Storage.get_all_models(),
            'volumes': models.Volume.get_all_models(),
            'snapshots': models.Snapshot.get_all_models(),
        }
        category = self.request.query_params.get('resource_category')
        if not category:
            return resource_models

        category_models = choices.get(category)
        if category_models:
            return {k: v for k, v in resource_models.items() if v in category_models}
        return {}

    def _filter_resources(self, resource_models):
        return {k: v for k, v in resource_models.items()
                if v in models.ResourceMixin.get_all_models()}

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        """
        To get a list of supported resources' actions, run **OPTIONS** against
        */api/<resource_url>/* as an authenticated user.

        It is possible to filter and order by resource-specific fields, but this filters will be applied only to
        resources that support such filtering. For example it is possible to sort resource by ?o=ram, but SugarCRM crms
        will ignore this ordering, because they do not support such option.

        Filter resources by type or category
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

        There are two query argument to select resources by their type.

        - Specify explicitly list of resource types, for example:

          /api/<resource_endpoint>/?resource_type=DigitalOcean.Droplet&resource_type=OpenStack.Instance

        - Specify category, one of vms, apps, private_clouds or storages for example:

          /api/<resource_endpoint>/?category=vms

        Filtering by monitoring fields
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

        Resources may have SLA attached to it. Example rendering of SLA:

        .. code-block:: javascript

            "sla": {
                "value": 95.0
                "agreed_value": 99.0,
                "period": "2016-03"
            }

        You may filter or order resources by SLA. Default period is current year and month.

        - Example query for filtering list of resources by actual SLA:

          /api/<resource_endpoint>/?actual_sla=90&period=2016-02

        - Warning! If resource does not have SLA attached to it, it is not included in ordered response.
          Example query for ordering list of resources by actual SLA:

          /api/<resource_endpoint>/?o=actual_sla&period=2016-02

        Service list is displaying current SLAs for each of the items. By default,
        SLA period is set to the current month. To change the period pass it as a query argument:

        - ?period=YYYY-MM - return a list with SLAs for a given month
        - ?period=YYYY - return a list with SLAs for a given year

        In all cases all currently running resources are returned, if SLA for the given period is
        not known or not present, it will be shown as **null** in the response.

        Resources may have monitoring items attached to it. Example rendering of monitoring items:

        .. code-block:: javascript

            "monitoring_items": {
               "application_state": 1
            }

        You may filter or order resources by monitoring item.

        - Example query for filtering list of resources by installation state:

          /api/<resource_endpoint>/?monitoring__installation_state=1

        - Warning! If resource does not have monitoring item attached to it, it is not included in ordered response.
          Example query for ordering list of resources by installation state:

          /api/<resource_endpoint>/?o=monitoring__installation_state

        Filtering by tags
        ^^^^^^^^^^^^^^^^^

        Resource may have tags attached to it. Example of tags rendering:

        .. code-block:: javascript

            "tags": [
                "license-os:centos7",
                "os-family:linux",
                "license-application:postgresql",
                "support:premium"
            ]

        Tags filtering:

         - ?tag=IaaS - filter by full tag name, using method OR. Can be list.
         - ?rtag=os-family:linux - filter by full tag name, using AND method. Can be list.
         - ?tag__license-os=centos7 - filter by tags with particular prefix.

        Tags ordering:

         - ?o=tag__license-os - order by tag with particular prefix. Instances without given tag will not be returned.
        """

        return super(ResourceSummaryViewSet, self).list(request, *args, **kwargs)

    @list_route()
    def count(self, request):
        """
        Count resources by type. Example output:

        .. code-block:: javascript

            {
                "Amazon.Instance": 0,
                "GitLab.Project": 3,
                "Azure.VirtualMachine": 0,
                "DigitalOcean.Droplet": 0,
                "OpenStack.Instance": 0,
                "GitLab.Group": 8
            }
        """
        queryset = self.filter_queryset(self.get_queryset())
        return Response({SupportedServices.get_name_for_model(qs.model): qs.count()
                         for qs in queryset.querysets})


class ServicesViewSet(mixins.ListModelMixin,
                      viewsets.GenericViewSet):
    """ The summary list of all user services. """

    model = models.Service
    serializer_class = serializers.SummaryServiceSerializer
    filter_backends = (filters.GenericRoleFilter, filters.ServiceSummaryFilterBackend)

    def get_queryset(self):
        service_models = {k: v['service'] for k, v in SupportedServices.get_service_models().items()}
        service_models = self._filter_by_types(service_models)
        # TODO: filter models by service type.
        queryset = managers.ServiceSummaryQuerySet(service_models.values())
        return serializers.SummaryServiceSerializer.eager_load(queryset, self.request)

    def _filter_by_types(self, service_models):
        types = self.request.query_params.getlist('service_type', None)
        if types:
            service_models = {k: v for k, v in service_models.items() if k in types}
        return service_models

    def list(self, request, *args, **kwargs):
        """
        Filter services by type
        ^^^^^^^^^^^^^^^^^^^^^^^

        It is possible to filter services by their types. Example:

          /api/services/?service_type=DigitalOcean&service_type=OpenStack
        """
        return super(ServicesViewSet, self).list(request, *args, **kwargs)


class BaseCounterView(viewsets.GenericViewSet):
    # Fix for schema generation
    queryset = []
    extra_counters = {}
    dynamic_counters = set()

    @classmethod
    def register_counter(cls, name, func):
        cls.extra_counters[name] = func

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

    def _get_alerts(self, aggregate_by):
        alert_types_to_exclude = expand_alert_groups(self.request.query_params.getlist('exclude_features'))
        return filters.filter_alerts_by_aggregate(
            logging_models.Alert.objects,
            aggregate_by,
            self.request.user,
            self.object.uuid.hex,
        ).filter(closed__isnull=True).exclude(alert_type__in=alert_types_to_exclude).count()

    @cached_property
    def object(self):
        return self.get_object()


class CustomerCountersView(BaseCounterView):
    """
    Count number of entities related to customer

    .. code-block:: javascript

        {
            "alerts": 12,
            "services": 1,
            "projects": 1,
            "users": 3
        }
    """
    lookup_field = 'uuid'
    extra_counters = {}
    dynamic_counters = set()

    def get_queryset(self):
        return filter_queryset_for_user(models.Customer.objects.all().only('pk', 'uuid'), self.request.user)

    def get_fields(self):
        return {
            'alerts': self.get_alerts,
            'projects': self.get_projects,
            'services': self.get_services,
            'users': self.get_users
        }

    def get_alerts(self):
        return self._get_alerts('customer')

    def get_users(self):
        return self.object.get_users().count()

    def get_projects(self):
        return self._count_model(models.Project)

    def get_services(self):
        models = [item['service'] for item in SupportedServices.get_service_models().values()]
        return self._total_count(models)

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
            "alerts": 2,
            "apps": 0,
            "vms": 1,
            "private_clouds": 1,
            "storages": 2,
        }
    """
    lookup_field = 'uuid'
    extra_counters = {}
    dynamic_counters = set()

    def get_queryset(self):
        return filter_queryset_for_user(models.Project.objects.all().only('pk', 'uuid'), self.request.user)

    def get_fields(self):
        fields = {
            'alerts': self.get_alerts,
            'vms': self.get_vms,
            'apps': self.get_apps,
            'private_clouds': self.get_private_clouds,
            'storages': self.get_storages,
            'users': self.get_users
        }
        return fields

    def get_alerts(self):
        return self._get_alerts('project')

    def get_vms(self):
        return self._total_count(models.VirtualMachine.get_all_models())

    def get_apps(self):
        return self._total_count(models.ApplicationMixin.get_all_models())

    def get_private_clouds(self):
        return self._total_count(models.PrivateCloud.get_all_models())

    def get_storages(self):
        return self._total_count(models.Storage.get_all_models())

    def get_users(self):
        return self.object.get_users().count()

    def _total_count(self, models):
        return sum(self._count_model(model) for model in models)

    def _count_model(self, model):
        qs = model.objects.filter(project=self.object).only('pk')
        qs = filter_queryset_for_user(qs, self.request.user)
        return qs.count()


class UserCountersView(BaseCounterView):
    """
    Count number of entities related to current user

    .. code-block:: javascript

        {
            "keys": 1,
            "hooks": 1
        }
    """

    def get_fields(self):
        return {
            'keys': self.get_keys,
            'hooks': self.get_hooks
        }

    def get_keys(self):
        return core_models.SshPublicKey.objects.filter(user_uuid=self.request.user.uuid.hex).count()

    def get_hooks(self):
        return core_managers.SummaryQuerySet(logging_models.BaseHook.get_all_models()).count()


class BaseServiceViewSet(core_mixins.EagerLoadMixin, core_views.ActionsViewSet):
    queryset = NotImplemented
    serializer_class = NotImplemented
    import_serializer_class = NotImplemented
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.BaseServiceFilter
    lookup_field = 'uuid'
    metadata_class = ActionsMetadata
    unsafe_methods_permissions = [permissions.is_owner, permissions.check_access_to_services_management]

    def list(self, request, *args, **kwargs):
        """
        To list all services without regard to its type, run **GET** against */api/services/* as an authenticated user.

        To list services of specific type issue **GET** to specific endpoint from a list above as a customer owner.
        Individual endpoint used for every service type.

        To create a service, issue a **POST** to specific endpoint from a list above as a customer owner.
        Individual endpoint used for every service type.

        You can create service based on shared service settings. Example:

        .. code-block:: http

            POST /api/digitalocean/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "Common DigitalOcean",
                "customer": "http://example.com/api/customers/1040561ca9e046d2b74268600c7e1105/",
                "settings": "http://example.com/api/service-settings/93ba615d6111466ebe3f792669059cb4/"
            }

        Or provide your own credentials. Example:

        .. code-block:: http

            POST /api/oracle/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "My Oracle",
                "customer": "http://example.com/api/customers/1040561ca9e046d2b74268600c7e1105/",
                "backend_url": "https://oracle.example.com:7802/em",
                "username": "admin",
                "password": "secret"
            }
        """
        return super(BaseServiceViewSet, self).list(request, *args, **kwargs)

    def _can_import(self):
        return self.import_serializer_class is not NotImplemented

    def get_serializer_class(self):
        serializer = super(BaseServiceViewSet, self).get_serializer_class()
        if self.action == 'link':
            serializer = self.import_serializer_class if self._can_import() else rf_serializers.Serializer

        return serializer

    def get_serializer_context(self):
        context = super(BaseServiceViewSet, self).get_serializer_context()
        # Viewset doesn't have object during schema generation
        if self.action == 'link' and self.lookup_field in self.kwargs:
            context['service'] = self.get_object()
        return context

    def get_import_context(self):
        return {}

    @detail_route()
    def managed_resources(self, request, uuid=None):
        service = self.get_object()
        backend = self.get_backend(service)

        try:
            resources = backend.get_managed_resources()
        except ServiceBackendNotImplemented:
            resources = []

        serializer = serializers.ManagedResourceSerializer(resources, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _has_import_serializer_permission(request, view, obj=None):
        if not view._can_import():
            raise MethodNotAllowed(view.action)

    def _require_staff_for_shared_settings(request, view, obj=None):
        """ Allow to execute action only if service settings are not shared or user is staff """
        if obj is None:
            return

        if obj.settings.shared and not request.user.is_staff:
            raise PermissionDenied(_('Only staff users are allowed to import resources from shared services.'))

    @detail_route(methods=['get', 'post'])
    def link(self, request, uuid=None):
        """
        To get a list of resources available for import, run **GET** against */<service_endpoint>/link/*
        as an authenticated user.
        Optionally project_uuid parameter can be supplied for services requiring it like OpenStack.

        To import (link with Waldur) resource issue **POST** against the same endpoint with resource id.

        .. code-block:: http

            POST /api/openstack/08039f01c9794efc912f1689f4530cf0/link/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "backend_id": "bd5ec24d-9164-440b-a9f2-1b3c807c5df3",
                "project": "http://example.com/api/projects/e5f973af2eb14d2d8c38d62bcbaccb33/"
            }
        """

        service = self.get_object()

        if self.request.method == 'GET':
            try:
                backend = self.get_backend(service)
                try:
                    resources = backend.get_resources_for_import(**self.get_import_context())
                except ServiceBackendNotImplemented:
                    resources = []

                page = self.paginate_queryset(resources)
                if page is not None:
                    return self.get_paginated_response(page)

                return Response(resources)
            except (ServiceBackendError, ValidationError) as e:
                raise APIException(e)

        else:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            try:
                resource = serializer.save()
            except ServiceBackendError as e:
                raise APIException(e)

            resource_imported.send(
                sender=resource.__class__,
                instance=resource,
            )

            return Response(serializer.data, status=status.HTTP_200_OK)

    link_permissions = [_has_import_serializer_permission, _require_staff_for_shared_settings]

    def get_backend(self, service):
        # project_uuid can be supplied in order to get a list of resources
        # available for import (link) based on project, depends on backend implementation
        project_uuid = self.request.query_params.get('project_uuid')
        if project_uuid:
            spl_class = SupportedServices.get_related_models(service)['service_project_link']
            try:
                spl = spl_class.objects.get(project__uuid=project_uuid, service=service)
            except spl_class.DoesNotExist:
                raise NotFound(_("Can't find project %s.") % project_uuid)
            else:
                return spl.get_backend()
        else:
            return service.get_backend()

    @detail_route(methods=['post'])
    def unlink(self, request, uuid=None):
        """
        Unlink all related resources, service project link and service itself.
        """
        service = self.get_object()
        service.unlink_descendants()
        self.perform_destroy(service)

        return Response(status=status.HTTP_204_NO_CONTENT)

    unlink_permissions = [
        _require_staff_for_shared_settings,
        permissions.check_access_to_services_management
    ]
    unlink.destructive = True


class BaseServiceProjectLinkViewSet(core_views.ActionsViewSet):
    queryset = NotImplemented
    serializer_class = NotImplemented
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.BaseServiceProjectLinkFilter
    unsafe_methods_permissions = [permissions.is_owner]
    disabled_actions = ['update', 'partial_update']

    def list(self, request, *args, **kwargs):
        """
        To get a list of connections between a project and an service, run **GET** against service_project_link_url
        as authenticated user. Note that a user can only see connections of a project where a user has a role.

        If service has `available_for_all` flag, project-service connections are created automatically.
        Otherwise, in order to be able to provision resources, service must first be linked to a project.
        To do that, **POST** a connection between project and a service to service_project_link_url
        as stuff user or customer owner.
        """
        return super(BaseServiceProjectLinkViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        To remove a link, issue **DELETE** to URL of the corresponding connection as stuff user or customer owner.
        """
        return super(BaseServiceProjectLinkViewSet, self).retrieve(request, *args, **kwargs)


class ResourceViewMetaclass(type):
    """ Store view in registry """
    def __new__(cls, name, bases, args):
        resource_view = super(ResourceViewMetaclass, cls).__new__(cls, name, bases, args)
        queryset = args.get('queryset')
        if hasattr(queryset, 'model') and not issubclass(queryset.model, models.SubResource):
            SupportedServices.register_resource_view(queryset.model, resource_view)
        return resource_view


class BaseServicePropertyViewSet(viewsets.ReadOnlyModelViewSet):
    filter_class = filters.BaseServicePropertyFilter


class AggregatedStatsView(views.APIView):
    """
    Quotas and quotas usage aggregated by projects/customers.

    Available request parameters:
        - ?aggregate=aggregate_model_name (default: 'customer'.
          Have to be from list: 'customer', 'project')
        - ?uuid=uuid_of_aggregate_model_object (not required. If this parameter will be defined -
          result will contain only object with given uuid)
        - ?quota_name - optional list of quota names, for example ram, vcpu, storage
    """

    def get(self, request, format=None):
        serializer = serializers.AggregateSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        quota_names = request.query_params.getlist('quota_name')
        if len(quota_names) == 0:
            quota_names = None
        querysets = serializer.get_service_project_links(request.user)

        total_sum = QuotaModelMixin.get_sum_of_quotas_for_querysets(querysets, quota_names)
        total_sum = sort_dict(total_sum)
        return Response(total_sum, status=status.HTTP_200_OK)


class QuotaTimelineStatsView(views.APIView):
    """
    Historical data of quotas and quotas usage aggregated by projects/customers.

    Available request parameters:

    - ?from=timestamp (default: now - 1 day, for example: 1415910025)
    - ?to=timestamp (default: now, for example: 1415912625)
    - ?interval (default: day. Has to be from list: hour, day, week, month)
    - ?item=<quota_name>. If this parameter is not defined - endpoint will return data for all items.
    - ?aggregate=aggregate_model_name (default: 'customer'. Have to be from list: 'customer', 'project')
    - ?uuid=uuid_of_aggregate_model_object (not required. If this parameter is defined, result will contain only object with given uuid)

    Answer will be list of dictionaries with fields, determining time frame. It's size is equal to interval parameter.
    Values within each bucket are averaged for each project and then all projects metrics are summarized.

    Value fields include:

    - vcpu_limit - virtual CPUs quota
    - vcpu_usage - virtual CPUs usage
    - ram_limit - RAM quota limit, in MiB
    - ram_usage - RAM usage, in MiB
    - storage_limit - volume storage quota limit, in MiB
    - storage_usage - volume storage quota consumption, in MiB
    """

    def get(self, request, format=None):
        scopes = self.get_quota_scopes(request)
        ranges = self.get_ranges(request)
        items = request.query_params.getlist('item') or self.get_all_spls_quotas()

        collector = QuotaTimelineCollector()
        for item in items:
            for scope in scopes:
                values = self.get_stats_for_scope(item, scope, ranges)
                for (end, start), (limit, usage) in zip(ranges, values):
                    collector.add_quota(start, end, item, limit, usage)

        stats = list(map(sort_dict, collector.to_dict()))[::-1]
        return Response(stats, status=status.HTTP_200_OK)

    def get_quota_scopes(self, request):
        serializer = serializers.AggregateSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        scopes = sum([list(qs) for qs in serializer.get_service_project_links(request.user)], [])
        # XXX: quick and dirty hack for OpenStack: use tenants instead of SPLs as quotas scope.
        new_scopes = []
        for index, scope in enumerate(scopes):
            if scope.service.settings.type == 'OpenStack':
                new_scopes += list(scope.tenants.all())
            else:
                new_scopes.append(scope)
        return new_scopes

    def get_all_spls_quotas(self):
        # XXX: quick and dirty hack for OpenStack: use tenants instead of SPLs as quotas scope.
        spl_models = [m if m.__name__ != 'OpenStackServiceProjectLink' else m.tenants.field.model
                      for m in models.ServiceProjectLink.get_all_models()]
        return sum([spl_model.get_quotas_names() for spl_model in spl_models], [])

    def get_stats_for_scope(self, quota_name, scope, dates):
        stats_data = []
        try:
            quota = scope.quotas.get(name=quota_name)
        except Quota.DoesNotExist:
            return stats_data
        versions = Version.objects.get_for_object(quota).select_related('revision').filter(
            revision__date_created__lte=dates[0][0]).iterator()
        version = None
        for end, start in dates:
            try:
                while version is None or version.revision.date_created > end:
                    version = next(versions)
                stats_data.append((version._object_version.object.limit,
                                   version._object_version.object.usage))
            except StopIteration:
                break

        return stats_data

    def get_ranges(self, request):
        mapped = {
            'start_time': request.query_params.get('from'),
            'end_time': request.query_params.get('to'),
            'interval': request.query_params.get('interval')
        }
        data = {key: val for (key, val) in mapped.items() if val}

        serializer = core_serializers.TimelineSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        date_points = serializer.get_date_points()
        reversed_dates = date_points[::-1]
        ranges = list(zip(reversed_dates[:-1], reversed_dates[1:]))
        return ranges


class QuotaTimelineCollector(object):
    """
    Helper class for QuotaTimelineStatsView.
    Aggregate quotas grouped by date range and quota name.
    Example output rendering:

    .. code-block:: javascript

        [
            {
                "from": start,
                "to" end,
                "vcpu_limit": 10,
                "vcpu_usage": 5,
                "ram_limit": 4000,
                "ran_usage": 1000
            }
        ]
    """

    def __init__(self):
        self.ranges = set()
        self.items = set()
        self.limits = defaultdict(int)
        self.usages = defaultdict(int)

    def add_quota(self, start, end, item, limit, usage):
        key = (start, end, item)
        if limit == -1 or self.limits[key] == -1:
            self.limits[key] = -1
        else:
            self.limits[key] += limit
        self.usages[key] += usage
        self.ranges.add((start, end))
        self.items.add(item)

    def to_dict(self):
        table = []
        for start, end in sorted(self.ranges):
            row = {
                'from': datetime_to_timestamp(start),
                'to': datetime_to_timestamp(end)
            }
            for item in sorted(self.items):
                key = (start, end, item)
                row['%s_limit' % item] = self.limits[key]
                row['%s_usage' % item] = self.usages[key]
            table.append(row)
        return table


def check_resource_backend_id(resource):
    if not resource.backend_id:
        raise ValidationError(_('Resource does not have backend ID.'))


class ResourceViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    """ Basic view set for all resource view sets. """
    lookup_field = 'uuid'
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)
    metadata_class = ActionsMetadata
    unsafe_methods_permissions = [permissions.is_administrator]
    update_validators = partial_update_validators = [core_validators.StateValidator(models.NewResource.States.OK)]
    destroy_validators = [core_validators.StateValidator(models.NewResource.States.OK, models.NewResource.States.ERRED)]

    @detail_route(methods=['post'])
    def pull(self, request, uuid=None):
        if not self.pull_executor:
            return Response({'detail': _('Pull operation is not implemented.')},
                            status=status.HTTP_409_CONFLICT)
        self.pull_executor.execute(self.get_object())
        return Response({'detail': _('Pull operation was successfully scheduled.')}, status=status.HTTP_202_ACCEPTED)

    pull_executor = NotImplemented
    pull_validators = [
        core_validators.StateValidator(models.NewResource.States.OK, models.NewResource.States.ERRED),
        check_resource_backend_id,
    ]


class BaseResourceViewSet(six.with_metaclass(ResourceViewMetaclass, ResourceViewSet)):
    pass


class ImportableResourceViewSet(BaseResourceViewSet):
    """
    This viewset enables uniform implementation of resource import.

    Comparing to the previous approach when import endpoint was defined in service viewset,
    this class assumes that resource-specific import options are defined in resource viewset.

    Consider the following example:

    importable_resources_backend_method = 'get_tenants_for_import'
    importable_resources_serializer_class = serializers.TenantImportableSerializer
    importable_resources_permissions = [structure_permissions.is_staff]
    import_resource_serializer_class = serializers.TenantImportSerializer
    import_resource_permissions = [structure_permissions.is_staff]
    import_resource_executor = executors.TenantImportExecutor

    Note that there are only 3 mandatory parameters:
    * importable_resources_backend_method
    * importable_resources_serializer_class
    * import_resource_serializer_class
    """
    import_resource_executor = None

    @list_route(methods=['get'])
    def importable_resources(self, request):
        serializer = self.get_serializer(data=request.GET)
        serializer.is_valid(raise_exception=True)
        service_project_link = serializer.validated_data['service_project_link']

        backend = service_project_link.get_backend()
        resources = getattr(backend, self.importable_resources_backend_method)()
        serializer = self.get_serializer(resources, many=True)
        page = self.paginate_queryset(serializer.data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data=serializer.data, status=status.HTTP_200_OK)

    @list_route(methods=['post'])
    def import_resource(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            resource = serializer.save()
        except IntegrityError:
            raise rf_serializers.ValidationError(_('Resource is already registered.'))
        else:
            resource_imported.send(
                sender=resource.__class__,
                instance=resource,
            )
        if self.import_resource_executor:
            self.import_resource_executor.execute(resource)

        return Response(data=serializer.data, status=status.HTTP_201_CREATED)


class ServiceCertificationViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    metadata_class = ActionsMetadata
    unsafe_methods_permissions = [permissions.is_staff]
    serializer_class = serializers.ServiceCertificationSerializer
    queryset = models.ServiceCertification.objects.all()


class DivisionViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Division.objects.all().order_by('name')
    serializer_class = serializers.DivisionSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    filter_class = filters.DivisionFilter
