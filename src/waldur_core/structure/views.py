import logging

from dbtemplates.models import Template
from dbtemplates.utils.cache import remove_cached_template
from django.conf import settings as django_settings
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.plumbing import (
    OpenApiTypes,
    build_array_type,
    build_basic_type,
)
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters as rf_filters
from rest_framework import mixins, status, viewsets
from rest_framework import permissions as rf_permissions
from rest_framework import serializers as rf_serializers
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.utils import is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    has_permission,
    permission_factory,
)
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters, models, permissions, serializers, utils
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_active_tokens,
    get_connected_customers,
    get_connected_projects,
    get_project_users,
)
from waldur_core.structure.utils import get_components_usage_data_from_resources
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace_site_agent import utils as remote_slurm_utils

logger = logging.getLogger(__name__)


BASE_USER_PARAMETERS = [
    OpenApiParameter("full_name", str, OpenApiParameter.QUERY),
    OpenApiParameter("user_keyword", str, OpenApiParameter.QUERY),
    OpenApiParameter("native_name", str, OpenApiParameter.QUERY),
    OpenApiParameter("organization", str, OpenApiParameter.QUERY),
    OpenApiParameter("email", str, OpenApiParameter.QUERY),
    OpenApiParameter("phone_number", str, OpenApiParameter.QUERY),
    OpenApiParameter("description", str, OpenApiParameter.QUERY),
    OpenApiParameter("job_title", str, OpenApiParameter.QUERY),
    OpenApiParameter("username", str, OpenApiParameter.QUERY),
    OpenApiParameter("civil_number", str, OpenApiParameter.QUERY),
    OpenApiParameter("is_active", str, OpenApiParameter.QUERY),
    OpenApiParameter("registration_method", str, OpenApiParameter.QUERY),
]


