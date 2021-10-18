import logging
from functools import partial

from django.conf import settings as django_settings
from django.contrib import auth
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as rf_filters
from rest_framework import mixins
from rest_framework import permissions as rf_permissions
from rest_framework import serializers as rf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.core import managers as core_managers
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import signals as core_signals
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.utils import is_uuid_like
from waldur_core.logging import models as logging_models
from waldur_core.structure import filters, models, permissions, serializers, utils
from waldur_core.structure.executors import ServiceSettingsCreateExecutor
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.signals import structure_role_updated

logger = logging.getLogger(__name__)

User = auth.get_user_model()


class CustomerViewSet(core_mixins.EagerLoadMixin, viewsets.ModelViewSet):
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
        utils.check_customer_blocked(serializer.instance)
        return super(CustomerViewSet, self).perform_update(serializer)

    def perform_destroy(self, instance):
        self.check_customer_permissions(instance)
        utils.check_customer_blocked(instance)

        core_signals.pre_delete_validate.send(
            sender=models.Customer, instance=instance, user=self.request.user
        )

        return super(CustomerViewSet, self).perform_destroy(instance)

    @action(
        detail=True, filter_backends=[filters.GenericRoleFilter],
    )
    def users(self, request, uuid=None):
        """ A list of users connected to the customer. """
        customer = self.get_object()
        queryset = customer.get_users()
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


