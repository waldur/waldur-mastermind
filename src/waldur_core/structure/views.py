import logging
from datetime import datetime

from constance import config as constance_config
from dbtemplates.models import Template
from dbtemplates.utils.cache import add_template_to_cache
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.db.models import Count, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import Length, TruncMonth
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.plumbing import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import filters as rf_filters
from rest_framework import mixins, status, viewsets
from rest_framework import permissions as rf_permissions
from rest_framework import serializers as rf_serializers
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.checklist import mixins as checklist_mixins
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.models import Answer, ChecklistCompletion, Question
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.core.permissions import PATScopeAwareIsAdminUser
from waldur_core.core.serializers import ReviewCommentSerializer
from waldur_core.core.user_attributes import get_profile_completeness_details
from waldur_core.core.utils import get_ip_address, is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import UserDataAccessLog
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import (
    has_permission,
    permission_factory,
)
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import (
    filters,
    managers,
    models,
    permissions,
    serializers,
    utils,
)
from waldur_core.structure.data_access import get_user_data_access_visibility
from waldur_core.structure.digest_tasks import (
    render_project_preview,
    send_digest_for_customer_preview,
)
from waldur_core.structure.managers import (
    filter_queryset_by_user_ip,
    filter_queryset_for_user,
    get_active_tokens,
    get_connected_customers,
    get_connected_projects,
    get_project_users,
)
from waldur_core.structure.serializers_data_access import (
    UserDataAccessLogSerializer,
    UserDataAccessSerializer,
)
from waldur_core.structure.utils import (
    get_components_usage_data_from_resources,
)
from waldur_core.structure.utils_data_access import bulk_log_user_data_access
from waldur_core.user_actions import serializers as user_action_serializers
from waldur_core.user_actions import tasks as user_action_tasks
from waldur_core.users import tasks as user_tasks
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation
from waldur_core.users.scim import tasks as scim_tasks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.enums import ResourceStates

logger = logging.getLogger(__name__)


CUSTOMER_UUID_PARAMETER = OpenApiParameter(
    name="customer_uuid",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    description="UUID of the customer",
)
PROJECT_UUID_PARAMETER = OpenApiParameter(
    name="project_uuid",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    description="UUID of the project",
)