class CustomerViewSet(
    UserRoleMixin,
    core_mixins.EagerLoadMixin,
    viewsets.ModelViewSet,
):
    queryset = models.Customer.objects.all().order_by("name")
    serializer_class = serializers.CustomerSerializer
    lookup_field = "uuid"
    filter_backends = (
        filters.GenericUserFilter,
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        rf_filters.OrderingFilter,
        filters.AccountingStartDateFilter,
        filters.ExternalCustomerFilterBackend,
    )
    ordering_fields = (
        "abbreviation",
        "accounting_start_date",
        "agreement_number",
        "contact_details",
        "created",
        "name",
        "native_name",
        "registration_code",
    )
    filterset_class = filters.CustomerFilter

    def list(self, request, *args, **kwargs):
        """
        To get a list of customers, run GET against /api/customers/ as authenticated user. Note that a user can
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

    @extend_schema(
        description="A new customer can only be created by users with staff privilege",
        request=serializers.CustomerSerializer,
        examples=[
            OpenApiExample(
                name="Create customer",
                value={
                    "name": "Customer A",
                    "native_name": "Customer A",
                    "abbreviation": "CA",
                    "contact_details": "Luhamaa 28, 10128 Tallinn",
                },
                request_only=True,
            ),
        ],
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        If a customer has connected projects, deletion request will fail with 409 response code.
        """
        return super().destroy(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.action == "users":
            return serializers.CustomerUserSerializer
        return super().get_serializer_class()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.action == "users":
            context["customer"] = self.get_object()
        return context

    def perform_create(self, serializer):
        if not self.request.user.is_staff:
            raise PermissionDenied()

        customer: models.Customer = serializer.save()

        if django_settings.WALDUR_CORE.get(
            "CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION", False
        ):
            project = models.Project(
                name=_("First project"),
                description=_("First project we have created for you"),
                customer=customer,
            )
            project.save()

    def perform_update(self, serializer):
        if not has_permission(
            self.request, PermissionEnum.UPDATE_CUSTOMER, serializer.instance
        ):
            raise PermissionDenied()

        utils.check_customer_blocked_or_archived(serializer.instance)
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        if not has_permission(self.request, PermissionEnum.DELETE_CUSTOMER, instance):
            raise PermissionDenied()

        utils.check_customer_blocked_or_archived(instance)

        return super().perform_destroy(instance)

    @extend_schema(
        description="A list of users connected to the customer.",
        responses=serializers.CustomerUserSerializer(many=True),
        parameters=BASE_USER_PARAMETERS
        + [
            OpenApiParameter("project_role", str, OpenApiParameter.QUERY),
            OpenApiParameter("organization_role", str, OpenApiParameter.QUERY),
            OpenApiParameter("o", str, OpenApiParameter.QUERY),
            OpenApiParameter(
                "field",
                build_array_type(build_basic_type(OpenApiTypes.STR)),
                OpenApiParameter.QUERY,
                enum=serializers.CustomerUserSerializer.Meta.fields,
            ),
        ],
    )
    @action(
        detail=True,
        filter_backends=[filters.GenericRoleFilter],
    )
    def users(self, request, uuid=None):
        customer: models.Customer = self.get_object()
        user = request.user
        queryset = customer.get_users()

        if not (
            has_permission(request, PermissionEnum.LIST_CUSTOMER_USERS, customer)
            or user.is_support
        ):
            raise PermissionDenied()

        # we need to handle filtration manually because we want to filter only customer users, not customers.
        name_filter_backend = filters.UserConcatenatedNameOrderingBackend()
        queryset = name_filter_backend.filter_queryset(request, queryset, self)
        roles_filter_backend = filters.UserRolesFilter()
        queryset = roles_filter_backend.filter_queryset(request, queryset, self)
        queryset = self.paginate_queryset(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        description="Return list of countries",
        request=None,
        responses=serializers.CountrySerializer(many=True),
    )
    @action(detail=False)
    def countries(self, request):
        return Response(
            [
                {"label": item[1], "value": item[0]}
                for item in serializers.CountrySerializerMixin.get_country_choices()
            ]
        )

    @extend_schema(
        description="Return statistics about customer resources usage",
        responses=serializers.ComponentsUsageStatsSerializer,
        parameters=[
            OpenApiParameter(
                name="for_current_month", type=bool, location=OpenApiParameter.QUERY
            ),
        ],
    )
    @action(detail=True)
    def stats(self, request, *args, **kwargs):
        customer: models.Customer = self.get_object()

        resources = marketplace_models.Resource.objects.filter(
            project__customer=customer
        ).exclude(state=ResourceStates.TERMINATED)
        resources = filter_queryset_for_user(resources, request.user)

        for_current_month = request.query_params.get("for_current_month", False)
        if for_current_month in ["true", "True"]:
            for_current_month = True

        components_data_list = get_components_usage_data_from_resources(
            resources, for_current_month
        )

        return Response(
            {
                "components": components_data_list,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Update organization groups for customer",
        request=marketplace_serializers.OrganizationGroupsSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def update_organization_groups(self, request, uuid):
        if not self.request.user.is_staff:
            raise PermissionDenied()
        customer: models.Customer = self.get_object()
        serializer = marketplace_serializers.OrganizationGroupsSerializer(
            instance=customer, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)


class AccessSubnetViewSet(core_views.ActionsViewSet):
    queryset = models.AccessSubnet.objects.all()
    serializer_class = serializers.AccessSubnetSerializer
    lookup_field = "uuid"
    filterset_class = filters.AccessSubnetFilter
    filter_backends = (DjangoFilterBackend, filters.GenericRoleFilter)
    destroy_permissions = [
        permission_factory(PermissionEnum.DELETE_ACCESS_SUBNET, ["customer"])
    ]
    update_permissions = partial_update_permissions = [
        permission_factory(PermissionEnum.UPDATE_ACCESS_SUBNET, ["customer"])
    ]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.is_staff or user.is_support:
            return qs
        connected_customers = get_connected_customers(user=user)
        return models.AccessSubnet.objects.filter(customer__in=connected_customers)


class ProjectTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.ProjectType.objects.all()
    serializer_class = serializers.ProjectTypeSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectTypeFilter


class ProjectViewSet(
    UserRoleMixin, core_mixins.EagerLoadMixin, core_views.ActionsViewSet
):
    queryset = models.Project.available_objects.all().order_by("name")
    serializer_class = serializers.ProjectSerializer
    lookup_field = "uuid"
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

    destroy_permissions = [
        permission_factory(PermissionEnum.DELETE_PROJECT, ["customer"])
    ]

    update_permissions = partial_update_permissions = [
        permission_factory(PermissionEnum.UPDATE_PROJECT, ["*", "customer"])
    ]

    @extend_schema(
        request=serializers.ProjectSerializer,
        examples=[
            OpenApiExample(
                name="Create project",
                value={
                    "name": "Project A",
                    "customer": "http://example.com/api/customers/6c9b01c251c24174a6691a1f894fae31/",
                },
                request_only=True,
            ),
        ],
    )
    def create(self, request, *args, **kwargs):
        """
        A new project can be created by users with staff privilege (is_staff=True) or customer owners.
        Project resource quota is optional.
        """
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        If a project has connected instances, deletion request will fail with 409 response code.
        """
        return super().destroy(request, *args, **kwargs)

    def perform_create(self, serializer):
        customer = serializer.validated_data["customer"]

        utils.check_customer_blocked_or_archived(customer)

        if not has_permission(self.request, PermissionEnum.CREATE_PROJECT, customer):
            raise PermissionDenied()

        super().perform_create(serializer)

    @extend_schema(
        request=serializers.MoveProjectSerializer,
        responses=serializers.ProjectSerializer,
    )
    @action(detail=True, methods=["post"])
    def move_project(self, request, uuid=None):
        project = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        customer = serializer.validated_data["customer"]
        preserve_permissions = serializer.validated_data["preserve_permissions"]

        utils.move_project(project, customer, request.user, preserve_permissions)
        serialized_project = serializers.ProjectSerializer(
            project, context={"request": self.request}
        )

        return Response(serialized_project.data, status=status.HTTP_200_OK)

    move_project_serializer_class = serializers.MoveProjectSerializer
    move_project_permissions = [permissions.is_staff]

    @extend_schema(
        description="Return statistics about project resources usage",
        responses=serializers.ComponentsUsageStatsSerializer,
        parameters=[
            OpenApiParameter(
                name="for_current_month", type=bool, location=OpenApiParameter.QUERY
            ),
        ],
    )
    @action(detail=True)
    def stats(self, request, *args, **kwargs):
        project = self.get_object()

        resources = marketplace_models.Resource.objects.filter(project=project).exclude(
            state=ResourceStates.TERMINATED
        )
        resources = filter_queryset_for_user(resources, request.user)

        for_current_month = request.query_params.get("for_current_month", False)
        if for_current_month in ["true", "True"]:
            for_current_month = True

        components_data_list = get_components_usage_data_from_resources(
            resources, for_current_month
        )

        return Response(
            {
                "components": components_data_list,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="A list of users which can be added to the "
        "current project from other projects of the same customer.",
        responses=serializers.BasicUserSerializer(many=True),
        parameters=BASE_USER_PARAMETERS,
    )
    @action(
        detail=True,
        filter_backends=[filters.GenericRoleFilter],
    )
    def other_users(self, request, uuid=None):
        project: models.Project = self.get_object()
        projects = (
            models.Project.objects.filter(customer=project.customer)
            .filter(id__in=get_connected_projects(request.user))
            .exclude(id=project.id)
        ).values_list("id", flat=True)

        queryset = core_models.User.objects.filter(id__in=get_project_users(projects))

        queryset = filters.UserConcatenatedNameOrderingBackend().filter_queryset(
            request, queryset, self
        )
        filterset = filters.BaseUserFilter(request.GET, queryset=queryset)
        queryset = filterset.qs
        queryset = self.paginate_queryset(queryset)
        serializer = serializers.BasicUserSerializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        description="Trigger user role sync for this project",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def sync_user_roles(self, request, uuid=None):
        """
        Trigger user role sync message for this project.
        Sends a notification to RabbitMQ that this project needs user role synchronization.
        """
        project: models.Project = self.get_object()

        remote_slurm_utils.push_user_role_sync_message(project)

        return Response(status=status.HTTP_200_OK)

    sync_user_roles_permissions = [permissions.is_staff]


class UserViewSet(core_views.ActionsViewSet):
    queryset = core_models.User.all_objects.select_related("auth_token")
    serializer_class = serializers.UserSerializer
    lookup_field = "uuid"
    permission_classes = (
        rf_permissions.IsAuthenticated,
        permissions.IsAdminOrOwner,
        core_permissions.ActionsPermission,
    )
    filter_backends = (
        filters.UserFilterBackend,
        DjangoFilterBackend,
    )
    filterset_class = filters.UserFilter

    def get_queryset(self):
        qs = super().get_queryset()
        if not self.request.user.is_authenticated:
            return qs.none()
        if self.request.user.is_staff or self.request.user.is_support:
            return qs
        return qs.filter(is_active=True)

    def list(self, request, *args, **kwargs):
        if request.user.is_identity_manager and not (
            request.user.is_staff or request.user.is_support
        ):
            return Response(
                _("Identity manager is not allowed to list users."),
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().list(request, *args, **kwargs)

    @extend_schema(
        request=serializers.UserEmailChangeSerializer,
        responses=None,
        description="Allows to change email for user.",
    )
    @action(detail=True, methods=["post"])
    def change_email(self, request, uuid=None):
        user = self.get_object()

        idp_protected_fields = utils.get_identity_provider_fields(
            user.registration_method
        )

        if "email" in idp_protected_fields:
            raise ValidationError(
                {
                    "detail": _(
                        "The registration method does not allow direct email modification."
                    )
                }
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        try:
            user.create_request_for_update_email(email)
        except django_exceptions.ValidationError as error:
            raise ValidationError(error.message_dict)

        return Response(
            {"detail": _("The change email request has been successfully created.")},
            status=status.HTTP_200_OK,
        )

    change_email_serializer_class = serializers.UserEmailChangeSerializer

    @extend_schema(
        request=None,
        responses=None,
        description="Cancel email update request",
    )
    @action(detail=True, methods=["post"])
    def cancel_change_email(self, request, uuid=None):
        user = self.get_object()
        count = core_models.ChangeEmailRequest.objects.filter(user=user).delete()[0]

        if count:
            msg = _("The change email request has been successfully deleted.")
        else:
            msg = _("The change email request has not been found.")

        return Response({"detail": msg}, status=status.HTTP_200_OK)

    @extend_schema(
        request=serializers.ConfirmEmailRequestSerializer,
        responses=None,
        description="Confirm email update using code",
    )
    @action(detail=False, methods=["post"])
    def confirm_email(self, request):
        code = request.data.get("code")
        if not code or not is_uuid_like(code):
            raise ValidationError(_("The confirmation code is required."))

        change_request = get_object_or_404(core_models.ChangeEmailRequest, uuid=code)

        if (
            change_request.created + django_settings.WALDUR_CORE["EMAIL_CHANGE_MAX_AGE"]
            < timezone.now()
        ):
            raise ValidationError(_("Request has expired."))

        with transaction.atomic():
            change_request.user.email = change_request.email
            change_request.user.save(update_fields=["email"])
            core_models.ChangeEmailRequest.objects.filter(
                email=change_request.email
            ).delete()
        return Response(
            {"detail": _("Email has been successfully updated.")},
            status=status.HTTP_200_OK,
        )

    def check_permissions(self, request):
        if self.action == "confirm_email":
            return
        super().check_permissions(request)

    @extend_schema(
        description="Get current user details, including authentication token.",
        parameters=[],
    )
    @action(detail=False, methods=["get"])
    def me(self, request):
        serializer = self.get_serializer(request.user)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses=None,
        description="Pulls remote user data from eduTEAMS.",
    )
    @action(detail=True, methods=["post"])
    def pull_remote_user(self, request, uuid=None):
        user = self.get_object()
        if (
            user.identity_source != ProviderChoices.EDUTEAMS
            and user.registration_method != ProviderChoices.EDUTEAMS
        ):
            raise ValidationError(_("User is not managed by eduTEAMS."))
        if not django_settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_ENABLED"]:
            raise ValidationError(
                _("Remote eduTEAMS account synchronization extension is disabled.")
            )
        pull_remote_eduteams_user(user.username)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=serializers.PasswordChangeSerializer,
        responses=None,
        description="Allows staff user to change password for any user.",
    )
    @action(detail=True, methods=["post"])
    def change_password(self, request, uuid=None):
        user = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user.set_password(serializer.validated_data["new_password"])
        user.save()

        event_logger.emit(
            "Password has been changed for user {affected_user_username} by %s."
            % self.request.user,
            event_type=EventType.USER_PASSWORD_UPDATED_BY_STAFF,
            event_context={"affected_user": user},
            scopes=[user],
        )
        logger.info(
            f"Password has been changed for user {user} by {self.request.user}."
        )

        return Response({"status": "password set"}, status=status.HTTP_200_OK)

    change_password_serializer_class = serializers.PasswordChangeSerializer
    change_password_permissions = [permissions.is_staff]

    @extend_schema(
        request=serializers.UserAuthTokenSerializer,
        responses=serializers.UserAuthTokenSerializer,
        filters=False,
    )
    @action(detail=True, methods=["get"])
    def token(self, request, uuid=None):
        user = self.get_object()
        token = user.auth_token
        serializer = self.get_serializer(token)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=serializers.UserAuthTokenSerializer,
        responses=serializers.UserAuthTokenSerializer,
        description="Allows to refresh user auth token.",
    )
    @action(detail=True, methods=["post"])
    def refresh_token(self, request, uuid=None):
        user = self.get_object()
        token = user.auth_token
        token.delete()
        token = Token.objects.create(user=user)
        serializer = self.get_serializer(token)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    token_permissions = refresh_token_permissions = [permissions.is_staff]
    token_serializer_class = refresh_token_serializer_class = (
        serializers.UserAuthTokenSerializer
    )

    def perform_create(self, serializer):
        user = serializer.save()
        event_logger.emit(
            "User {affected_user_username} has been created by %s." % self.request.user,
            event_type=EventType.USER_HAS_BEEN_CREATED_BY_STAFF,
            event_context={"affected_user": user},
            scopes=[user],
        )
        logger.info(f"User {user} has been created by {self.request.user}.")


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
    lookup_field = "uuid"

    @extend_schema(
        request=None,
        responses=None,
        description="Close customer permission review.",
    )
    @action(detail=True, methods=["post"])
    def close(self, request, uuid=None):
        review: models.CustomerPermissionReview = self.get_object()
        if not review.is_pending:
            raise ValidationError(_("Review is already closed."))
        review.close(request.user)
        return Response(status=status.HTTP_200_OK)


@extend_schema_view(
    create=extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="ssh-key-create",
                value={
                    "name": "ssh_public_key1",
                    "public_key": """ssh-rsa ... jhon@example.com""",
                },
            )
        ]
    )
)
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
    lookup_field = "uuid"
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
                _("Only staff users are allowed to delete shared SSH public key.")
            )
        else:
            instance.delete()

    def perform_create(self, serializer):
        user = self.request.user
        name = serializer.validated_data["name"]

        if core_models.SshPublicKey.objects.filter(user=user, name=name).exists():
            raise rf_serializers.ValidationError(
                {"name": [_("This field must be unique.")]}
            )

        serializer.save(user=user)


class ServiceSettingsViewSet(
    core_mixins.EagerLoadMixin, core_views.ReadOnlyActionsViewSet
):
    queryset = models.ServiceSettings.objects.filter().order_by("pk")
    serializer_class = serializers.ServiceSettingsSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.ServiceSettingsScopeFilterBackend,
        rf_filters.OrderingFilter,
    )
    filterset_class = filters.ServiceSettingsFilter
    lookup_field = "uuid"
    ordering_fields = (
        "type",
        "name",
        "state",
    )


class BaseServicePropertyViewSet(viewsets.ReadOnlyModelViewSet):
    filterset_class = filters.BaseServicePropertyFilter


def check_resource_backend_id(resource):
    if not resource.backend_id:
        raise ValidationError(_("Resource does not have backend ID."))


class ResourceViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    """Basic view set for all resource view sets."""

    lookup_field = "uuid"
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)
    unsafe_methods_permissions = [permissions.is_administrator]
    update_validators = partial_update_validators = [
        core_validators.StateValidator(CoreStates.OK)
    ]
    destroy_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]

    pull_serializer_class = EmptySerializer

    @action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        if self.pull_executor == NotImplemented:
            return Response(
                {"detail": _("Pull operation is not implemented.")},
                status=status.HTTP_409_CONFLICT,
            )
        self.pull_executor.execute(self.get_object())
        return Response(
            {"detail": _("Pull operation was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_executor = NotImplemented
    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
        check_resource_backend_id,
    ]

    unlink_serializer_class = EmptySerializer

    @action(detail=True, methods=["post"])
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


class OrganizationGroupViewSet(core_views.ActionsViewSet):
    queryset = (
        models.OrganizationGroup.objects.all()
        .order_by("name")
        .annotate(customers_count=Count("customers"))
    )
    serializer_class = serializers.OrganizationGroupSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, rf_filters.OrderingFilter)
    filterset_class = filters.OrganizationGroupFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)
    ordering_fields = ("name", "customers_count")


class UserAgreementsViewSet(ActionsViewSet):
    serializer_class = serializers.UserAgreementSerializer
    permission_classes = (core_permissions.ActionsPermission,)
    unsafe_methods_permissions = [permissions.is_staff]
    filterset_class = filters.UserAgreementsFilter
    lookup_field = "uuid"
    queryset = models.UserAgreement.objects.all()


class NotificationViewSet(ActionsViewSet):
    queryset = core_models.Notification.objects.all().order_by("id")
    serializer_class = serializers.NotificationSerializer
    permission_classes = (rf_permissions.IsAdminUser,)
    filterset_class = filters.NotificationFilter
    lookup_field = "uuid"

    @extend_schema(
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def enable(self, request, uuid=None):
        notification: core_models.Notification = self.get_object()
        message = f"The notification {notification.key} has been enabled"
        if not notification.enabled:
            notification.enabled = True
            notification.save()
            logger.info(message)
        return Response(
            {"detail": _(message)},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def disable(self, request, uuid=None):
        notification: core_models.Notification = self.get_object()
        message = f"The notification {notification.key} has been disabled"
        if notification.enabled:
            notification.enabled = False
            notification.save()
            logger.info(message)
        return Response(
            {"detail": _(message)},
            status=status.HTTP_200_OK,
        )


class NotificationTemplateViewSet(ActionsViewSet):
    queryset = core_models.NotificationTemplate.objects.all()
    serializer_class = serializers.NotificationTemplateDetailSerializers
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.NotificationTemplateFilter

    @extend_schema(
        request=serializers.NotificationTemplateUpdateSerializers,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def override(self, request, uuid=None):
        template: core_models.NotificationTemplate = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_content = serializer.validated_data["content"]
        name = template.path
        message = f"The template {name} has been overridden"
        try:
            template_dbtemplates = Template.objects.get(name=name)
            template_dbtemplates.content = new_content
            template_dbtemplates.save()
        except Template.DoesNotExist:
            template_dbtemplates = Template.objects.create(
                name=name, content=new_content
            )

        remove_cached_template(template_dbtemplates)
        logger.info(message)
        return Response({"detail": _(message)}, status=status.HTTP_200_OK)

    override_serializer_class = serializers.NotificationTemplateUpdateSerializers
    override_permissions = [permissions.is_staff]


class AuthTokenViewSet(ActionsViewSet):
    serializer_class = serializers.AuthTokenSerializer
    lookup_field = "user_id"
    disabled_actions = ["create", "update", "partial_update"]
    permission_classes = (core_permissions.IsStaff,)

    def get_queryset(self):
        return get_active_tokens()