class ProjectViewSet(core_mixins.EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Project.objects.all().order_by('name')
    serializer_class = serializers.ProjectSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.CustomerAccountingStartDateFilter,
    )
    filterset_class = filters.ProjectFilter
    partial_update_validators = [utils.check_customer_blocked]
    destroy_validators = [utils.check_customer_blocked, utils.project_is_empty]

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
                Q(
                    customer__permissions__user=user,
                    customer__permissions__role=models.CustomerRole.OWNER,
                    customer__permissions__is_active=True,
                )
                | Q(
                    permissions__user=user,
                    permissions__role=models.ProjectRole.MANAGER,
                    permissions__is_active=True,
                )
            ).distinct()

        can_admin = self.request.query_params.get('can_admin', None)

        if can_admin is not None:
            queryset = queryset.filter(
                permissions__user=user,
                permissions__role=models.ProjectRole.ADMINISTRATOR,
                permissions__is_active=True,
            )

        return queryset

    def perform_create(self, serializer):
        customer = serializer.validated_data['customer']

        if not self.can_create_project_with(customer):
            raise PermissionDenied()

        utils.check_customer_blocked(customer)

        super(ProjectViewSet, self).perform_create(serializer)

    @action(detail=True, filter_backends=[filters.GenericRoleFilter])
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

    @action(detail=True, methods=['post'])
    def move_project(self, request, uuid=None):
        project = self.get_object()
        serializer = self.get_serializer(project, data=request.data)
        serializer.is_valid(raise_exception=True)

        customer = serializer.validated_data['customer']

        utils.move_project(project, customer)
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

    @action(detail=True, methods=['post'])
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

        return Response(
            {'detail': _('Password has been successfully updated.')},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def change_email(self, request, uuid=None):
        user = self.get_object()

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
        super(UserViewSet, self).check_permissions(request)

    @action(detail=False, methods=['get'])
    def me(self, request):
        serializer = self.get_serializer(request.user)

        return Response(serializer.data, status=status.HTTP_200_OK,)

    @action(detail=True, methods=['post'])
    def pull_remote_user(self, request, uuid=None):
        user = self.get_object()
        if user.registration_method != 'eduteams':
            raise ValidationError(_('User is not managed by eduTEAMS.'))
        if not django_settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
            raise ValidationError(
                _('Remote eduTEAMS account synchronization extension is disabled.')
            )
        pull_remote_eduteams_user(user.username)
        return Response(status=status.HTTP_200_OK)


class BasePermissionViewSet(viewsets.ModelViewSet):
    """
    This is a base class for both customer and project permissions.
    scope_field is required parameter, it should be either 'customer' or 'project'.
    """

    scope_field = None

    def perform_create(self, serializer):
        scope = serializer.validated_data[self.scope_field]
        role = serializer.validated_data.get('role')
        expiration_time = serializer.validated_data.get('expiration_time')

        if not scope.can_manage_role(self.request.user, role, expiration_time):
            raise PermissionDenied()

        utils.check_customer_blocked(scope)

        super(BasePermissionViewSet, self).perform_create(serializer)

    def perform_update(self, serializer):
        permission = serializer.instance
        scope = getattr(permission, self.scope_field)
        role = getattr(permission, 'role', None)

        utils.check_customer_blocked(scope)

        new_expiration_time = serializer.validated_data.get('expiration_time')
        old_expiration_time = permission.expiration_time
        if new_expiration_time == old_expiration_time:
            return

        if not scope.can_manage_role(self.request.user, role, new_expiration_time):
            raise PermissionDenied()

        serializer.save()
        structure_role_updated.send(
            sender=self.queryset.model, instance=permission, user=self.request.user,
        )

    def perform_destroy(self, instance):
        permission = instance
        scope = getattr(permission, self.scope_field)
        role = getattr(permission, 'role', None)
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

    queryset = models.ProjectPermission.objects.filter(is_active=True).order_by(
        '-created'
    )
    serializer_class = serializers.ProjectPermissionSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.ProjectPermissionFilter
    scope_field = 'project'

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


class ProjectPermissionLogViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = models.ProjectPermission.objects.filter(is_active=None)
    serializer_class = serializers.ProjectPermissionLogSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.ProjectPermissionFilter


class CustomerPermissionViewSet(BasePermissionViewSet):
    """
    - Customers are connected to users through roles, whereas user may have role "customer owner".
    - Each customer may have multiple owners, and each user may own multiple customers.
    - Staff members can list all available customers and create new customers.
    - Customer owners can list all customers they own. Customer owners can also create new customers.
    - Project administrators can list all the customers that own any of the projects they are administrators in.
    - Project managers can list all the customers that own any of the projects they are managers in.
    """

    queryset = models.CustomerPermission.objects.filter(is_active=True).order_by(
        '-created'
    )
    serializer_class = serializers.CustomerPermissionSerializer
    filterset_class = filters.CustomerPermissionFilter
    scope_field = 'customer'

    def get_queryset(self):
        queryset = super(CustomerPermissionViewSet, self).get_queryset()

        if not (self.request.user.is_staff or self.request.user.is_support):
            queryset = queryset.filter(
                Q(user=self.request.user, is_active=True)
                | Q(
                    customer__projects__permissions__user=self.request.user,
                    is_active=True,
                )
                | Q(customer__permissions__user=self.request.user, is_active=True)
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


class CustomerPermissionLogViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = models.CustomerPermission.objects.filter(is_active=None)
    serializer_class = serializers.CustomerPermissionLogSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.CustomerPermissionFilter


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
        queryset = super(SshKeyViewSet, self).get_queryset()
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
        return super(SshKeyViewSet, self).list(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = self.request.user
        name = serializer.validated_data['name']

        if core_models.SshPublicKey.objects.filter(user=user, name=name).exists():
            raise rf_serializers.ValidationError(
                {'name': [_('This field must be unique.')]}
            )

        serializer.save(user=user)


class ServiceSettingsViewSet(core_mixins.EagerLoadMixin, core_views.ActionsViewSet):
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

    def perform_create(self, serializer):
        service_settings = serializer.save()

        transaction.on_commit(
            lambda: ServiceSettingsCreateExecutor.execute(service_settings)
        )

    def list(self, request, *args, **kwargs):
        """
        To get a list of service settings, run **GET** against */api/service-settings/* as an authenticated user.
        Only settings owned by this user or shared settings will be listed.

        Supported filters are:

        - ?name=<text> - partial matching used for searching
        - ?type=<type> - choices: OpenStack, DigitalOcean, Amazon, JIRA
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

    update_permissions = partial_update_permissions = [
        can_user_update_settings,
        permissions.check_access_to_services_management,
    ]

    update_validators = partial_update_validators = [utils.check_customer_blocked]

    destroy_permissions = [can_user_update_settings]


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
        return self.object.get_users().count()

    def get_projects(self):
        return self._count_model(models.Project)

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
        return {'keys': self.get_keys, 'hooks': self.get_hooks}

    def get_keys(self):
        return core_models.SshPublicKey.objects.filter(
            user_uuid=self.request.user.uuid.hex
        ).count()

    def get_hooks(self):
        return core_managers.SummaryQuerySet(
            logging_models.BaseHook.get_all_models()
        ).count()


class BaseServicePropertyViewSet(viewsets.ReadOnlyModelViewSet):
    filterset_class = filters.BaseServicePropertyFilter


def check_resource_backend_id(resource):
    if not resource.backend_id:
        raise ValidationError(_('Resource does not have backend ID.'))


class ResourceViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    """ Basic view set for all resource view sets. """

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
    def unlink(self, request, resource, uuid=None):
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
