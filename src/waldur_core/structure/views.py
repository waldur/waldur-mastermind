import logging
from datetime import datetime

from dbtemplates.models import Template
from dbtemplates.utils.cache import remove_cached_template
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.db import transaction
from django.db.models import Count, Q, QuerySet
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
from waldur_core.checklist import mixins as checklist_mixins
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.models import Answer, ChecklistCompletion, Question
from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.utils import get_ip_address, is_uuid_like
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import (
    has_permission,
    permission_factory,
)
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.structure import filters, models, permissions, serializers, utils
from waldur_core.structure.managers import (
    filter_queryset_by_user_ip,
    filter_queryset_for_user,
    get_active_tokens,
    get_connected_customers,
    get_connected_projects,
    get_project_users,
)
from waldur_core.structure.utils import get_components_usage_data_from_resources
from waldur_core.users import tasks as user_tasks
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation
from waldur_mastermind.billing import models as billing_models
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
        "description",
        "name",
        "native_name",
        "registration_code",
    )
    filterset_class = filters.CustomerFilter

    def get_queryset(self):
        queryset = super().get_queryset()

        # Annotate with projects_count to avoid N+1 queries
        queryset = queryset.annotate(
            projects_count=Count("projects", filter=Q(projects__is_removed=False))
        )

        # Add users_count annotation - we'll calculate this differently due to complexity
        # For now, we'll use a simpler approach that can be optimized later
        # The serializer will try to use the annotated value if available
        queryset = queryset.extra(
            select={
                "users_count": "0"
            }  # Placeholder - will be calculated efficiently in serializer
        )

        return queryset

    def paginate_queryset(self, queryset):
        """Override to add bulk optimizations after pagination."""
        page = super().paginate_queryset(queryset)
        if page is not None:
            # Only optimize expensive fields if they're actually requested
            requested_fields = self.request.query_params.getlist("field")
            if not requested_fields or "users_count" in requested_fields:
                self._optimize_users_count(page)
            if not requested_fields or "billing_price_estimate" in requested_fields:
                self._optimize_billing_estimates(page)
        return page

    def _optimize_users_count(self, customers):
        """Bulk calculate users_count for a list of customers to avoid N+1 queries."""
        if not customers:
            return

        # Skip user count optimization for basic requests to reduce query load
        # Only calculate if users_count field is explicitly requested
        if hasattr(self.request, "query_params"):
            fields = self.request.query_params.getlist("field")
        else:
            fields = getattr(self.request, "GET", {}).getlist("field")

        if "users_count" not in fields:
            # Set default value and skip expensive calculation
            for customer in customers:
                customer._cached_users_count = 0
            return

        # Calculate users count for all customers in a single efficient operation
        # Get all users with roles in these customers or their projects
        customer_ct = ContentType.objects.get_for_model(models.Customer)
        project_ct = ContentType.objects.get_for_model(models.Project)

        # Use exact user counting that handles overlap between customer and project roles

        # For each customer, count unique users with roles at customer OR project level
        for customer in customers:
            # Get project IDs for this customer
            project_ids = list(
                models.Project.available_objects.filter(
                    customer_id=customer.id
                ).values_list("id", flat=True)
            )

            # Count unique users with roles either at customer level or project level
            user_roles_query = Q(
                content_type=customer_ct, object_id=customer.id, is_active=True
            )

            if project_ids:
                user_roles_query |= Q(
                    content_type=project_ct, object_id__in=project_ids, is_active=True
                )

            # Count distinct users - this ensures no double counting
            unique_user_count = (
                UserRole.objects.filter(user_roles_query)
                .values("user_id")
                .distinct()
                .count()
            )

            customer._cached_users_count = unique_user_count

    def _optimize_billing_estimates(self, customers):
        """Bulk load price estimates for customers to avoid N+1 queries."""
        if not customers:
            return

        # Only optimize if billing_price_estimate field is requested
        if hasattr(self.request, "query_params"):
            fields = self.request.query_params.getlist("field")
        else:
            fields = self.request.GET.getlist("field")
        if "billing_price_estimate" not in fields:
            return

        customer_ids = [c.id for c in customers]
        customer_ct = ContentType.objects.get_for_model(models.Customer)

        # Bulk load all price estimates for these customers
        price_estimates = billing_models.PriceEstimate.objects.filter(
            content_type=customer_ct, object_id__in=customer_ids
        ).select_related("content_type")

        # Create a mapping of customer_id -> price_estimate
        estimates_by_customer = {}
        for estimate in price_estimates:
            estimates_by_customer[estimate.object_id] = estimate

        # Cache the estimates on the request for use in serializers
        if not hasattr(self.request, "_price_estimates_cache"):
            self.request._price_estimates_cache = {}

        # Add estimates to cache, including None for customers without estimates
        for customer in customers:
            self.request._price_estimates_cache[customer.id] = (
                estimates_by_customer.get(customer.id)
            )

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