@extend_schema_view(
    list=extend_schema(
        summary="List customers",
        description="Retrieve a list of customers. The list is filtered based on the user's permissions.",
    ),
    retrieve=extend_schema(
        summary="Retrieve customer details",
        description="Fetch the details of a specific customer by its UUID.",
    ),
    create=extend_schema(
        summary="Create a new customer",
        description="A new customer can only be created by users with staff privilege.",
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
    ),
    update=extend_schema(
        summary="Update a customer",
        description="Update the details of an existing customer. Requires customer owner or staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a customer",
        description="Partially update the details of an existing customer. Requires customer owner or staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a customer",
        description="Delete a customer. This action is only available to staff users. If a customer has any active projects, the deletion request will fail with a 409 Conflict response.",
    ),
)
class CustomerViewSet(
    UserRoleMixin,
    core_views.HistoryViewSetMixin,
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
        "description",
        "name",
        "native_name",
        "registration_code",
    )
    filterset_class = filters.CustomerFilter

    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        if user.is_staff or user.is_support:
            queryset = queryset.annotate(
                annotated_projects_count=Count(
                    "projects", filter=Q(projects__is_removed=False), distinct=True
                )
            )
        elif user.is_authenticated:
            user_projects = managers.get_visible_projects(user)

            queryset = queryset.annotate(
                annotated_projects_count=Count(
                    "projects",
                    filter=Q(
                        projects__id__in=user_projects, projects__is_removed=False
                    ),
                    distinct=True,
                )
            )

        # Prefetch projects securely based on user visibility
        prefetch_projects = self._get_project_prefetch(user)
        if prefetch_projects:
            queryset = queryset.prefetch_related(prefetch_projects)

        return queryset

    def _get_project_prefetch(self, user):
        """Returns a Prefetch object restricted by user permissions"""
        requested_fields = self.request.query_params.getlist("field")
        if requested_fields and "projects" not in requested_fields:
            return None

        project_qs = models.Project.available_objects.all()

        if not (user.is_staff or user.is_support):
            user_projects = managers.get_visible_projects(user)
            project_qs = project_qs.filter(id__in=user_projects)

        # Use to_attr to keep it cleanly separated from the default 'projects' manager
        return Prefetch("projects", queryset=project_qs, to_attr="visible_projects")

    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

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
        if not self.request.user.is_staff:
            raise PermissionDenied()

        utils.check_customer_blocked_or_archived(instance)

        return super().perform_destroy(instance)

    @extend_schema(
        summary="Get list of available countries",
        description="Returns a list of countries that can be used when creating or updating a customer. The list can be configured by the service provider.",
        request=None,
        responses=serializers.CountrySerializer(many=True),
        examples=[
            OpenApiExample(
                "Country list response",
                response_only=True,
                value=[
                    {"label": "Estonia", "value": "EE"},
                    {"label": "Latvia", "value": "LV"},
                    {"label": "Finland", "value": "FI"},
                ],
            )
        ],
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
        summary="Update customer contact details",
        description=(
            "Update organization contact information. Requires "
            "CUSTOMER_CONTACT_UPDATE or CUSTOMER.UPDATE permission."
        ),
        request=serializers.CustomerContactUpdateSerializer,
        responses=serializers.CustomerContactUpdateSerializer,
    )
    @action(detail=True, methods=["post"], url_path="contact")
    def contact(self, request, uuid=None):
        customer: models.Customer = self.get_object()
        if not (
            request.user.is_staff
            or has_permission(request, PermissionEnum.UPDATE_CUSTOMER, customer)
            or has_permission(request, PermissionEnum.CUSTOMER_CONTACT_UPDATE, customer)
        ):
            raise PermissionDenied()

        utils.check_customer_blocked_or_archived(customer)

        serializer = serializers.CustomerContactUpdateSerializer(
            customer, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get customer resource usage statistics",
        description="Provides statistics about the resource usage (e.g., CPU, RAM, storage) for all projects within a customer. Can be filtered to show usage for the current month only.",
        responses=serializers.ComponentsUsageStatsSerializer,
        parameters=[
            OpenApiParameter(
                name="for_current_month",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="If true, returns usage data for the current month only. Otherwise, returns total usage.",
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
        summary="Update organization groups for a customer",
        description="Assigns a customer to one or more organization groups. This action is restricted to staff users.",
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

    @extend_schema(
        summary="Get project digest configuration",
        description="Retrieve the project digest email configuration for this organization.",
        responses=serializers.ProjectDigestConfigSerializer,
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="project-digest-config",
    )
    def project_digest_config(self, request, uuid=None):
        if not constance_config.ENABLE_PROJECT_DIGEST:
            raise ValidationError(_("Project digest feature is disabled."))

        customer = self.get_object()
        if not (
            request.user.is_staff
            or has_permission(request, PermissionEnum.UPDATE_CUSTOMER, customer)
        ):
            raise PermissionDenied()

        try:
            config = customer.project_digest_config
        except models.ProjectDigestConfiguration.DoesNotExist:
            config = models.ProjectDigestConfiguration(customer=customer)

        serializer = serializers.ProjectDigestConfigSerializer(config)
        return Response(serializer.data)

    @extend_schema(
        summary="Update project digest configuration",
        description="Update the project digest email configuration for this organization.",
        request=serializers.ProjectDigestConfigSerializer,
        responses=serializers.ProjectDigestConfigSerializer,
    )
    @action(
        detail=True,
        methods=["put", "patch"],
        url_path="update-project-digest-config",
    )
    def update_project_digest_config(self, request, uuid=None):
        if not constance_config.ENABLE_PROJECT_DIGEST:
            raise ValidationError(_("Project digest feature is disabled."))

        customer = self.get_object()
        if not (
            request.user.is_staff
            or has_permission(request, PermissionEnum.UPDATE_CUSTOMER, customer)
        ):
            raise PermissionDenied()

        try:
            config = customer.project_digest_config
        except models.ProjectDigestConfiguration.DoesNotExist:
            config = models.ProjectDigestConfiguration(
                customer=customer, created_by=request.user
            )

        partial = request.method == "PATCH"
        serializer = serializers.ProjectDigestConfigSerializer(
            config, data=request.data, partial=partial
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(
        summary="Send a test digest email",
        description="Send a test digest email to the requesting user.",
        request=None,
        responses={200: None},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="project-digest-config/send-test",
    )
    def project_digest_send_test(self, request, uuid=None):
        if not constance_config.ENABLE_PROJECT_DIGEST:
            raise ValidationError(_("Project digest feature is disabled."))

        customer = self.get_object()
        if not (
            request.user.is_staff
            or has_permission(request, PermissionEnum.UPDATE_CUSTOMER, customer)
        ):
            raise PermissionDenied()

        send_digest_for_customer_preview(customer, request.user)
        return Response(
            {"detail": _("Test digest email has been sent.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Preview digest for a project",
        description="Returns rendered HTML preview of the digest for a specific project.",
        request=serializers.ProjectDigestPreviewSerializer,
        responses=serializers.ProjectDigestPreviewResponseSerializer,
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="project-digest-config/preview",
    )
    def project_digest_preview(self, request, uuid=None):
        if not constance_config.ENABLE_PROJECT_DIGEST:
            raise ValidationError(_("Project digest feature is disabled."))

        customer = self.get_object()
        if not (
            request.user.is_staff
            or has_permission(request, PermissionEnum.UPDATE_CUSTOMER, customer)
        ):
            raise PermissionDenied()

        serializer = serializers.ProjectDigestPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_uuid = serializer.validated_data["project_uuid"]

        project = get_object_or_404(
            models.Project.available_objects,
            uuid=project_uuid,
            customer=customer,
        )

        result = render_project_preview(project, customer)
        return Response(result)

    @extend_schema(
        summary="Update default affiliations for an organization",
        description=(
            "Replaces the organization's default affiliation list. "
            "Project creators in the organization will be limited to choosing "
            "from this list when affiliating a project. Staff-only."
        ),
        request=serializers.CustomerDefaultAffiliationsUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_default_affiliations(self, request, uuid=None):
        if not request.user.is_staff:
            raise PermissionDenied()
        customer = self.get_object()
        serializer = serializers.CustomerDefaultAffiliationsUpdateSerializer(
            instance=customer, data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_default_affiliations_serializer_class = (
        serializers.CustomerDefaultAffiliationsUpdateSerializer
    )


@extend_schema(
    summary="List users of a customer",
    parameters=[CUSTOMER_UUID_PARAMETER],
    description="Lists all users who have a role in the specified customer or any of its projects. Requires permissions to list customer users.",
)
class CustomerUsersViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = serializers.CustomerUserSerializer
    filter_backends = [
        filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.UserRolesFilter,
        filters.ConcatenatedNameOrderingBackend,
    ]
    filterset_class = filters.BaseUserFilter
    queryset = core_models.User.objects.none()

    def get_serializer_context(self) -> dict[str, any]:
        ctx = super().get_serializer_context()
        ctx["customer"] = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        return ctx

    def get_queryset(self) -> QuerySet[core_models.User]:
        customer = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        if not (
            has_permission(self.request, PermissionEnum.LIST_CUSTOMER_USERS, customer)
            or self.request.user.is_support
        ):
            raise PermissionDenied()
        return customer.get_users()


@extend_schema_view(
    list=extend_schema(
        summary="List access subnets",
        description="Retrieve a list of access subnets. Staff and support users can see all subnets, while other users can only see subnets associated with customers they have a role in.",
    ),
    retrieve=extend_schema(
        summary="Retrieve access subnet",
        description="Fetch the details of a specific access subnet by its UUID.",
    ),
    create=extend_schema(
        summary="Create an access subnet",
        description="Create a new access subnet for a customer.",
    ),
    update=extend_schema(
        summary="Update an access subnet",
        description="Update an existing access subnet.",
    ),
    partial_update=extend_schema(
        summary="Partially update an access subnet",
        description="Partially update an existing access subnet.",
    ),
    destroy=extend_schema(
        summary="Delete an access subnet",
        description="Delete an existing access subnet.",
    ),
)
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


@extend_schema_view(
    list=extend_schema(
        summary="List project types",
        description="Retrieve a list of available project types.",
    ),
    retrieve=extend_schema(
        summary="Retrieve project type details",
        description="Fetch details of a specific project type by its UUID.",
    ),
)
class ProjectTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.ProjectType.objects.all()
    serializer_class = serializers.ProjectTypeSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectTypeFilter


@extend_schema_view(
    list=extend_schema(
        summary="List projects",
        description="Retrieve a list of projects. The list is filtered based on the user's permissions. By default, only active projects are shown.",
        parameters=[
            OpenApiParameter(
                "include_terminated",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Include soft-deleted (terminated) projects. Only available to staff and support users, or users with organizational roles who can see their terminated projects.",
            )
        ],
    ),
    retrieve=extend_schema(
        summary="Retrieve project details",
        description="Fetch the details of a specific project by its UUID. Users can access details of terminated projects they previously had access to.",
    ),
    update=extend_schema(
        summary="Update project details",
        description="Update the details of a project. Requires project administrator or customer owner permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update project details",
        description="Partially update the details of a project. Requires project administrator or customer owner permissions.",
    ),
)
class ProjectViewSet(
    checklist_mixins.UserChecklistMixin,
    UserRoleMixin,
    core_mixins.EagerLoadMixin,
    core_views.ActionsViewSet,
):
    queryset = models.Project.available_objects.all().order_by("name")

    def get_queryset(self):
        user = getattr(self.request, "user", None)
        query_params = getattr(self.request, "query_params", self.request.GET)
        include_terminated = (
            query_params.get("include_terminated", "false").lower() == "true"
        )

        if include_terminated and user and (user.is_staff or user.is_support):
            # Staff and support users can see ALL terminated projects
            queryset = models.Project.objects.all().order_by("name")
        elif include_terminated and user and user.is_authenticated:
            # Regular users can see terminated projects they would normally have access to
            # Get the base queryset using normal filtering but include terminated projects
            base_queryset = models.Project.objects.all().order_by("name")
            # Apply the same filters that would normally be applied (GenericRoleFilter logic)
            queryset = filter_queryset_for_user(base_queryset, user)
            queryset = filter_queryset_by_user_ip(queryset, self.request)
        else:
            queryset = models.Project.available_objects.all().order_by("name")

        # Apply eager loading to prevent N+1 queries
        if getattr(self, "action", None) in ("list", "retrieve"):
            queryset = serializers.ProjectSerializer.eager_load(queryset, self.request)

        return queryset

    def get_object(self):
        """
        Override get_object to allow access to soft-deleted projects by UUID.
        This enables access to individual project endpoints (like stats) for
        terminated projects without requiring include_terminated parameter.
        For active projects, uses default Django behavior.
        """
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs[lookup_url_kwarg]

        # First check if the project exists and if it's soft-deleted
        try:
            project = models.Project.objects.get(**{self.lookup_field: lookup_value})

            # If project is active, use default Django behavior (preserves existing permission logic)
            if not project.is_removed:
                return super().get_object()

            # Only apply custom logic for soft-deleted projects
            user = getattr(self.request, "user", None)

            if not user or not user.is_authenticated:
                raise Http404("No Project matches the given query.")

            # Staff and support can access any terminated project
            if user.is_staff or user.is_support:
                return project

            # Regular users need to have normal access to the project
            filtered_queryset = filter_queryset_for_user(
                models.Project.objects.filter(id=project.id), user
            )
            filtered_queryset = filter_queryset_by_user_ip(
                filtered_queryset, self.request
            )
            if not filtered_queryset.exists():
                raise Http404("No Project matches the given query.")

            return project

        except models.Project.DoesNotExist:
            # Use default behavior for non-existent projects
            return super().get_object()

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

    # Checklist permissions (view access for all project members and customer roles)
    checklist_permissions = [permissions.has_project_access]
    completion_status_permissions = [permissions.has_project_access]
    submit_answers_permissions = [permissions.is_manager]

    def get_checklist_completion(self, obj):
        """Get checklist completion for project metadata."""
        try:
            # Check if customer has project metadata checklist configured
            if not obj.customer.project_metadata_checklist:
                return None

            # Get the ChecklistCompletion for this project
            project_content_type = ContentType.objects.get_for_model(obj)
            completion = ChecklistCompletion.objects.get(
                checklist=obj.customer.project_metadata_checklist,
                scope_content_type=project_content_type,
                scope_object_id=obj.id,
            )
            return completion
        except ChecklistCompletion.DoesNotExist:
            return None

    def get_checklist_for_new_object(self, parent_obj):
        """Get checklist for new projects from customer configuration."""
        if hasattr(parent_obj, "project_metadata_checklist"):
            return parent_obj.project_metadata_checklist
        return None

    def get_parent_object_for_checklist(self, parent_uuid):
        """Get customer object for checklist template lookup."""
        try:
            return models.Customer.objects.get(uuid=parent_uuid)
        except models.Customer.DoesNotExist:
            return None

    @extend_schema(
        summary="Create a new project",
        description="A new project can be created by users with staff privilege (is_staff=True) or customer owners. Project resource quota is optional.",
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
        return super().create(request, *args, **kwargs)

    @extend_schema(
        summary="Delete a project",
        description="Delete a project. If the project has any active resources, the deletion request will fail with a 409 Conflict response. This action performs a soft-delete, and the project can be recovered later.",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def perform_create(self, serializer):
        customer = serializer.validated_data["customer"]

        utils.check_customer_blocked_or_archived(customer)

        if not has_permission(self.request, PermissionEnum.CREATE_PROJECT, customer):
            raise PermissionDenied()

        super().perform_create(serializer)

    def perform_destroy(self, instance):
        """Override to pass the terminating user to the soft delete method."""
        instance._soft_delete(terminated_by=self.request.user)

    @extend_schema(
        summary="Move project to another customer",
        description="Moves a project and its associated resources to a different customer. You can choose whether to preserve existing project permissions for users. Terminated projects can also be moved.",
        request=serializers.MoveProjectSerializer,
        responses={
            200: serializers.ProjectSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def move_project(self, request, uuid=None):
        # Using get_object() would fail for org owners without direct project roles
        try:
            project = models.Project.objects.get(uuid=uuid)
        except models.Project.DoesNotExist:
            raise Http404("No Project matches the given query.")

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
    move_project_permissions = [permissions.can_move_project]

    @extend_schema(
        summary="Update affiliation for a project",
        description="Assigns the project to a single affiliation (or clears it when null).",
        request=serializers.ProjectAffiliationUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_affiliation(self, request, uuid=None):
        project = self.get_object()
        serializer = serializers.ProjectAffiliationUpdateSerializer(
            instance=project, data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        affiliation = serializer.validated_data.get("affiliation")
        if affiliation is not None and not request.user.is_staff:
            if not project.customer.default_affiliations.filter(
                pk=affiliation.pk
            ).exists():
                raise rf_serializers.ValidationError(
                    {
                        "affiliation": _(
                            "Selected affiliation is not in this organization's default list."
                        )
                    }
                )
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_affiliation_serializer_class = serializers.ProjectAffiliationUpdateSerializer
    update_affiliation_permissions = [
        permission_factory(PermissionEnum.UPDATE_PROJECT, ["*", "customer"])
    ]

    @extend_schema(
        summary="Get project resource usage statistics",
        description="Provides statistics about the resource usage (e.g., CPU, RAM, storage) for all resources within a project. Can be filtered to show usage for the current month only.",
        responses=serializers.ComponentsUsageStatsSerializer,
        parameters=[
            OpenApiParameter(
                name="for_current_month",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="If true, returns usage data for the current month only. Otherwise, returns total usage.",
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
        request=serializers.ProjectRecoverySerializer,
        responses=serializers.ProjectSerializer,
        summary="Recover a soft-deleted project",
        description="Recovers a soft-deleted (terminated) project, making it active again. Provides options to restore previous team members automatically (staff-only) or send them new invitations.",
    )
    @action(detail=True, methods=["post"])
    def recover(self, request, uuid=None):
        project = self.get_object()

        # Check if project is actually soft-deleted
        if not project.is_removed:
            raise ValidationError("Project is not deleted and cannot be recovered.")

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        restore_team_members = serializer.validated_data["restore_team_members"]
        send_invitations = serializer.validated_data[
            "send_invitations_to_previous_members"
        ]
        end_date = serializer.validated_data.get("end_date")

        # Validate that team recovery options are only used when metadata is available
        if (
            restore_team_members or send_invitations
        ) and not project.termination_metadata:
            raise ValidationError(
                "This project was deleted before team member metadata was captured. "
                "Only basic project recovery is available. "
                "Team member restoration and invitations require projects deleted after the metadata feature was implemented."
            )

        # Recover the project
        project.is_removed = False
        if end_date is not None:
            project.end_date = end_date
            project.save(update_fields=["is_removed", "end_date"])
        else:
            project.save(update_fields=["is_removed"])

        # Handle team recovery options
        restored_users = []
        sent_invitations = []

        if restore_team_members:
            restored_users = self._restore_team_members(project, request.user)
        elif send_invitations:
            sent_invitations = self._send_invitations_to_previous_members(
                project, request.user
            )

        # Log the recovery action
        event_context = {
            "project": project,
            "restore_team_members": restore_team_members,
            "send_invitations": send_invitations,
            "restored_users_count": len(restored_users),
            "sent_invitations_count": len(sent_invitations),
        }
        if end_date is not None:
            event_context["end_date"] = end_date.isoformat()

        event_logger.emit(
            "Project {project_name} has been recovered.",
            event_type=EventType.PROJECT_UPDATE_SUCCEEDED,
            event_context=event_context,
            scopes=[project, project.customer],
        )

        serialized_project = serializers.ProjectSerializer(
            project, context={"request": self.request}
        )

        # Add recovery information to response
        response_data = serialized_project.data
        if restore_team_members and restored_users:
            response_data["recovery_info"] = {
                "restored_users_count": len(restored_users),
                "restored_users": [
                    {
                        "user_uuid": str(user_role.user.uuid),
                        "username": user_role.user.username,
                        "role": user_role.role.name,
                    }
                    for user_role in restored_users
                ],
            }
        elif send_invitations and sent_invitations:
            response_data["recovery_info"] = {
                "sent_invitations_count": len(sent_invitations),
                "sent_invitations": [
                    {
                        "invitation_uuid": str(invitation.uuid),
                        "email": invitation.email,
                        "role": invitation.role.name,
                        "state": invitation.state,
                    }
                    for invitation in sent_invitations
                ],
            }

        return Response(response_data, status=status.HTTP_200_OK)

    def _validate_user_role_data(self, role_data, project):
        """Validate user role data and return user, role objects if valid."""
        User = get_user_model()

        # Get user and role objects by username and role name
        user = User.objects.get(username=role_data["user_username"])
        role = Role.objects.get(name=role_data["role_name"])

        # Check if user is still active
        if not user.is_active:
            return None, None

        # Parse expiration time
        expiration_time = None
        if role_data.get("original_expiration_time"):
            expiration_time = datetime.fromisoformat(
                role_data["original_expiration_time"]
            )

        # Check if role hasn't expired
        if expiration_time and expiration_time < timezone.now():
            return None, None

        # Check if user already has this role for this project
        existing_role = UserRole.objects.filter(
            user=user, role=role, scope=project, is_active=True
        ).first()
        if existing_role:
            return None, None

        return user, role

    def _restore_team_members(self, project, restored_by_user):
        """Restore team members from project termination metadata."""
        User = get_user_model()

        if (
            not project.termination_metadata
            or "user_roles" not in project.termination_metadata
        ):
            return []

        restored_roles = []
        user_roles_data = project.termination_metadata["user_roles"]

        for role_data in user_roles_data:
            if role_data.get("is_restored", False):
                continue  # Skip already restored roles

            try:
                user, role = self._validate_user_role_data(role_data, project)
                if not user or not role:
                    # Check if users/roles exist and mark existing role as restored
                    try:
                        existing_user = User.objects.get(
                            username=role_data["user_username"]
                        )
                        existing_role = Role.objects.get(name=role_data["role_name"])
                        existing_user_role = UserRole.objects.filter(
                            user=existing_user,
                            role=existing_role,
                            scope=project,
                            is_active=True,
                        ).first()
                        if existing_user_role:
                            role_data["is_restored"] = True
                            role_data["restored_at"] = timezone.now().isoformat()
                            role_data["restored_by"] = restored_by_user.username
                    except (User.DoesNotExist, Role.DoesNotExist):
                        pass  # Skip invalid users/roles
                    continue

                created_by = None
                if role_data.get("created_by_username"):
                    created_by = User.objects.get(
                        username=role_data["created_by_username"]
                    )

                # Parse expiration time for user role creation
                expiration_time = None
                if role_data.get("original_expiration_time"):
                    expiration_time = datetime.fromisoformat(
                        role_data["original_expiration_time"]
                    )

                # Recreate the UserRole
                user_role = UserRole.objects.create(
                    user=user,
                    role=role,
                    scope=project,
                    created_by=created_by,
                    expiration_time=expiration_time,
                    is_active=True,
                )

                # Mark as restored in metadata
                role_data["is_restored"] = True
                role_data["restored_at"] = timezone.now().isoformat()
                role_data["restored_by"] = restored_by_user.username

                restored_roles.append(user_role)

            except (User.DoesNotExist, Role.DoesNotExist) as e:
                # Log error and continue with other roles
                logger.warning(f"Could not restore role for project {project.id}: {e}")
                continue

        # Save updated metadata
        if user_roles_data:  # Only save if there was metadata to process
            project.save(update_fields=["termination_metadata"])

        return restored_roles

    def _send_invitations_to_previous_members(self, project, invited_by_user):
        """Send invitations to users who had access before project termination."""
        User = get_user_model()

        if (
            not project.termination_metadata
            or "user_roles" not in project.termination_metadata
        ):
            return []

        sent_invitations = []
        user_roles_data = project.termination_metadata["user_roles"]

        for role_data in user_roles_data:
            if role_data.get("invitation_sent", False):
                continue  # Skip if invitation already sent

            try:
                user, role = self._validate_user_role_data(role_data, project)
                if not user or not role:
                    # Check if users/roles exist and mark existing role invitation as sent
                    try:
                        existing_user = User.objects.get(
                            username=role_data["user_username"]
                        )
                        existing_role = Role.objects.get(name=role_data["role_name"])
                        existing_user_role = UserRole.objects.filter(
                            user=existing_user,
                            role=existing_role,
                            scope=project,
                            is_active=True,
                        ).first()
                        if existing_user_role:
                            role_data["invitation_sent"] = True
                            role_data["invitation_sent_at"] = timezone.now().isoformat()
                            role_data["invitation_sent_by"] = invited_by_user.username
                    except (User.DoesNotExist, Role.DoesNotExist):
                        pass  # Skip invalid users/roles
                    continue

                # Check if there's already a pending invitation for this user and role
                project_ct = ContentType.objects.get_for_model(project)
                existing_invitation = Invitation.objects.filter(
                    email=user.email,
                    role=role,
                    content_type=project_ct,
                    object_id=project.id,
                    state=InvitationState.PENDING,
                ).first()
                if existing_invitation:
                    # Mark as already invited
                    role_data["invitation_sent"] = True
                    role_data["invitation_sent_at"] = timezone.now().isoformat()
                    role_data["invitation_sent_by"] = invited_by_user.username
                    role_data["existing_invitation_uuid"] = str(
                        existing_invitation.uuid
                    )
                    sent_invitations.append(existing_invitation)
                    continue

                # Create invitation
                invitation = Invitation.objects.create(
                    email=user.email,
                    role=role,
                    scope=project,
                    created_by=invited_by_user,
                    state=InvitationState.PENDING,
                    customer=project.customer,
                    full_name=user.full_name or user.get_full_name(),
                    extra_invitation_text=f"You previously had {role.name} access to this project before it was temporarily removed.",
                )

                # Send invitation email
                sender = invited_by_user.full_name or invited_by_user.username
                user_tasks.send_invitation_created.delay(invitation.uuid.hex, sender)

                # Mark as invitation sent in metadata
                role_data["invitation_sent"] = True
                role_data["invitation_sent_at"] = timezone.now().isoformat()
                role_data["invitation_sent_by"] = invited_by_user.username
                role_data["invitation_uuid"] = str(invitation.uuid)

                sent_invitations.append(invitation)

            except (User.DoesNotExist, Role.DoesNotExist) as e:
                # Log error and continue with other roles
                logger.warning(
                    f"Could not send invitation for project {project.id}: {e}"
                )
                continue

        # Save updated metadata
        if user_roles_data:  # Only save if there was metadata to process
            project.save(update_fields=["termination_metadata"])

        return sent_invitations

    recover_serializer_class = serializers.ProjectRecoverySerializer
    recover_permissions = [
        permission_factory(PermissionEnum.CREATE_PROJECT, ["customer"])
    ]


@extend_schema(
    parameters=[PROJECT_UUID_PARAMETER],
    description="A list of users which can be added to the "
    "current project from other projects of the same customer.",
)
class ProjectOtherUsersViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = serializers.BasicUserSerializer
    filter_backends = [
        filters.GenericRoleFilter,
        filters.ConcatenatedNameOrderingBackend,
        DjangoFilterBackend,
    ]
    filterset_class = filters.BaseUserFilter
    queryset = core_models.User.objects.none()

    def get_queryset(self) -> QuerySet[core_models.User]:
        project = models.Project.objects.get(uuid=self.kwargs["project_uuid"])
        projects = (
            models.Project.objects.filter(customer=project.customer)
            .filter(id__in=get_connected_projects(self.request.user))
            .exclude(id=project.id)
        ).values_list("id", flat=True)

        return core_models.User.objects.filter(id__in=get_project_users(projects))


class UserViewSet(core_views.HistoryViewSetMixin, core_views.ActionsViewSet):
    queryset = core_models.User.all_objects.select_related(
        "auth_token", "changeemailrequest"
    )
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
        # Prefetch user permissions to avoid N+1 queries in UserSerializer.get_permissions
        permissions_prefetch = Prefetch(
            "userrole_set",
            queryset=UserRole.objects.filter(is_active=True).select_related(
                "user", "role", "created_by", "content_type"
            ),
            to_attr="prefetched_permissions",
        )
        qs = qs.prefetch_related(permissions_prefetch)
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
        response = super().list(request, *args, **kwargs)
        # Flush buffered GDPR data access logs as a single bulk INSERT
        entries = getattr(self, "_data_access_log_entries", None)
        if entries:
            bulk_log_user_data_access(entries, request.user, request)
            del self._data_access_log_entries
        return response

    @extend_schema(
        summary="Request email change",
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
        summary="Cancel email change request",
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
        summary="Trigger SCIM synchronization for all users",
        request=None,
        responses=serializers.ScimSyncAllResponseSerializer,
        description="Staff-only action to queue SCIM synchronization for all users.",
    )
    @action(
        detail=False,
        methods=["post"],
        permission_classes=[rf_permissions.IsAuthenticated, core_permissions.IsStaff],
    )
    def scim_sync_all(self, request):
        scim_tasks.sync_all_entitlements.delay()
        return Response(
            {"detail": _("SCIM synchronization has been scheduled.")},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Confirm email change",
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
        summary="Get current user details",
        description="Get current user details, including authentication token and profile completeness status.",
        parameters=[],
    )
    @action(detail=False, methods=["get"])
    def me(self, request):
        serializer = self.get_serializer(request.user)
        response_data = serializer.data
        response_data["ip_address"] = get_ip_address(request)
        response_data["profile_completeness"] = get_profile_completeness_details(
            request.user
        )

        return Response(
            response_data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Check profile completeness",
        description="Check if user profile is complete with all mandatory attributes.",
        responses={200: serializers.ProfileCompletenessSerializer},
    )
    @action(detail=False, methods=["get"])
    def profile_completeness(self, request):
        """Check if user profile is complete with all mandatory attributes."""
        completeness = get_profile_completeness_details(request.user)
        return Response(completeness, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get user data access visibility",
        description=(
            "Shows who has access to the user's profile data. "
            "Includes administrative access (staff/support), organizational access "
            "(same customer/project), and service provider access (via consent). "
            "Regular users see counts for admin access; staff/support see individual records."
        ),
        responses={200: UserDataAccessSerializer},
    )
    @action(detail=True, methods=["get"])
    def data_access(self, request, uuid=None):
        user = self.get_object()

        # Only allow users to view their own data access, or staff/support
        if not (
            request.user == user or request.user.is_staff or request.user.is_support
        ):
            raise PermissionDenied(
                _("You do not have permission to view this user's data access.")
            )

        # Tiered visibility: staff/support see individual admin records,
        # regular users see only counts for administrative access
        include_admin_details = request.user.is_staff or request.user.is_support

        data = get_user_data_access_visibility(user, include_admin_details)
        serializer = UserDataAccessSerializer(data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get user data access history",
        description=(
            "Shows historical log of who has accessed the user's profile data. "
            "Regular users see anonymized accessor categories. "
            "Staff/support see full details including accessor identity, IP, and context."
        ),
        parameters=[
            OpenApiParameter(
                "start_date",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Filter logs from this date (inclusive)",
            ),
            OpenApiParameter(
                "end_date",
                type=OpenApiTypes.DATE,
                location=OpenApiParameter.QUERY,
                description="Filter logs until this date (inclusive)",
            ),
            OpenApiParameter(
                "accessor_type",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by accessor type (staff, support, organization_member, self)",
            ),
        ],
        responses={200: UserDataAccessLogSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def data_access_history(self, request, uuid=None):
        user = self.get_object()

        # Only allow users to view their own history, or staff/support
        if not (
            request.user == user or request.user.is_staff or request.user.is_support
        ):
            raise PermissionDenied(
                _("You do not have permission to view this user's data access history.")
            )

        # Get access logs for the user
        queryset = UserDataAccessLog.objects.filter(target_user=user).select_related(
            "accessor"
        )

        # Apply filters
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        accessor_type = request.query_params.get("accessor_type")

        if start_date:
            queryset = queryset.filter(timestamp__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(timestamp__date__lte=end_date)
        if accessor_type:
            queryset = queryset.filter(accessor_type=accessor_type)

        # Paginate results
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = UserDataAccessLogSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = UserDataAccessLogSerializer(
            queryset, many=True, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Synchronize user details from eduTEAMS",
        request=None,
        responses=None,
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
        summary="Recalculate user actions for a specific user",
        request=user_action_serializers.UpdateActionsSerializer,
        responses={202: user_action_serializers.UpdateActionsResponseSerializer},
        description="Staff-only action to trigger recalculation of user actions for a specific user.",
    )
    @action(detail=True, methods=["post"])
    def update_actions(self, request, uuid=None):
        user = self.get_object()
        serializer = user_action_serializers.UpdateActionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provider_action_type = serializer.validated_data.get("provider_action_type")

        user_action_tasks.update_user_actions.delay(
            user_uuid=user.uuid.hex,
            provider_action_type=provider_action_type,
        )

        response_data = {
            "status": "scheduled",
            "message": f"User actions update for {user.username} has been scheduled",
            "provider_action_type": provider_action_type,
        }
        response_serializer = user_action_serializers.UpdateActionsResponseSerializer(
            response_data
        )
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)

    update_actions_permissions = [permissions.is_staff]
    update_actions_serializer_class = user_action_serializers.UpdateActionsSerializer

    @extend_schema(
        summary="Send action notification to a specific user",
        request=None,
        responses={202: user_action_serializers.SendNotificationResponseSerializer},
        description="Staff-only action to send a pending actions digest notification to a specific user.",
    )
    @action(detail=True, methods=["post"])
    def send_notification(self, request, uuid=None):
        user = self.get_object()
        if not user.email:
            raise ValidationError(_("User does not have an email address."))

        user_action_tasks.send_user_action_notification.delay(
            user_uuid=user.uuid.hex,
        )

        response_data = {
            "status": "scheduled",
            "message": f"Notification for {user.username} has been scheduled",
        }
        response_serializer = (
            user_action_serializers.SendNotificationResponseSerializer(response_data)
        )
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)

    send_notification_permissions = [permissions.is_staff]

    @extend_schema(
        summary="Change user password",
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
        summary="Remove user password",
        request=None,
        responses=None,
        description="Allows staff user to remove password for any user, making it unusable.",
    )
    @action(detail=True, methods=["post"])
    def remove_password(self, request, uuid=None):
        user = self.get_object()
        user.set_unusable_password()
        user.save(update_fields=["password"])

        event_logger.emit(
            "Password has been removed for user {affected_user_username} by %s."
            % self.request.user,
            event_type=EventType.USER_PASSWORD_REMOVED_BY_STAFF,
            event_context={"affected_user": user},
            scopes=[user],
        )
        logger.info(
            f"Password has been removed for user {user} by {self.request.user}."
        )

        return Response({"status": "password removed"}, status=status.HTTP_200_OK)

    remove_password_permissions = [permissions.is_staff]

    @extend_schema(
        summary="Get user auth token",
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
        request=None,
        responses=serializers.UserAuthTokenSerializer,
        summary="Refresh user auth token",
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

    @extend_schema(
        summary="Get user counts by active status",
        responses={200: serializers.UserActiveStatusCountSerializer(many=True)},
        description="Returns aggregated counts of users by active/inactive status. Staff or support only.",
    )
    @action(detail=False, methods=["get"])
    def user_active_status_count(self, request):
        """Get user counts grouped by active status."""
        qs = core_models.User.all_objects.all()
        active_count = qs.filter(is_active=True).count()
        inactive_count = qs.filter(is_active=False).count()

        data = [
            {"status": "active", "count": active_count},
            {"status": "inactive", "count": inactive_count},
        ]
        serializer = serializers.UserActiveStatusCountSerializer(data, many=True)
        return Response(serializer.data)

    user_active_status_count_permissions = [permissions.is_staff_or_support]

    @extend_schema(
        summary="Get user counts by preferred language",
        responses={200: serializers.UserLanguageCountSerializer(many=True)},
        description="Returns aggregated counts of users by preferred language. Staff or support only.",
    )
    @action(detail=False, methods=["get"])
    def user_language_count(self, request):
        """Get user counts grouped by preferred language."""
        qs = core_models.User.all_objects.filter(is_active=True)
        language_counts = (
            qs.values("preferred_language")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        data = [
            {
                "language": item["preferred_language"] or "unset",
                "count": item["count"],
            }
            for item in language_counts
        ]
        serializer = serializers.UserLanguageCountSerializer(data, many=True)
        return Response(serializer.data)

    user_language_count_permissions = [permissions.is_staff_or_support]

    @extend_schema(
        summary="Get user registration trends by month",
        responses={200: serializers.UserRegistrationTrendSerializer(many=True)},
        description="Returns user registration counts aggregated by month. Staff or support only.",
    )
    @action(detail=False, methods=["get"])
    def user_registration_trend(self, request):
        """Get user registration counts grouped by month."""
        qs = core_models.User.all_objects.all()
        monthly_counts = (
            qs.annotate(month=TruncMonth("date_joined"))
            .values("month")
            .annotate(count=Count("id"))
            .order_by("month")
        )

        data = [
            {
                "month": item["month"].strftime("%Y-%m")
                if item["month"]
                else "unknown",
                "count": item["count"],
            }
            for item in monthly_counts
        ]
        serializer = serializers.UserRegistrationTrendSerializer(data, many=True)
        return Response(serializer.data)

    user_registration_trend_permissions = [permissions.is_staff_or_support]

    @extend_schema(
        summary="Get identity bridge status for a user",
        responses={200: serializers.IdentityBridgeUserStatusSerializer},
        description=(
            "Returns diagnostic information about a user's identity bridge state: "
            "active ISDs, per-attribute source tracking with staleness detection, "
            "and effective bridge-writable fields. Staff only."
        ),
    )
    @action(detail=True, methods=["get"])
    def identity_bridge_status(self, request, uuid=None):
        """Get identity bridge diagnostic info for a user."""
        user = self.get_object()
        attribute_sources = user.attribute_sources or {}
        now = timezone.now()
        stale_threshold_days = 7

        enriched_sources = {}
        stale_attributes = []
        for field, info in attribute_sources.items():
            if isinstance(info, dict):
                source = info.get("source", "")
                timestamp = info.get("timestamp", "")
            else:
                source = str(info)
                timestamp = ""

            if timestamp:
                try:
                    ts = datetime.fromisoformat(timestamp)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=now.tzinfo)
                    age = now - ts
                    age_days = round(age.total_seconds() / 86400, 1)
                except (ValueError, TypeError):
                    age_days = -1
            else:
                age_days = -1

            is_stale = age_days > stale_threshold_days or age_days < 0
            if is_stale:
                stale_attributes.append(field)

            enriched_sources[field] = {
                "source": source,
                "timestamp": timestamp,
                "age_days": age_days,
                "is_stale": is_stale,
            }

        from waldur_core.core.user_attributes import (
            get_federated_identity_sync_allowed_fields,
        )

        data = {
            "active_isds": user.active_isds or [],
            "managed_isds": user.managed_isds or [],
            "attribute_sources": enriched_sources,
            "stale_attributes": sorted(stale_attributes),
            "effective_bridge_fields": sorted(
                get_federated_identity_sync_allowed_fields()
            ),
            "is_federated": bool(user.active_isds),
        }
        return Response(data, status=status.HTTP_200_OK)

    identity_bridge_status_permissions = [permissions.is_staff]
    identity_bridge_status_serializer_class = (
        serializers.IdentityBridgeUserStatusSerializer
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
        summary="Close customer permission review",
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def close(self, request, uuid=None):
        review: models.CustomerPermissionReview = self.get_object()
        if not review.is_pending:
            raise ValidationError(_("Review is already closed."))
        review.close(request.user)
        event_logger.emit(
            "Customer permission review has been closed for organization %s."
            % review.customer.name,
            event_type=EventType.CUSTOMER_PERMISSION_REVIEW_CLOSED,
            event_context={"customer_permission_review": review},
            scopes=[review.customer],
        )
        return Response(status=status.HTTP_200_OK)


class ProjectPermissionReviewViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = models.ProjectPermissionReview.objects.all()
    serializer_class = serializers.ProjectPermissionReviewSerializer
    filter_backends = (
        filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.ProjectPermissionReviewFilter
    lookup_field = "uuid"

    @extend_schema(
        summary="Close project permission review",
        request=None,
        responses=None,
        description="Complete project permission review.",
    )
    @action(detail=True, methods=["post"])
    def close(self, request, uuid=None):
        review: models.ProjectPermissionReview = self.get_object()
        if not review.is_pending:
            raise ValidationError(_("Review is already completed."))
        review.close(request.user)
        event_logger.emit(
            "Project permission review has been closed for project %s."
            % review.project.name,
            event_type=EventType.PROJECT_PERMISSION_REVIEW_CLOSED,
            event_context={"project_permission_review": review},
            scopes=[review.project],
        )
        return Response(status=status.HTTP_200_OK)


def user_can_approve_project_end_date_change_request(
    request, view, obj: models.ProjectEndDateChangeRequest | None = None
):
    """Only users with UPDATE_PROJECT on customer or project can approve/reject."""
    if not obj:
        return
    if has_permission(
        request.user, PermissionEnum.UPDATE_PROJECT, obj.project.customer
    ) or has_permission(request.user, PermissionEnum.UPDATE_PROJECT, obj.project):
        return
    raise PermissionDenied()


class ProjectEndDateChangeRequestViewSet(
    core_mixins.EagerLoadMixin, core_views.ActionsViewSet
):
    queryset = models.ProjectEndDateChangeRequest.objects.all()
    approve_permissions = reject_permissions = [
        user_can_approve_project_end_date_change_request
    ]
    serializer_class = serializers.ProjectEndDateChangeRequestSerializer
    create_serializer_class = serializers.ProjectEndDateChangeRequestCreateSerializer
    filter_backends = [filters.GenericRoleFilter, DjangoFilterBackend]
    filterset_class = filters.ProjectEndDateChangeRequestFilter
    disabled_actions = ["update", "partial_update", "destroy"]
    lookup_field = "uuid"

    @extend_schema(
        request=ReviewCommentSerializer,
        responses=None,
        description="Approve project end date change request",
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, **kwargs):
        review_request: models.ProjectEndDateChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        with transaction.atomic():
            review_request.approve(request.user, comment)
            # Update project end_date on approval
            review_request.project.end_date = review_request.requested_end_date
            review_request.project.end_date_requested_by = request.user
            review_request.project.save(
                update_fields=["end_date", "end_date_requested_by"]
            )
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=ReviewCommentSerializer,
        responses=None,
        description="Reject project end date change request",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, **kwargs):
        review_request: models.ProjectEndDateChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        review_request.reject(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=None,
        responses=None,
        description="Cancel project end date change request. Only the creator can cancel.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, **kwargs):
        review_request: models.ProjectEndDateChangeRequest = self.get_object()
        if review_request.created_by != request.user:
            raise PermissionDenied(
                _("You can only cancel your own project end date change requests.")
            )
        review_request.cancel()
        return Response(
            {"detail": _("Project end date change request has been canceled.")},
            status=status.HTTP_200_OK,
        )

    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer
    approve_validators = reject_validators = cancel_validators = [
        core_validators.StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)
    ]


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
    core_views.HistoryViewSetMixin,
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

    queryset = core_models.SshPublicKey.objects.select_related("user").all()
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

    @extend_schema(
        summary="Synchronize resource state",
        description=(
            "Schedule an asynchronous pull operation to synchronize resource state from the "
            "backend. Returns 202 if the pull was scheduled successfully, or 409 if the "
            "pull operation is not implemented for this resource type."
        ),
        request=None,
        responses={
            202: inline_serializer(
                "PullResponse",
                fields={"detail": rf_serializers.CharField()},
            ),
            409: inline_serializer(
                "PullConflictResponse",
                fields={"detail": rf_serializers.CharField()},
            ),
        },
    )
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

    @extend_schema(
        summary="Unlink resource",
        description="""Delete resource from the database without scheduling operations on backend
        and without checking current state of the resource. It is intended to be used
        for removing resource stuck in transitioning state.""",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        obj = self.get_object()
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    unlink_permissions = [permissions.is_staff]

    set_erred_serializer_class = serializers.SetErredSerializer

    @extend_schema(
        summary="Mark resource as ERRED",
        description=(
            "Manually transition the resource to ERRED state. "
            "This is useful for resources stuck in transitional states "
            "(CREATING, UPDATING, DELETING) that cannot be synced via pull. "
            "Staff-only operation."
        ),
        responses={
            200: inline_serializer(
                "SetErredResponse",
                fields={"detail": rf_serializers.CharField()},
            )
        },
    )
    @action(detail=True, methods=["post"])
    def set_erred(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource.error_message = serializer.validated_data.get("error_message", "")
        resource.error_traceback = serializer.validated_data.get("error_traceback", "")
        resource.set_erred()
        resource.save(update_fields=["state", "error_message", "error_traceback"])
        return Response(
            {"detail": _("Resource has been marked as ERRED.")},
            status=status.HTTP_200_OK,
        )

    set_erred_permissions = [permissions.is_staff]

    @extend_schema(
        summary="Mark resource as OK",
        description=(
            "Manually transition the resource to OK state and clear error fields. "
            "Staff-only operation."
        ),
        request=None,
        responses={
            200: inline_serializer(
                "SetOkResponse",
                fields={"detail": rf_serializers.CharField()},
            )
        },
    )
    @action(detail=True, methods=["post"])
    def set_ok(self, request, uuid=None):
        resource = self.get_object()
        resource.error_message = ""
        resource.error_traceback = ""
        resource.set_ok()
        resource.save(update_fields=["state", "error_message", "error_traceback"])
        return Response(
            {"detail": _("Resource has been marked as OK.")},
            status=status.HTTP_200_OK,
        )

    set_ok_permissions = [permissions.is_staff]


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


class AffiliatedOrganizationStatsSerializer(rf_serializers.Serializer):
    active_projects_count = rf_serializers.IntegerField()
    resources_count = rf_serializers.IntegerField()
    estimated_monthly_cost = rf_serializers.DecimalField(
        max_digits=22, decimal_places=10
    )


class AffiliatedOrganizationReportRowSerializer(rf_serializers.Serializer):
    org_uuid = rf_serializers.UUIDField(allow_null=True)
    org_name = rf_serializers.CharField()
    org_abbreviation = rf_serializers.CharField()
    projects_count = rf_serializers.IntegerField()
    resources_count = rf_serializers.IntegerField()
    estimated_cost = rf_serializers.DecimalField(max_digits=22, decimal_places=10)


class AffiliatedOrganizationViewSet(core_views.ActionsViewSet):
    queryset = (
        models.AffiliatedOrganization.objects.all()
        .order_by("name")
        .annotate(
            projects_count=Count(
                "projects",
                filter=Q(projects__is_removed=False),
            )
        )
    )
    serializer_class = serializers.AffiliatedOrganizationSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, rf_filters.OrderingFilter)
    filterset_class = filters.AffiliatedOrganizationFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)
    ordering_fields = ("name", "projects_count", "created")

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if user.is_staff or user.is_support:
            return qs
        # Non-staff: only affiliations approved for at least one customer the
        # user has a role in. The optional ?default_for_customer filter
        # narrows further within this scope.
        user_customers = get_connected_customers(user=user)
        return qs.filter(default_for_customers__in=user_customers).distinct()

    @extend_schema(
        summary="Get affiliated organization statistics",
        responses={200: AffiliatedOrganizationStatsSerializer},
        description="Returns permission-filtered statistics for this affiliated organization.",
    )
    @action(detail=True, methods=["get"])
    def stats(self, request, uuid=None):
        org = self.get_object()
        user = request.user
        projects = org.projects.filter(is_removed=False)
        if not (user.is_staff or user.is_support):
            projects = filter_queryset_for_user(projects, user)

        active_projects_count = projects.count()
        resources_count = (
            marketplace_models.Resource.objects.filter(
                project__in=projects,
            )
            .exclude(state=ResourceStates.TERMINATED)
            .count()
        )
        estimated_monthly_cost = (
            marketplace_models.Resource.objects.filter(
                project__in=projects,
            )
            .exclude(state=ResourceStates.TERMINATED)
            .aggregate(total=Sum("cost"))["total"]
            or 0
        )

        data = {
            "active_projects_count": active_projects_count,
            "resources_count": resources_count,
            "estimated_monthly_cost": estimated_monthly_cost,
        }
        response_serializer = AffiliatedOrganizationStatsSerializer(data)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get affiliated organizations report",
        responses={200: AffiliatedOrganizationReportRowSerializer(many=True)},
        description="Staff-only report showing aggregated data for all affiliated organizations plus an unaffiliated row.",
    )
    @action(detail=False, methods=["get"])
    def report(self, request):
        if not request.user.is_staff:
            raise PermissionDenied()
        rows = []
        for org in models.AffiliatedOrganization.objects.all().order_by("name"):
            projects = org.projects.filter(is_removed=False)
            resources = marketplace_models.Resource.objects.filter(
                project__in=projects,
            ).exclude(state=ResourceStates.TERMINATED)
            rows.append(
                {
                    "org_uuid": org.uuid,
                    "org_name": org.name,
                    "org_abbreviation": org.abbreviation,
                    "projects_count": projects.count(),
                    "resources_count": resources.count(),
                    "estimated_cost": resources.aggregate(total=Sum("cost"))["total"]
                    or 0,
                }
            )

        # Unaffiliated row
        unaffiliated_projects = models.Project.available_objects.filter(
            affiliation__isnull=True
        )
        unaffiliated_resources = marketplace_models.Resource.objects.filter(
            project__in=unaffiliated_projects,
        ).exclude(state=ResourceStates.TERMINATED)
        rows.append(
            {
                "org_uuid": None,
                "org_name": "Unaffiliated",
                "org_abbreviation": "",
                "projects_count": unaffiliated_projects.count(),
                "resources_count": unaffiliated_resources.count(),
                "estimated_cost": unaffiliated_resources.aggregate(total=Sum("cost"))[
                    "total"
                ]
                or 0,
            }
        )
        response_serializer = AffiliatedOrganizationReportRowSerializer(rows, many=True)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    report_permissions = [permissions.is_staff]


class ScienceDomainViewSet(core_views.ActionsViewSet):
    queryset = (
        models.ScienceDomain.objects.all()
        .order_by(Length("code"), "code", "name")
        .annotate(subdomains_count=Count("subdomains"))
    )
    serializer_class = serializers.ScienceDomainSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ScienceDomainFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)

    @extend_schema(
        summary="List available science domain presets",
        responses={200: serializers.ScienceDomainPresetSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def presets(self, request):
        from waldur_core.structure.presets import SCIENCE_DOMAIN_PRESETS

        data = [
            {
                "name": name,
                "label": preset["label"],
                "description": preset["description"],
            }
            for name, preset in SCIENCE_DOMAIN_PRESETS.items()
        ]
        serializer = serializers.ScienceDomainPresetSerializer(data, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Load a science domain preset",
        request=serializers.LoadScienceDomainPresetSerializer,
        responses={200: serializers.LoadScienceDomainPresetResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def load_preset(self, request):
        serializer = serializers.LoadScienceDomainPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from waldur_core.structure.presets import load_preset

        result = load_preset(serializer.validated_data["preset"])
        response_serializer = serializers.LoadScienceDomainPresetResponseSerializer(
            result
        )
        return Response(response_serializer.data)

    load_preset_permissions = [permissions.is_staff]


class ScienceSubDomainViewSet(core_views.ActionsViewSet):
    queryset = (
        models.ScienceSubDomain.objects.all()
        .select_related("domain")
        .order_by(Length("code"), "code")
        .annotate(
            projects_count=Count(
                "projects",
                filter=Q(projects__is_removed=False),
            )
        )
    )
    serializer_class = serializers.ScienceSubDomainSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ScienceSubDomainFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)


@extend_schema_view(
    list=extend_schema(
        summary="List user agreements",
        description="Retrieve a list of user agreements (Terms of Service and Privacy Policy). Supports filtering by agreement type and language with fallback behavior.",
        parameters=[
            OpenApiParameter(
                "language",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="ISO 639-1 language code (e.g., 'en', 'de', 'et'). Returns requested language or falls back to default version if unavailable.",
            )
        ],
    ),
    retrieve=extend_schema(
        summary="Retrieve user agreement",
        description="Fetch the details of a specific user agreement by its UUID.",
    ),
)
class UserAgreementsViewSet(ActionsViewSet):
    serializer_class = serializers.UserAgreementSerializer
    permission_classes = (core_permissions.ActionsPermission,)
    unsafe_methods_permissions = [permissions.is_staff]
    filterset_class = filters.UserAgreementsFilter
    lookup_field = "uuid"
    queryset = models.UserAgreement.objects.all()

    def get_queryset(self):
        queryset = super().get_queryset()
        language = self.request.query_params.get("language")

        if not language:
            return queryset

        agreement_type = self.request.query_params.get("agreement_type")

        if agreement_type:
            # Single agreement type requested - try exact match, then fallback
            exact_match = queryset.filter(
                agreement_type=agreement_type, language=language
            )
            if exact_match.exists():
                return exact_match
            # Fallback to default (empty language)
            return queryset.filter(agreement_type=agreement_type, language="")

        # Multiple agreement types - get language version or default for each
        result_ids = []
        for at in models.UserAgreement.UserAgreements.CHOICES:
            agreement_type_code = at[0]
            match = queryset.filter(
                agreement_type=agreement_type_code, language=language
            ).first()
            if match:
                result_ids.append(match.id)
            else:
                default = queryset.filter(
                    agreement_type=agreement_type_code, language=""
                ).first()
                if default:
                    result_ids.append(default.id)

        return queryset.filter(id__in=result_ids)


class NotificationViewSet(ActionsViewSet):
    queryset = core_models.Notification.objects.all().order_by("id")
    serializer_class = serializers.NotificationSerializer
    permission_classes = (PATScopeAwareIsAdminUser,)
    filterset_class = filters.NotificationFilter
    lookup_field = "uuid"

    @extend_schema(
        summary="Enable a notification",
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
        summary="Disable a notification",
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
        summary="Override notification template content",
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

        # Explicitly refresh the dbtemplates cache entry.  remove_cached_template()
        # would be a no-op here because a freshly-created Template has no sites yet,
        # and it never clears the "notfound" sentinel the loader plants on a DB miss.
        # add_template_to_cache() does all three steps: removes the old positive entry,
        # removes the notfound sentinel, and writes the new content into cache — so the
        # override takes effect on the very next email send without a process restart.
        add_template_to_cache(template_dbtemplates)
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


@extend_schema_view(
    list=extend_schema(
        summary="List external links",
        description="Retrieve a list of external links available in the system.",
    ),
    retrieve=extend_schema(
        summary="Retrieve external link",
        description="Fetch the details of a specific external link by its UUID.",
    ),
    create=extend_schema(
        summary="Create an external link",
        description="Create a new external link. This action is restricted to staff users.",
    ),
    update=extend_schema(
        summary="Update an external link",
        description="Update an existing external link. This action is restricted to staff users.",
    ),
    partial_update=extend_schema(
        summary="Partially update an external link",
        description="Partially update an existing external link. This action is restricted to staff users.",
    ),
    destroy=extend_schema(
        summary="Delete an external link",
        description="Delete an existing external link. This action is restricted to staff users.",
    ),
)
class ExternalLinkViewSet(viewsets.ModelViewSet):
    queryset = models.ExternalLink.objects.all()
    serializer_class = serializers.ExternalLinkSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, rf_filters.OrderingFilter)
    filterset_class = filters.ExternalLinkFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)
    ordering_fields = ("name", "url")


@extend_schema(
    parameters=[CUSTOMER_UUID_PARAMETER],
    summary="Get project metadata compliance overview",
    description="Provides aggregated statistics about project metadata compliance for all projects within a customer.",
)
class CustomerProjectMetadataComplianceOverviewViewSet(
    mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    ViewSet for customer project metadata compliance overview statistics.

    Provides aggregated compliance statistics across all projects in the customer.
    """

    serializer_class = serializers.ComplianceOverviewSerializer
    queryset = models.Customer.objects.none()

    def get_customer(self):
        """Get customer and check permissions."""
        customer = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        if not has_permission(
            self.request,
            PermissionEnum.LIST_PROJECTS,
            customer,
        ):
            raise PermissionDenied()
        return customer

    def list(self, request, customer_uuid=None):
        """Get compliance overview statistics for all projects."""
        customer = self.get_customer()

        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return Response(
                {
                    "detail": "No project metadata checklist configured for this customer."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = customer.project_metadata_checklist

        # Get ContentType for Project (cached after first call)
        content_type = ContentType.objects.get_for_model(models.Project)

        # Get all projects for this customer
        projects = models.Project.objects.filter(customer=customer).order_by("name")

        # Get all completion data in bulk
        project_ids = list(projects.values_list("id", flat=True))
        completion_data = {}

        if project_ids:
            # Bulk query for all completions related to projects
            completions = checklist_models.ChecklistCompletion.objects.filter(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id__in=project_ids,
            )

            for completion in completions:
                completion_data[completion.scope_object_id] = {
                    "is_completed": completion.is_completed,
                    "requires_review": completion.requires_review,
                    "completion_percentage": completion.get_completion_percentage(),
                }

        # Calculate statistics
        total_projects = len(project_ids)
        projects_with_completions = len(completion_data)
        fully_completed_projects = sum(
            1 for data in completion_data.values() if data["is_completed"]
        )
        projects_requiring_review = sum(
            1 for data in completion_data.values() if data["requires_review"]
        )

        # Calculate average completion percentage
        if completion_data:
            total_percentage = sum(
                data["completion_percentage"] for data in completion_data.values()
            )
            average_completion_percentage = round(total_percentage / total_projects, 1)
        else:
            average_completion_percentage = 0.0

        # Create response data
        response_data = {
            "total_projects": total_projects,
            "projects_with_completions": projects_with_completions,
            "fully_completed_projects": fully_completed_projects,
            "projects_requiring_review": projects_requiring_review,
            "average_completion_percentage": average_completion_percentage,
        }

        # Use serializer for response
        serializer = self.get_serializer(response_data)
        return Response(serializer.data)


@extend_schema(
    parameters=[CUSTOMER_UUID_PARAMETER],
    summary="Get detailed project metadata compliance",
    description="Provides detailed compliance status for all projects within a customer, including individual answers and completion status.",
)
class CustomerProjectMetadataComplianceDetailsViewSet(
    mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    ViewSet for detailed customer project metadata compliance information.

    Provides detailed compliance status for all projects with individual completion data.
    """

    queryset = models.Project.objects.none()  # Required for schema generation
    serializer_class = serializers.ProjectDetailsResponseSerializer

    def get_customer(self):
        """Get customer and check permissions."""
        customer = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        if not has_permission(
            self.request,
            PermissionEnum.LIST_PROJECTS,
            customer,
        ):
            raise PermissionDenied()
        return customer

    def get_queryset(self):
        """Get projects for the customer."""
        customer = self.get_customer()
        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return models.Project.objects.none()
        return models.Project.objects.filter(customer=customer).order_by("name")

    def list(self, request, customer_uuid=None):
        """Get detailed project compliance information."""
        customer = self.get_customer()

        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return Response(
                {
                    "detail": "No project metadata checklist configured for this customer."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = customer.project_metadata_checklist

        # Use database-level pagination by paginating the queryset first
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)

        if page is not None:
            # Now bulk-load completion data only for projects on this page
            self._bulk_load_completion_data(page, checklist)
            project_details = self._build_project_details(page)

            # For statistics, use efficient count queries instead of loading all data
            content_type = ContentType.objects.get_for_model(models.Project)
            all_projects_count = models.Project.objects.filter(
                customer=customer
            ).count()
            completions_count = checklist_models.ChecklistCompletion.objects.filter(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id__in=models.Project.objects.filter(
                    customer=customer
                ).values("id"),
            ).count()
            fully_completed_count = checklist_models.ChecklistCompletion.objects.filter(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id__in=models.Project.objects.filter(
                    customer=customer
                ).values("id"),
                is_completed=True,
            ).count()
            projects_requiring_review_count = (
                checklist_models.ChecklistCompletion.objects.filter(
                    checklist=checklist,
                    scope_content_type=content_type,
                    scope_object_id__in=models.Project.objects.filter(
                        customer=customer
                    ).values("id"),
                    requires_review=True,
                ).count()
            )

            response_data = {
                "checklist": {
                    "uuid": checklist.uuid.hex,
                    "name": checklist.name,
                    "checklist_type": checklist.checklist_type,
                },
                "total_projects": all_projects_count,
                "projects_with_completions": completions_count,
                "fully_completed_projects": fully_completed_count,
                "projects_requiring_review": projects_requiring_review_count,
                "project_details": project_details,
            }

            serializer = self.get_serializer(response_data)
            return self.get_paginated_response(serializer.data)

        # Fallback (shouldn't happen with pagination class)
        return Response({"project_details": []})

    def _bulk_load_completion_data(self, projects, checklist):
        """Bulk load completion data for the given projects."""
        content_type = ContentType.objects.get_for_model(models.Project)
        project_ids = [project.id for project in projects]

        if project_ids:
            completions = checklist_models.ChecklistCompletion.objects.filter(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id__in=project_ids,
            ).prefetch_related("answers__question__question_options", "answers__user")

            # Attach completion data to projects
            completion_map = {
                completion.scope_object_id: completion for completion in completions
            }
            for project in projects:
                project._completion_cache = completion_map.get(project.id)

    def _build_project_details(self, projects):
        """Build project details for the given projects."""
        customer = self.get_customer()
        checklist = customer.project_metadata_checklist

        # Get all questions with prefetched options for efficiency
        questions = list(
            checklist.questions.prefetch_related("question_options").order_by("order")
        )

        # Build question options map for all questions upfront
        question_options_map = {}
        question_data_map = {}

        for question in questions:
            # Store question data for quick access
            question_options = []
            options_map = {}

            if question.question_type in ["single_select", "multi_select"]:
                options = list(question.question_options.all())
                sorted_options = sorted(options, key=lambda opt: opt.order)

                # Build options list for API response
                question_options = [
                    {
                        "uuid": str(option.uuid),
                        "label": option.label,
                        "order": option.order,
                    }
                    for option in sorted_options
                ]

                # Build options mapping for label conversion
                options_map = {
                    str(option.uuid): option.label for option in sorted_options
                }

            question_data_map[question.id] = {
                "uuid": str(question.uuid),
                "description": question.description,
                "question_type": question.question_type,
                "required": question.required,
                "min_value": question.min_value,
                "max_value": question.max_value,
                "question_options": question_options,
                "options_map": options_map,
            }
            question_options_map[question.id] = options_map

        project_details = []

        for project in projects:
            completion = getattr(project, "_completion_cache", None)

            if completion:
                completion_percentage = completion.get_completion_percentage()
                is_completed = completion.is_completed
                requires_review = completion.requires_review
                completion_uuid = completion.uuid.hex

                # Build answers with pre-computed data
                answers = []
                answered_question_ids = set()

                for answer in completion.answers.all():
                    question_id = answer.question_id
                    answered_question_ids.add(question_id)

                    # Get pre-computed question data
                    question_data = question_data_map[question_id]

                    # Get answer labels for select-type questions
                    answer_labels = None
                    if question_data["question_type"] in [
                        "single_select",
                        "multi_select",
                    ]:
                        options_map = question_data["options_map"]
                        answer_labels = self._get_answer_labels_for_compliance(
                            question_data["question_type"],
                            answer.answer_data,
                            options_map,
                        )

                    answers.append(
                        {
                            "question_uuid": question_data["uuid"],
                            "question_description": question_data["description"],
                            "question_type": question_data["question_type"],
                            "min_value": question_data["min_value"],
                            "max_value": question_data["max_value"],
                            "question_options": question_data["question_options"],
                            "answer_data": answer.answer_data,
                            "answer_labels": answer_labels,
                            "user_name": answer.user.full_name or answer.user.username,
                            "created": answer.created.isoformat(),
                            "modified": answer.modified.isoformat(),
                        }
                    )

                # Get unanswered required questions
                unanswered_required = [
                    {
                        "uuid": question_data["uuid"],
                        "description": question_data["description"],
                        "question_type": question_data["question_type"],
                        "min_value": question_data["min_value"],
                        "max_value": question_data["max_value"],
                    }
                    for question_id, question_data in question_data_map.items()
                    if question_data["required"]
                    and question_id not in answered_question_ids
                ]
            else:
                completion_percentage = 0.0
                is_completed = False
                requires_review = False
                completion_uuid = None
                answers = []
                # All required questions are unanswered if no completion
                unanswered_required = [
                    {
                        "uuid": question_data["uuid"],
                        "description": question_data["description"],
                        "question_type": question_data["question_type"],
                        "min_value": question_data["min_value"],
                        "max_value": question_data["max_value"],
                    }
                    for question_data in question_data_map.values()
                    if question_data["required"]
                ]

            project_details.append(
                {
                    "project_uuid": project.uuid.hex,
                    "project_name": project.name,
                    "completion_uuid": completion_uuid,
                    "completion_percentage": completion_percentage,
                    "is_completed": is_completed,
                    "requires_review": requires_review,
                    "answers": answers,
                    "unanswered_required_questions": unanswered_required,
                }
            )

        return project_details

    def _get_answer_labels_for_compliance(
        self, question_type, answer_data, options_map
    ):
        """Convert answer data UUIDs to human-readable labels for compliance details."""
        if not answer_data or not options_map:
            return None

        if (
            question_type == "single_select"
            and isinstance(answer_data, list)
            and len(answer_data) > 0
        ):
            return options_map.get(answer_data[0], answer_data[0])
        elif question_type == "multi_select" and isinstance(answer_data, list):
            return [options_map.get(uuid, uuid) for uuid in answer_data]

        return None


@extend_schema(
    parameters=[CUSTOMER_UUID_PARAMETER],
    summary="List projects with compliance data",
    description="Provides a paginated list of projects with their checklist completion status and answer details.",
)
class CustomerProjectMetadataComplianceProjectsViewSet(
    mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    ViewSet for listing customer projects with checklist compliance data.

    Provides paginated list of projects with their checklist completion and answer details.
    """

    queryset = models.Project.objects.none()  # Required for schema generation
    serializer_class = serializers.ProjectAnswerSerializer

    def get_customer(self):
        """Get customer and check permissions."""
        customer = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        if not has_permission(
            self.request,
            PermissionEnum.LIST_PROJECTS,
            customer,
        ):
            raise PermissionDenied()
        return customer

    def get_queryset(self):
        """Get projects for the customer with checklist data."""
        customer = self.get_customer()

        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return models.Project.objects.none()

        return models.Project.objects.filter(customer=customer).order_by("name")

    def get_serializer_context(self):
        """Add checklist to serializer context for efficient data loading."""
        context = super().get_serializer_context()

        try:
            customer = self.get_customer()
            context["checklist"] = customer.project_metadata_checklist
        except (AttributeError, models.Customer.DoesNotExist):
            context["checklist"] = None

        return context

    def list(self, request, customer_uuid=None):
        """List project checklist answer data."""
        customer = self.get_customer()

        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return Response(
                {
                    "detail": "No project metadata checklist configured for this customer."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        checklist = customer.project_metadata_checklist

        # Use database-level pagination by paginating the queryset first
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)

        if page is not None:
            # Now bulk-load completion data only for projects on this page
            self._bulk_load_completion_data(page, checklist)

            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        # No pagination - load completion data for all projects
        projects = list(queryset)
        self._bulk_load_completion_data(projects, checklist)

        serializer = self.get_serializer(projects, many=True)
        return Response(serializer.data)

    def _bulk_load_completion_data(self, projects, checklist):
        """Bulk load completion data for the given projects."""
        if not projects:
            return

        # Get ContentType for Project (cached after first call)
        content_type = ContentType.objects.get_for_model(models.Project)

        # Get project IDs for the current page only
        project_ids = [project.id for project in projects]

        # Bulk query for completions related to projects on this page
        completions = (
            checklist_models.ChecklistCompletion.objects.filter(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id__in=project_ids,
            ).prefetch_related("answers")  # Prefetch answers for counting
        )

        # Create completion mapping for the serializer
        completion_map = {}
        for completion in completions:
            completion_map[completion.scope_object_id] = completion

        # Attach bulk completion data to serializer class for efficient access
        serializer_class = self.get_serializer_class()
        serializer_class._bulk_completion_data = completion_map


@extend_schema(
    parameters=[CUSTOMER_UUID_PARAMETER],
    summary="List questions with project answers",
    description="Provides a paginated list of all questions from the customer's compliance checklist, including the answers given in each project.",
)
class CustomerProjectMetadataQuestionAnswersViewSet(
    mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    ViewSet for listing questions with their answers from all projects.

    Provides paginated list of questions with answers from all customer projects.
    Each question shows answers from all projects in the customer.
    """

    queryset = Question.objects.none()  # Required for schema generation
    serializer_class = serializers.QuestionAnswerSerializer

    def get_customer(self):
        """Get customer and check permissions."""
        customer = models.Customer.objects.get(uuid=self.kwargs["customer_uuid"])
        if not has_permission(
            self.request,
            PermissionEnum.LIST_PROJECTS,
            customer,
        ):
            raise PermissionDenied()
        return customer

    def get_queryset(self):
        """Get questions for the customer's checklist."""
        customer = self.get_customer()
        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return []

        return (
            Question.objects.filter(checklist=customer.project_metadata_checklist)
            .prefetch_related("question_options")
            .order_by("order")
        )

    def get_serializer_context(self):
        """Add customer to serializer context for efficient data loading."""
        context = super().get_serializer_context()

        try:
            customer = self.get_customer()
            context["customer"] = customer
        except (AttributeError, models.Customer.DoesNotExist):
            context["customer"] = None

        return context

    def list(self, request, customer_uuid=None):
        """List questions with project answers"""
        customer = self.get_customer()

        # Check if customer has project metadata checklist configured
        if not customer.project_metadata_checklist:
            return Response(
                {
                    "detail": "No project metadata checklist configured for this customer."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Use database-level pagination by paginating the questions queryset first
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)

        if page is not None:
            # Now bulk-load project and answer data only for questions on this page
            self._bulk_load_question_data(page, customer)

            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        # No pagination - load data for all questions
        questions = list(queryset)
        self._bulk_load_question_data(questions, customer)

        serializer = self.get_serializer(questions, many=True)
        return Response(serializer.data)

    def _bulk_load_question_data(self, questions, customer):
        """Bulk load project and answer data for the given questions."""
        if not questions:
            return

        # Get all projects for the customer (this is the same for all questions)
        projects = list(
            models.Project.objects.filter(customer=customer).order_by("name")
        )
        project_ct = ContentType.objects.get_for_model(models.Project)
        project_ids = [p.id for p in projects]

        # Bulk query for all answers for questions on this page
        question_ids = [q.id for q in questions]
        answers = Answer.objects.filter(
            question_id__in=question_ids,
            completion__scope_content_type=project_ct,
            completion__scope_object_id__in=project_ids,
        ).select_related("user", "completion")

        # Group answers by question_id
        answers_by_question = {}
        for answer in answers:
            question_id = answer.question_id
            if question_id not in answers_by_question:
                answers_by_question[question_id] = {}
            answers_by_question[question_id][answer.completion.scope_object_id] = answer

        # Create bulk data mapping for the serializer
        question_data_map = {}
        for question in questions:
            answers_by_project = answers_by_question.get(question.id, {})

            # Pre-build option UUID to label mapping for this question
            options_map = {}
            if question.question_type in ["single_select", "multi_select"]:
                # question_options is already prefetched
                options_map = {
                    str(option.uuid): option.label
                    for option in question.question_options.all()
                }

            question_data_map[question.id] = {
                "projects": projects,
                "answers_by_project": answers_by_project,
                "total_projects": len(projects),
                "answered_projects_count": len(answers_by_project),
                "options_map": options_map,  # Pre-computed for efficiency
            }

        # Attach bulk data to serializer class for efficient access
        serializer_class = self.get_serializer_class()
        serializer_class._bulk_question_data = question_data_map


class AvailabilityCheckViewMixin(viewsets.ModelViewSet):
    def get_object(self):
        obj = super().get_object()

        if (
            not getattr(obj, "can_be_managed", True)
            and self.request.method not in SAFE_METHODS
        ):
            raise ValidationError(_("Operation is not allowed. Object is unavailable."))

        return obj