@extend_schema(
    parameters=[CUSTOMER_UUID_PARAMETER],
    description="A list of users connected to the customer.",
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


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "include_terminated",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Include soft-deleted (terminated) projects. Only available to staff and support users, or users with organizational roles who can see their terminated projects.",
            )
        ]
    )
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
            return models.Project.objects.all().order_by("name")
        elif include_terminated and user and user.is_authenticated:
            # Regular users can see terminated projects they would normally have access to
            # Get the base queryset using normal filtering but include terminated projects
            base_queryset = models.Project.objects.all().order_by("name")
            # Apply the same filters that would normally be applied (GenericRoleFilter logic)
            filtered_queryset = filter_queryset_for_user(base_queryset, user)
            filtered_queryset = filter_queryset_by_user_ip(
                filtered_queryset, self.request
            )
            return filtered_queryset

        return models.Project.available_objects.all().order_by("name")

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

    def perform_destroy(self, instance):
        """Override to pass the terminating user to the soft delete method."""
        instance._soft_delete(terminated_by=self.request.user)

    @extend_schema(
        request=serializers.MoveProjectSerializer,
        responses={
            200: serializers.ProjectSerializer,
            400: {
                "type": "object",
                "properties": {
                    "non_field_errors": {"type": "array", "items": {"type": "string"}}
                },
                "description": "Validation error when trying to move a terminated project",
                "example": {"non_field_errors": ["Cannot move terminated projects."]},
            },
        },
    )
    @action(detail=True, methods=["post"])
    def move_project(self, request, uuid=None):
        project = self.get_object()

        # Restrict moving terminated projects
        if project.is_removed:
            raise ValidationError(
                {"non_field_errors": ["Cannot move terminated projects."]}
            )

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
        request=serializers.ProjectRecoverySerializer,
        responses=serializers.ProjectSerializer,
        description="Recover a soft-deleted project with team member restoration",
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
        response_data = serializer.data
        response_data["ip_address"] = get_ip_address(request)

        return Response(
            response_data,
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


class ExternalLinkViewSet(viewsets.ModelViewSet):
    queryset = models.ExternalLink.objects.all()
    serializer_class = serializers.ExternalLinkSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, rf_filters.OrderingFilter)
    filterset_class = filters.ExternalLinkFilter
    permission_classes = (core_permissions.IsAdminOrReadOnly,)
    ordering_fields = ("name", "url")


@extend_schema(parameters=[CUSTOMER_UUID_PARAMETER])
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


@extend_schema(parameters=[CUSTOMER_UUID_PARAMETER])
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
        """Get detailed project compliance information with database-level pagination."""
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


@extend_schema(parameters=[CUSTOMER_UUID_PARAMETER])
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
        """List project checklist answer data with database-level pagination."""
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


@extend_schema(parameters=[CUSTOMER_UUID_PARAMETER])
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
        """List questions with project answers, paginated by question at database level."""
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
