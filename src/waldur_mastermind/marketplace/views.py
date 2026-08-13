import base64
import copy
import datetime
import logging
import textwrap
import traceback
from typing import cast
from urllib.parse import urlparse

import httpx
import reversion
import tomli_w
import yaml
from constance import config
from cryptography.fernet import InvalidToken
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.db.models import (
    Avg,
    CharField,
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Func,
    OuterRef,
    PositiveSmallIntegerField,
    Prefetch,
    Q,
    Subquery,
)
from django.db.models.aggregates import Sum
from django.db.models.fields import FloatField, IntegerField
from django.db.models.functions import Coalesce, Lower, Trim, TruncDate, TruncMonth
from django.db.models.functions.math import Ceil
from django.http import HttpResponse
from django.http.response import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import exceptions as rf_exceptions
from rest_framework import (
    generics,
    mixins,
    response,
    status,
    views,
)
from rest_framework import permissions as rf_permissions
from rest_framework import (
    serializers as drf_serializers,
)
from rest_framework import viewsets as rf_viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.generics import ListAPIView
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.mixins import ReviewerChecklistMixin, UserChecklistMixin
from waldur_core.core import encryption
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.core.models import User
from waldur_core.core.pagination import LinkHeaderPagination
from waldur_core.core.renderers import PlainTextRenderer
from waldur_core.core.serializers import (
    EmptySerializer,
    RestrictedSerializerMixin,
    ReviewCommentSerializer,
    StatusSerializer,
)
from waldur_core.core.utils import (
    SubqueryCount,
    is_uuid_like,
    month_start,
    order_with_nulls,
)
from waldur_core.logging import event_logger
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.media import utils as media_utils
from waldur_core.permissions import models as permission_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.filters import UserPermissionFilter
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import (
    add_user,
    get_user_ids,
    has_permission,
    permission_factory,
)
from waldur_core.permissions.views import UserRoleMixin
from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import utils as structure_utils
from waldur_core.structure import views as structure_views
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.executors import ServiceSettingsPullExecutor
from waldur_core.structure.managers import (
    filter_queryset_by_user_ip,
    filter_queryset_for_user,
    get_connected_customers,
    get_connected_customers_by_permission,
    get_connected_projects,
    get_connected_projects_by_permission,
    get_organization_groups,
    get_visible_users,
)
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.signals import resource_imported
from waldur_core.structure.utils import get_identity_provider_name
from waldur_core.structure.utils_data_access import bulk_log_user_data_access
from waldur_core.users.affiliations import parse_affiliation
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import Invitation
from waldur_core.users.utils import get_invitation_duplicates
from waldur_mastermind.analytics import models as analytics_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import serializers as invoice_serializers
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.catalog_loaders import (
    detect_eessi_version,
    detect_spack_version,
)
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    BillingTypes,
    CourseAccountState,
    ImpactLevel,
    LimitPeriods,
    MaintenanceState,
    MaintenanceType,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
    ServiceAccountState,
)
from waldur_mastermind.marketplace.managers import (
    ResourceQuerySet,
    filter_offering_permissions,
    get_connected_offerings,
    get_connected_offerings_by_permission,
    get_user_resource_project_ids,
)
from waldur_mastermind.marketplace.utils import (
    get_components_usage_data_per_offering,
    get_model_serializer,
    get_offering_usage_by_project,
    get_offering_usage_timeseries,
    validate_attributes,
)
from waldur_mastermind.policy.models import SlurmPeriodicUsagePolicy
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.proposal import managers as proposal_managers
from waldur_mastermind.support import models as support_models
from waldur_openstack import models as openstack_models
from waldur_pid import models as pid_models

from . import (
    filters,
    log,
    models,
    order_approval,
    permissions,
    plugins,
    posix_ids,
    serializers,
    tasks,
    utils,
)
from .demo_presets.manifest import DemoPresetManager
from .handlers import get_plan_scopes

logger = logging.getLogger(__name__)


def get_allowed_offering_users_for_user(
    request_user, include_consent_filtering=False, action=None
):
    """
    Get the queryset of OfferingUsers that the current user is allowed to see.
    This implements the shared permission logic used by both OfferingUsersViewSet
    and OfferingUserChecklistCompletionsViewSet.

    Args:
        request_user: The current user making the request
        include_consent_filtering: Whether to apply consent filtering
        action: The action being performed (for consent filtering)
    """
    queryset = models.OfferingUser.objects.all()

    if request_user.is_staff or request_user.is_support:
        # Staff and support users see all OfferingUsers without any filtering
        return queryset

    visible_users = get_visible_users(request_user)
    managed_customers = get_connected_customers(request_user)
    managed_projects = get_connected_projects(request_user)
    nested_customers = structure_models.Project.objects.filter(
        id__in=managed_projects
    ).values_list("customer_id", flat=True)
    visible_customers = managed_customers.union(nested_customers)
    visible_organization_groups = structure_models.Customer.objects.filter(
        id__in=visible_customers
    ).values_list("organization_groups__id", flat=True)
    # Resolve M2M-via-organization_groups to a flat set of offering ids so that
    # the outer query does not need a LEFT JOIN on
    # marketplace_offering_organization_groups (which forces SELECT DISTINCT
    # over the wide OfferingUser/Offering/User column set).
    offerings_via_organization_groups = models.Offering.objects.filter(
        organization_groups__in=visible_organization_groups
    ).values("id")

    # Build base visibility conditions
    managed_offerings = get_connected_offerings(request_user)

    base_visibility_q = (
        Q(user=request_user)
        | (
            # service provider can see all records related to managed offerings
            # but only for users with active consent
            (Q(offering__customer__in=managed_customers) | Q(user__in=visible_users))
            & (
                # only offerings managed by customer where the current user has a role
                Q(offering__customer__id__in=visible_customers)
                |
                # only offerings from organization_groups including the current user's customers
                Q(offering__id__in=offerings_via_organization_groups)
            )
        )
        | (
            # offering managers can see all offering users on offerings they manage
            Q(offering__id__in=managed_offerings)
        )
    )

    # Identity managers can see OfferingUsers whose linked user's active_isds
    # overlap with the manager's managed_isds (mirrors event delivery logic)
    if request_user.is_identity_manager and request_user.managed_isds:
        identity_manager_q = Q()
        for isd in request_user.managed_isds:
            identity_manager_q |= Q(user__active_isds__contains=[isd])
        base_visibility_q = base_visibility_q | identity_manager_q

    queryset = queryset.filter(
        # Exclude offerings with disabled OfferingUsers feature
        Q(offering__plugin_options__service_provider_can_create_offering_user=True)
        & base_visibility_q
    ).distinct()

    if (
        include_consent_filtering
        and config.ENFORCE_USER_CONSENT_FOR_OFFERINGS
        and action in ["list", "retrieve"]
    ):
        # Show if offering has no terms of service or user has active consent
        queryset = queryset.filter(
            Q(user=request_user)
            | ~Q(offering__terms_of_service_configs__is_active=True)
            | Q(
                user__offering_consents__offering=F("offering"),
                user__offering_consents__revocation_date__isnull=True,
            )
        )

    if config.ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS and action in [
        "list",
        "retrieve",
    ]:
        incomplete_q = utils.build_incomplete_profile_q()
        # Exclude incomplete profiles, but NOT for user's own records
        queryset = queryset.exclude(~Q(user=request_user) & incomplete_q)

    return queryset


def _stamp_glauth_integration_status(offering, request):
    """Mark this offering's GLAUTH_SYNC integration as active and timestamped."""
    integration_status, _ = models.IntegrationStatus.objects.get_or_create(
        offering=offering,
        agent_type=models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
    )
    integration_status.set_last_request_timestamp()
    integration_status.service_name = request.headers.get("User-Agent", "")
    integration_status.set_backend_active()
    integration_status.save()


def _strip_internal(tree: dict) -> dict:
    """Drop the private keys that `build_glauth_tree` carries for the TOML emitter."""
    return {k: v for k, v in tree.items() if not k.startswith("_")}


def _render_glauth_toml(offering, *, resource_filter=None) -> str:
    """Render the glauth TOML config for an offering or single resource.

    Builds the structured tree, then derives the TOML output from it so
    the role-aware ``[[groups]]`` blocks and per-user ``otherGroups``
    additions stay aligned with what `glauth_tree` returns.
    """
    tree = utils.build_glauth_tree(offering, resource_filter=resource_filter)

    user_data = utils.generate_glauth_records_for_offering_users(
        offering,
        tree["_offering_users"],
        extra_user_gids=tree["_user_role_gids"],
    )

    robot_qs = models.RobotAccount.objects.filter(resource__offering=offering)
    if resource_filter is not None:
        robot_qs = robot_qs.filter(resource=resource_filter)
    robot_data = utils.generate_glauth_records_for_robot_accounts(offering, robot_qs)

    groups = user_data["groups"] + robot_data["groups"]
    for group in tree["groups"]:
        # Per-user personal groups are already emitted by
        # generate_glauth_records_for_offering_users above; the tree carries them
        # only so the JSON view can show them. Skip here to avoid double-emitting.
        if group.get("kind") == "personal":
            continue
        groups.append(
            {
                "name": group["name"],
                "gidnumber": int(group["gid"]),
            }
        )

    # Render groups as ``[[groups]]`` array-of-tables blocks rather than letting
    # tomli_w emit a top-level ``groups = [ {..}, .. ]`` inline array. GLAuth's
    # config backend (and the glauth image, which *concatenates* this output onto
    # a base config that already declares its service-account ``[[groups]]``)
    # requires array-of-tables so the groups accumulate into one list. A bare
    # ``groups = [...]`` key collides with the pre-existing ``[[groups]]`` and is
    # dropped, so none of the Waldur project/role groups reach LDAP. Emitting
    # ``[[groups]]`` parses to the identical structure while merging cleanly.
    group_blocks = "".join(
        "[[groups]]\n"
        + tomli_w.dumps({"name": group["name"], "gidnumber": int(group["gidnumber"])})
        for group in groups
    )
    users_toml = tomli_w.dumps({"users": user_data["users"] + robot_data["users"]})
    return group_blocks + users_toml


class BaseMarketplaceView(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    update_permissions = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_owner
    ]


class PublicViewsetMixin:
    """Mixin to allow anonymous access to offerings when configured."""

    def get_permissions(self):
        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self, "swagger_fake_view", False):
            return super().get_permissions()

        if config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS and self.action in [
            "list",
            "retrieve",
        ]:
            return [rf_permissions.AllowAny()]
        else:
            return super().get_permissions()


class ConnectedOfferingDetailsMixin:
    """Mixin to provide offering details action for connected resources."""

    @extend_schema(
        summary="Get offering details",
        description="Returns details of the offering connected to the requested object.",
        responses=serializers.PublicOfferingDetailsSerializer,
        filters=False,
    )
    @action(detail=True, methods=["get"])
    def offering(self, request, *args, **kwargs):
        requested_object = self.get_object()
        if hasattr(requested_object, "offering"):
            offering = requested_object.offering
            serializer = serializers.PublicOfferingDetailsSerializer(
                instance=offering, context=self.get_serializer_context()
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(status.HTTP_204_NO_CONTENT)


class ConnectedResourceDetailsMixin:
    """Mixin to provide resource details action for connected resources."""

    @extend_schema(
        summary="Get resource details",
        description="Returns details of the resource connected to the requested object.",
        responses=serializers.ResourceSerializer,
        filters=False,
    )
    @action(detail=True, methods=["get"])
    def resource(self, request, *args, **kwargs):
        requested_object = self.get_object()
        if hasattr(requested_object, "resource"):
            resource = requested_object.resource
            serializer = serializers.ResourceSerializer(
                instance=resource, context=self.get_serializer_context()
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(
        summary="List service providers",
        description="Returns a paginated list of service providers.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a service provider",
        description="Returns details of a specific service provider.",
    ),
    create=extend_schema(
        summary="Create a service provider",
        description="Creates a new service provider profile for a customer.",
    ),
    update=extend_schema(
        summary="Update a service provider",
        description="Updates an existing service provider profile.",
    ),
    partial_update=extend_schema(
        summary="Partially update a service provider",
        description="Partially updates an existing service provider profile.",
    ),
    destroy=extend_schema(
        summary="Delete a service provider",
        description="Deletes a service provider profile. Only possible if there are no active offerings.",
    ),
)
class ServiceProviderViewSet(UserRoleMixin, PublicViewsetMixin, BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all().order_by("customer__name")
    serializer_class = serializers.ServiceProviderSerializer
    filterset_class = filters.ServiceProviderFilter

    @extend_schema(
        operation_id="service_provider_api_secret_code_retrieve",
        summary="Get service provider API secret code",
        description="Returns the API secret code for a service provider. Requires service provider owner permission.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderApiSecretCodeSerializer
        },
        filters=False,
        methods=["GET"],
    )
    @extend_schema(
        operation_id="service_provider_api_secret_code_generate",
        summary="Generate new service provider API secret code",
        description="Generates a new API secret code for a service provider, invalidating the old one. Requires service provider owner permission.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderApiSecretCodeSerializer
        },
        methods=["POST"],
    )
    @action(detail=True, methods=["GET", "POST"])
    def api_secret_code(self, request, uuid=None):
        service_provider: models.ServiceProvider = self.get_object()
        if request.method == "GET":
            if not has_permission(
                request,
                PermissionEnum.GET_SERVICE_PROVIDER_API_SECRET_CODE,
                service_provider.customer,
            ):
                raise PermissionDenied()
            return Response(
                {"api_secret_code": service_provider.api_secret_code},
                status=status.HTTP_200_OK,
            )
        else:
            if not has_permission(
                request,
                PermissionEnum.GENERATE_SERVICE_PROVIDER_API_SECRET_CODE,
                service_provider.customer,
            ):
                raise PermissionDenied()
            service_provider.generate_api_secret_code()
            service_provider.save()
            return Response(
                {
                    "detail": _("Api secret code updated."),
                    "api_secret_code": service_provider.api_secret_code,
                },
                status=status.HTTP_200_OK,
            )

    def check_related_resources(request, view, obj=None):
        if obj and obj.has_active_offerings:
            raise rf_exceptions.ValidationError(
                _("Service provider has active offerings. Please archive them first.")
            )

    destroy_permissions = [structure_permissions.is_owner, check_related_resources]

    set_offerings_username_permissions = [
        permission_factory(
            PermissionEnum.SET_SERVICE_PROVIDER_OFFERINGS_USERNAME,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="Set offering username for a user",
        description="Sets or updates the offering-specific username for a user across all offerings managed by the service provider that the user has access to.",
        request=serializers.SetOfferingsUsernameSerializer,
        responses={status.HTTP_201_CREATED: None},
    )
    @action(detail=True, methods=["POST"])
    def set_offerings_username(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_uuid = serializer.validated_data["user_uuid"]
        username = serializer.validated_data["username"]

        try:
            user = core_models.User.objects.get(uuid=user_uuid)
        except core_models.User.DoesNotExist:
            validation_message = f"A user with the uuid [{user_uuid}] is not found."
            raise rf_exceptions.ValidationError(_(validation_message))

        user_projects_ids = get_connected_projects(user)
        offering_ids = (
            models.Resource.objects.exclude(state=ResourceStates.TERMINATED)
            .filter(
                project_id__in=user_projects_ids,
                offering__customer=self.get_object().customer,
            )
            .values_list("offering_id", flat=True)
        )

        for offering_id in offering_ids:
            offering_user, created = models.OfferingUser.objects.get_or_create(
                user=user, offering_id=offering_id
            )
            # Update username - only set if non-empty to avoid unwanted state transitions
            if username:
                offering_user.username = username
                offering_user.save()  # This triggers the FSM transition via model save method
            else:
                logger.info(
                    "ServiceProvider set_offerings_username called with empty username: service_provider_uuid=%s user_uuid=%s offering_id=%s actor_uuid=%s",
                    uuid,
                    user_uuid.hex,
                    offering_id,
                    getattr(request.user, "uuid", None) and request.user.uuid.hex,
                )

        return Response(
            {
                "detail": _("Offering users have been set."),
            },
            status=status.HTTP_201_CREATED,
        )

    set_offerings_username_serializer_class = serializers.SetOfferingsUsernameSerializer

    stat_permissions = [
        permission_factory(
            PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="Get service provider statistics",
        description="Returns various statistics for the service provider, such as number of active campaigns, customers, and resources.",
        responses=serializers.ServiceProviderStatisticsSerializer,
        filters=False,
    )
    @action(detail=True, methods=["GET"])
    def stat(self, request, uuid=None):
        to_day = timezone.datetime.today().date()
        service_provider: models.ServiceProvider = self.get_object()

        active_campaigns = promotions_models.Campaign.objects.filter(
            service_provider=service_provider,
            state=promotions_models.Campaign.States.ACTIVE,
            start_date__lte=to_day,
            end_date__gte=to_day,
        ).count()

        current_customers = (
            models.Resource.objects.filter(
                offering__customer=service_provider.customer,
            )
            .exclude(state=ResourceStates.TERMINATED)
            .order_by()
            .values_list("project__customer", flat=True)
            .distinct()
            .count()
        )

        active_resources = models.Resource.objects.filter(
            offering__customer=service_provider.customer,
        ).exclude(state=ResourceStates.TERMINATED)

        active_and_paused_offerings = models.Offering.objects.filter(
            customer=service_provider.customer,
            billable=True,
            shared=True,
            state__in=(
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
                OfferingStates.UNAVAILABLE,
            ),
        ).count()

        content_type = ContentType.objects.get_for_model(support_models.Issue)
        unresolved_tickets = len(
            [
                i
                for i in support_models.Issue.objects.filter(
                    resource_content_type=content_type,
                    resource_object_id__in=(
                        active_resources.values_list("id", flat=True)
                    ),
                )
                if not i.resolved
            ]
        )

        pending_orders = models.Order.objects.filter(
            offering__customer=service_provider.customer,
            state=OrderStates.PENDING_PROVIDER,
        ).count()

        erred_resources = models.Resource.objects.filter(
            offering__customer=service_provider.customer,
            state=ResourceStates.ERRED,
        ).count()

        return Response(
            dict(
                active_campaigns=active_campaigns,
                current_customers=current_customers,
                customers_number_change=utils.count_customers_number_change(
                    service_provider
                ),
                active_resources=active_resources.count(),
                resources_number_change=utils.count_resources_number_change(
                    service_provider
                ),
                active_and_paused_offerings=active_and_paused_offerings,
                unresolved_tickets=unresolved_tickets,
                pending_orders=pending_orders,
                erred_resources=erred_resources,
            ),
            status=status.HTTP_200_OK,
        )

    revenue_permissions = [
        permission_factory(
            PermissionEnum.GET_SERVICE_PROVIDER_REVENUE,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="Get service provider revenue",
        description="Returns monthly revenue data for the last year for the service provider.",
        responses=serializers.ServiceProviderRevenues(many=True),
        filters=False,
    )
    @action(detail=True, methods=["GET"])
    def revenue(self, request, uuid=None):
        start = month_start(timezone.datetime.today()) - relativedelta(years=1)
        service_provider: models.ServiceProvider = self.get_object()
        customer = service_provider.customer

        data = (
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start,
                resource__offering__customer=customer,
            )
            .values("invoice__year", "invoice__month")
            .annotate(total=Sum(F("unit_price") * F("quantity")))
            .order_by("invoice__year", "invoice__month")
        )

        return Response(
            serializers.ServiceProviderRevenues(data, many=True).data,
            status=status.HTTP_200_OK,
        )

    robot_account_customers_permissions = [
        permission_factory(
            PermissionEnum.GET_SERVICE_PROVIDER_ROBOT_ACCOUNT_CUSTOMERS,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="List customers with robot accounts",
        description="Returns a paginated list of customers who have robot accounts for resources managed by this service provider.",
        responses=serializers.NameUUIDSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="customer_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by customer name (case-insensitive partial match).",
            ),
        ],
        filters=False,
    )
    @action(detail=True, methods=["GET"])
    def robot_account_customers(self, request, uuid=None):
        service_provider: models.ServiceProvider = self.get_object()
        valid_states = [
            RobotAccountStates.OK,
            RobotAccountStates.REQUESTED_DELETION,
        ]
        qs = models.RobotAccount.objects.filter(
            resource__offering__customer=service_provider.customer,
            state__in=valid_states,
        )
        customer_name = request.query_params.get("customer_name")
        if customer_name:
            qs = qs.filter(resource__project__customer__name__icontains=customer_name)
        customer_ids = qs.values_list("resource__project__customer_id").distinct()
        customers = structure_models.Customer.objects.filter(
            id__in=customer_ids
        ).order_by("name")
        page = self.paginate_queryset(customers)
        data = serializers.NameUUIDSerializer(page, many=True).data
        return self.get_paginated_response(data)

    robot_account_projects_permissions = [
        permission_factory(
            PermissionEnum.GET_SERVICE_PROVIDER_ROBOT_ACCOUNT_PROJECTS,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="List projects with robot accounts",
        description="Returns a paginated list of projects which have robot accounts for resources managed by this service provider.",
        responses=serializers.NameUUIDSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="project_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by project name (case-insensitive partial match).",
            ),
        ],
        filters=False,
    )
    @action(detail=True, methods=["GET"])
    def robot_account_projects(self, request, uuid=None):
        service_provider: models.ServiceProvider = self.get_object()
        valid_states = [
            RobotAccountStates.OK,
            RobotAccountStates.REQUESTED_DELETION,
        ]
        qs = models.RobotAccount.objects.filter(
            resource__offering__customer=service_provider.customer,
            state__in=valid_states,
        )
        project_name = request.query_params.get("project_name")
        if project_name:
            qs = qs.filter(resource__offering__project__name__icontains=project_name)
        project_ids = qs.values_list("resource__project_id").distinct()
        projects = structure_models.Project.objects.filter(id__in=project_ids).order_by(
            "name"
        )
        page = self.paginate_queryset(projects)
        data = serializers.NameUUIDSerializer(page, many=True).data
        return self.get_paginated_response(data)

    generate_site_agent_config_permissions = [
        permission_factory(
            PermissionEnum.GET_SERVICE_PROVIDER_API_SECRET_CODE,
            ["customer"],
        )
    ]

    @extend_schema(
        summary="Generate site agent configuration",
        description=(
            "Generates a YAML configuration file for waldur-site-agent based on selected SLURM offerings. "
            "The configuration includes offering details, components, backend settings, and optionally "
            "SLURM periodic usage policy settings. Secrets are shown as placeholders that need to be filled in."
        ),
        request=serializers.SiteAgentConfigGenerationSerializer,
        responses={
            status.HTTP_200_OK: OpenApiTypes.BINARY,
        },
    )
    @action(detail=True, methods=["POST"])
    def generate_site_agent_config(self, request, uuid=None):
        """Generate site agent configuration YAML for SLURM offerings."""
        service_provider: models.ServiceProvider = self.get_object()

        serializer = serializers.SiteAgentConfigGenerationSerializer(
            data=request.data,
            context={"request": request, "service_provider": service_provider},
        )
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        offerings = serializer.context.get("validated_offerings", [])
        include_policy = validated_data.get("include_policy_settings", True)
        waldur_api_url = validated_data.get(
            "waldur_api_url"
        ) or request.build_absolute_uri("/api/")
        tz = validated_data.get("timezone", "UTC")

        config = self._build_site_agent_config(
            offerings=offerings,
            waldur_api_url=waldur_api_url,
            timezone_str=tz,
            include_policy_settings=include_policy,
        )

        # Generate YAML with header comments
        yaml_content = self._generate_yaml_with_header(config)

        return HttpResponse(yaml_content, content_type="text/plain; charset=utf-8")

    def _build_site_agent_config(
        self, offerings, waldur_api_url, timezone_str, include_policy_settings
    ):
        """Build site agent configuration dictionary."""
        config = {
            "sentry_dsn": "",
            "timezone": timezone_str,
            "offerings": [],
        }

        for offering in offerings:
            offering_config = self._build_offering_config(
                offering, waldur_api_url, include_policy_settings
            )
            config["offerings"].append(offering_config)

        return config

    def _build_offering_config(self, offering, waldur_api_url, include_policy_settings):
        """Build configuration for a single offering."""
        offering_config = {
            "name": offering.name,
            "waldur_api_url": waldur_api_url,
            "waldur_api_token": "<YOUR_API_TOKEN_HERE>",
            "waldur_offering_uuid": str(offering.uuid),
            "order_processing_backend": "slurm",
            "membership_sync_backend": "slurm",
            "reporting_backend": "slurm",
            "backend_settings": self._build_backend_settings(offering),
            "backend_components": self._build_backend_components(offering),
        }

        if include_policy_settings:
            policy_settings = self._build_policy_settings(offering)
            if policy_settings:
                offering_config["policy_settings"] = policy_settings

        return offering_config

    def _build_backend_settings(self, offering):
        """Build backend_settings from offering plugin_options."""
        plugin_options = offering.plugin_options or {}

        return {
            "default_account": plugin_options.get("default_account", "root"),
            "customer_prefix": plugin_options.get("customer_prefix", ""),
            "project_prefix": plugin_options.get("project_prefix", ""),
            "allocation_prefix": plugin_options.get("allocation_prefix", ""),
            "qos_downscaled": plugin_options.get("qos_downscaled", "limited"),
            "qos_paused": plugin_options.get("qos_paused", "paused"),
            "qos_default": plugin_options.get("qos_default", "normal"),
            "hostname": plugin_options.get("hostname", "<YOUR_SLURM_HOST>"),
            "enable_user_homedir_account_creation": plugin_options.get(
                "enable_user_homedir_account_creation", False
            ),
        }

    def _build_backend_components(self, offering):
        """Convert offering components to backend_components format."""
        components = {}
        for component in offering.components.all():
            components[component.type] = {
                "measured_unit": component.measured_unit or "",
                "unit_factor": component.unit_factor or 1,
                "accounting_type": "usage"
                if component.billing_type == BillingTypes.USAGE
                else "limit",
                "label": component.name,
            }
        return components

    def _build_policy_settings(self, offering):
        """Build policy settings from SlurmPeriodicUsagePolicy if exists."""
        try:
            policy = SlurmPeriodicUsagePolicy.objects.get(scope=offering)
            return {
                "limit_type": policy.limit_type,
                "tres_billing_enabled": policy.tres_billing_enabled,
                "tres_billing_weights": policy.tres_billing_weights or {},
                "carryover_factor": policy.carryover_factor,
                "grace_ratio": float(policy.grace_ratio),
                "carryover_enabled": policy.carryover_enabled,
                "raw_usage_reset": policy.raw_usage_reset,
                "qos_strategy": policy.qos_strategy,
            }
        except SlurmPeriodicUsagePolicy.DoesNotExist:
            return None

    def _generate_yaml_with_header(self, config):
        """Generate YAML string with instructional header comments."""
        header = (
            textwrap.dedent("""
            # Waldur Site Agent Configuration
            # Generated: {timestamp}
            #
            # IMPORTANT - SECRETS TO CONFIGURE:
            #   - waldur_api_token: Generate an API token from Waldur user settings
            #     (User Profile -> Credentials -> API Token)
            #   - hostname: Your SLURM cluster hostname (if shown as placeholder)
            #
            # Documentation: https://docs.waldur.com/admin-guide/providers/remote-offerings/
            #
            # Place this file at /etc/waldur/waldur-site-agent-config.yaml
            # and start the site agent service.

        """)
            .format(timestamp=timezone.now().isoformat())
            .lstrip()
        )

        yaml_content = yaml.dump(
            config, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        return header + yaml_content


SERVICE_PROVIDER_UUID = OpenApiParameter(
    name="service_provider_uuid",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
)


@extend_schema_view(
    list=extend_schema(
        summary="List customers of a service provider",
        description="Returns a paginated list of customers who have consumed resources from the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderCustomersViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.MarketplaceProviderCustomerSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.CustomerFilter
    queryset = structure_models.Customer.objects.all()

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        customer_ids = utils.get_service_provider_customer_ids(
            self.get_service_provider()
        )
        return self.queryset.filter(id__in=customer_ids)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    list=extend_schema(
        summary="List customer projects of a service provider",
        description="Returns a paginated list of projects belonging to a specific customer that have consumed resources from the specified service provider.",
        parameters=[
            SERVICE_PROVIDER_UUID,
            OpenApiParameter(
                name="project_customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the customer to filter projects by.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
        ],
    )
)
class ServiceProviderCustomerProjectsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.MarketplaceProviderCustomerProjectSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.ProjectFilter
    queryset = structure_models.Project.available_objects.all()

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMER_PROJECTS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        customer_uuid = self.request.query_params.get("project_customer_uuid")
        if not customer_uuid or not is_uuid_like(customer_uuid):
            return self.queryset.none()
        project_ids = (
            utils.get_service_provider_resources(self.get_service_provider())
            .filter(project__customer__uuid=customer_uuid)
            .values_list("project_id", flat=True)
        )
        return self.queryset.filter(id__in=project_ids)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    list=extend_schema(
        summary="List projects of a service provider",
        description="Returns a paginated list of all projects that have consumed resources from the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderProjectsViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = structure_serializers.ProjectSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.ProjectFilter
    queryset = structure_models.Project.available_objects.all()

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_PROJECTS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        project_ids = utils.get_service_provider_project_ids(
            self.get_service_provider()
        )
        return self.queryset.filter(id__in=project_ids)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    list=extend_schema(
        summary="List project permissions of a service provider",
        description="Returns a paginated list of project permissions for all projects that have consumed resources from the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderProjectPermissionsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = structure_serializers.ProjectPermissionLogSerializer
    queryset = UserRole.objects.all().select_related("user", "role", "created_by")
    filter_backends = (DjangoFilterBackend,)
    filterset_class = UserPermissionFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_PROJECT_PERMISSIONS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        project_ids = utils.get_service_provider_project_ids(
            self.get_service_provider()
        )
        content_type = ContentType.objects.get_for_model(structure_models.Project)
        return self.queryset.filter(
            content_type=content_type,
            object_id__in=project_ids,
            is_active=True,
            user__is_active=True,
        ).prefetch_related(
            GenericPrefetch(
                "scope",
                [structure_models.Project.available_objects.select_related("customer")],
            ),
        )


@extend_schema_view(
    list=extend_schema(
        summary="List SSH keys of a service provider",
        description="Returns a paginated list of SSH public keys for all users who have consumed resources from the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderKeysViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = structure_serializers.SshKeySerializer
    queryset = core_models.SshPublicKey.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.SshKeyFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_KEYS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        user_ids = utils.get_service_provider_user_ids(
            self.request.user, self.get_service_provider()
        )
        return self.queryset.filter(user_id__in=user_ids).select_related("user")


@extend_schema_view(
    list=extend_schema(
        summary="List users of a service provider",
        description="Returns a paginated list of all users who have consumed resources from the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderUsersViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = serializers.MarketplaceServiceProviderUserSerializer
    queryset = core_models.User.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.UserFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_USERS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        service_provider = self.get_service_provider()
        user_ids = utils.get_service_provider_user_ids(
            self.request.user, service_provider
        )
        queryset = self.queryset.filter(id__in=user_ids)
        if config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
            # Only users with active consent for ToS-required offerings or use offerings that don't require ToS
            queryset = queryset.filter(
                Q(
                    offering_consents__offering__customer=service_provider.customer,
                    offering_consents__offering__plugin_options__service_provider_can_create_offering_user=True,
                    offering_consents__offering__terms_of_service_configs__is_active=True,
                    offering_consents__revocation_date__isnull=True,
                )
                # Users who have resource access to resource from offering that doesn't require ToS
                | Q(
                    id__in=models.OfferingUser.objects.filter(
                        offering__customer=service_provider.customer,
                        offering__plugin_options__service_provider_can_create_offering_user=True,
                        offering__terms_of_service_configs__isnull=True,
                    ).values_list("user_id", flat=True)
                )
            )
        return queryset.distinct()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # During schema generation, don't query the database
        if getattr(self, "swagger_fake_view", False):
            return context
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    list=extend_schema(
        summary="List offerings of a service provider",
        description="Returns a paginated list of all billable, shared offerings provided by the specified service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderOfferingsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.ProviderOfferingSerializer
    queryset = models.Offering.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingFilter

    def get_service_provider(self):
        uuid = self.kwargs["service_provider_uuid"]
        # Try to find by service provider UUID first
        service_provider = models.ServiceProvider.objects.filter(uuid=uuid).first()
        if service_provider:
            return service_provider
        # Fallback: try to find by customer UUID (for frontend compatibility)
        return get_object_or_404(models.ServiceProvider, customer__uuid=uuid)

    def get_queryset(self):
        return self.queryset.filter(
            customer=self.get_service_provider().customer,
            billable=True,
            shared=True,
        )

    @extend_schema(
        summary="List distinct offering types for a service provider",
        parameters=[SERVICE_PROVIDER_UUID],
        responses={
            status.HTTP_200_OK: drf_serializers.ListSerializer(
                child=drf_serializers.CharField()
            )
        },
    )
    @action(detail=False, methods=["GET"])
    def types(self, request, **kwargs):
        types = sorted(self.get_queryset().values_list("type", flat=True).distinct())
        serializer = drf_serializers.ListSerializer(
            instance=types, child=drf_serializers.CharField()
        )
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(
        summary="List customers of a specific user within a service provider's scope",
        description="""Returns a paginated list of customers that a specified user has access to within the scope of a service provider.

        This includes:
        - Customers where the user has direct permissions.
        - Customers with projects where the user has project roles.
        - Customers related to the service provider's resources that the user can access.
        """,
        parameters=[
            SERVICE_PROVIDER_UUID,
            OpenApiParameter(
                name="user_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of the user to get related customers for.",
                extensions={"x-waldur-operation-id": "users_retrieve"},
            ),
        ],
    )
)
class ServiceProviderUserCustomersViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.MarketplaceProviderCustomerSerializer
    queryset = structure_models.Customer.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.CustomerFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_USER_CUSTOMERS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        service_provider = self.get_service_provider()

        user_uuid = self.request.query_params.get("user_uuid")
        if not user_uuid or not is_uuid_like(user_uuid):
            return self.queryset.none()

        try:
            user = User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            return self.queryset.none()

        resources = utils.get_service_provider_resources(service_provider)
        resource_projects = resources.values_list("project_id", flat=True)
        connected_projects = get_connected_projects(user)

        resource_customers = resources.values_list("project__customer_id", flat=True)
        connected_customers = get_connected_customers(user)

        valid_projects = resource_projects.intersection(connected_projects)
        valid_customers = resource_customers.intersection(connected_customers)

        project_customers = structure_models.Project.objects.filter(
            id__in=valid_projects
        ).values_list("customer_id", flat=True)

        return self.queryset.filter(id__in=project_customers.union(valid_customers))

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    compliance_overview=extend_schema(
        operation_id="service_provider_compliance_overview",
        summary="Get compliance overview for a service provider",
        description="Returns compliance overview statistics for all offerings managed by this service provider.",
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderComplianceOverviewSerializer(
                many=True
            )
        },
        parameters=[SERVICE_PROVIDER_UUID],
        methods=["GET"],
    ),
    offering_users=extend_schema(
        operation_id="service_provider_offering_users_compliance",
        summary="List offering users' compliance status",
        description="Returns a list of offering users with their compliance status for this service provider. Can be filtered by offering and compliance status.",
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderOfferingUserComplianceSerializer(
                many=True
            )
        },
        parameters=[
            SERVICE_PROVIDER_UUID,
            OpenApiParameter(
                name="offering_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by offering UUID.",
                required=False,
                extensions={
                    "x-waldur-operation-id": "marketplace_provider_offerings_list"
                },
            ),
            OpenApiParameter(
                name="compliance_status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by compliance status: completed, pending, no_checklist.",
                required=False,
            ),
        ],
        methods=["GET"],
    ),
    checklists_summary=extend_schema(
        operation_id="service_provider_checklists_summary",
        summary="Get summary of compliance checklists",
        description="Returns a summary of all compliance checklists used by this service provider with usage counts.",
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderChecklistSummarySerializer(
                many=True
            )
        },
        parameters=[SERVICE_PROVIDER_UUID],
        methods=["GET"],
    ),
)
# Declare the nested parent path parameter at class level so it is typed for
# every operation, including the auto-generated HEAD `count` companions (the
# per-action annotations above are restricted to methods=["GET"]).
@extend_schema(parameters=[SERVICE_PROVIDER_UUID])
class ServiceProviderComplianceViewSet(rf_viewsets.GenericViewSet):
    """
    ViewSet for service providers to manage and view compliance data.

    Provides endpoints for service providers to:
    - View compliance statistics across all their offerings
    - List offering users with compliance status
    - Monitor completion rates and identify users needing attention
    """

    # Required for OpenAPI schema generation
    queryset = models.ServiceProvider.objects.none()

    def get_service_provider(self):
        """Get service provider and check permissions."""
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    @extend_schema(
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderComplianceOverviewSerializer
        }
    )
    @action(detail=False, methods=["get"])
    def compliance_overview(self, request, service_provider_uuid=None):
        """Get compliance overview statistics for all offerings."""
        service_provider = self.get_service_provider()

        # Get ContentType for OfferingUser (cached after first call)
        content_type = ContentType.objects.get_for_model(models.OfferingUser)

        # Build base queryset with optimized prefetching
        base_queryset = (
            models.Offering.objects.filter(
                customer=service_provider.customer,
                compliance_checklist__isnull=False,  # Only include offerings with checklist
            )
            .select_related("compliance_checklist")  # Avoid N+1 for checklist names
            .prefetch_related(
                # Prefetch offering users
                Prefetch("offeringuser_set", queryset=models.OfferingUser.objects.all())
            )
            .annotate(total_users=Count("offeringuser", distinct=True))
            .order_by("name")  # Ensure consistent ordering
        )

        # Apply pagination to the QuerySet BEFORE processing
        paginated_offerings = self.paginate_queryset(base_queryset)
        if paginated_offerings is None:
            # No pagination requested, return empty response
            return self.get_paginated_response([])

        # Get completion data only for paginated offerings
        all_completion_data = {}
        offering_ids = [offering.id for offering in paginated_offerings]

        if offering_ids:
            # Bulk query for completions only for paginated offerings
            completions_qs = (
                checklist_models.ChecklistCompletion.objects.filter(
                    scope_content_type=content_type,
                    scope_object_id__in=models.OfferingUser.objects.filter(
                        offering_id__in=offering_ids
                    ).values("id"),
                )
                .select_related("checklist")
                .values("checklist_id", "scope_object_id", "is_completed")
            )

            # Group completion data by offering
            offering_user_to_offering = {}
            for offering in paginated_offerings:
                for user in offering.offeringuser_set.all():
                    offering_user_to_offering[user.id] = offering.id

            for completion in completions_qs:
                offering_id = offering_user_to_offering.get(
                    completion["scope_object_id"]
                )
                if offering_id:
                    if offering_id not in all_completion_data:
                        all_completion_data[offering_id] = {
                            "users_with_completions": set(),
                            "completed_users": set(),
                        }

                    all_completion_data[offering_id]["users_with_completions"].add(
                        completion["scope_object_id"]
                    )

                    if completion["is_completed"]:
                        all_completion_data[offering_id]["completed_users"].add(
                            completion["scope_object_id"]
                        )

        # Build response data from paginated offerings only
        overview_data = []
        for offering in paginated_offerings:
            # All offerings now have checklists due to filtering
            completion_data = all_completion_data.get(
                offering.id, {"users_with_completions": set(), "completed_users": set()}
            )

            users_with_completions = len(completion_data["users_with_completions"])
            completed_users = len(completion_data["completed_users"])
            pending_users = users_with_completions - completed_users
            compliance_rate = (
                (completed_users / offering.total_users) * 100
                if offering.total_users > 0
                else 0.0
            )

            overview_data.append(
                {
                    "offering_uuid": offering.uuid,
                    "offering_name": offering.name,
                    "checklist_name": offering.compliance_checklist.name,
                    "total_users": offering.total_users,
                    "users_with_completions": users_with_completions,
                    "completed_users": completed_users,
                    "pending_users": pending_users,
                    "compliance_rate": compliance_rate,
                }
            )

        # Serialize only the paginated data
        serializer = serializers.ServiceProviderComplianceOverviewSerializer(
            overview_data, many=True
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderOfferingUserComplianceSerializer
        }
    )
    @action(detail=False, methods=["get"])
    def offering_users(self, request, service_provider_uuid=None):
        """List offering users with their compliance status."""
        service_provider = self.get_service_provider()

        # Get all offering users for this service provider
        queryset = (
            models.OfferingUser.objects.filter(
                offering__customer=service_provider.customer
            )
            .select_related("user", "offering", "offering__compliance_checklist")
            .order_by("offering__name", "user__last_name", "user__first_name")
        )

        # Apply filters
        offering_uuid = request.query_params.get("offering_uuid")
        if offering_uuid:
            queryset = queryset.filter(offering__uuid=offering_uuid)

        compliance_status = request.query_params.get("compliance_status")
        if compliance_status:
            if compliance_status == "no_checklist":
                queryset = queryset.filter(offering__compliance_checklist__isnull=True)
            elif compliance_status == "completed":
                # Users with completed checklists
                content_type = ContentType.objects.get_for_model(models.OfferingUser)
                completed_ids = checklist_models.ChecklistCompletion.objects.filter(
                    is_completed=True,
                    scope_content_type=content_type,
                    checklist__offerings__customer=service_provider.customer,
                ).values_list("scope_object_id", flat=True)
                queryset = queryset.filter(id__in=completed_ids)
            elif compliance_status == "pending":
                # Users with incomplete or missing completions
                content_type = ContentType.objects.get_for_model(models.OfferingUser)
                completed_ids = checklist_models.ChecklistCompletion.objects.filter(
                    is_completed=True,
                    scope_content_type=content_type,
                    checklist__offerings__customer=service_provider.customer,
                ).values_list("scope_object_id", flat=True)
                queryset = queryset.exclude(id__in=completed_ids).exclude(
                    offering__compliance_checklist__isnull=True
                )

        # Paginate results
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = serializers.ServiceProviderOfferingUserComplianceSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = serializers.ServiceProviderOfferingUserComplianceSerializer(
            queryset, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @extend_schema(
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderChecklistSummarySerializer
        }
    )
    @action(detail=False, methods=["get"])
    def checklists_summary(self, request, service_provider_uuid=None):
        """Get summary of all checklists used by this service provider's offerings."""
        service_provider = self.get_service_provider()

        # Create a more efficient query that gets unique checklists with all needed data
        checklists_queryset = (
            checklist_models.Checklist.objects.filter(
                offerings__customer=service_provider.customer,
                offerings__compliance_checklist__isnull=False,
            )
            .prefetch_related("questions")
            .annotate(
                offerings_count=Count("offerings", distinct=True),
                questions_count=Count("questions", distinct=True),
            )
            .distinct()
            .order_by("-offerings_count", "name")
        )

        # Apply pagination to the QuerySet
        page = self.paginate_queryset(checklists_queryset)
        if page is not None:
            # Transform paginated checklists to expected format
            summary_data = [
                {
                    "checklist_uuid": checklist.uuid,
                    "checklist_name": checklist.name,
                    "questions_count": checklist.questions_count,
                    "offerings_count": checklist.offerings_count,
                }
                for checklist in page
            ]

            serializer = serializers.ServiceProviderChecklistSummarySerializer(
                summary_data, many=True
            )
            return self.get_paginated_response(serializer.data)

        # Fallback for no pagination (shouldn't happen with DRF pagination enabled)
        summary_data = [
            {
                "checklist_uuid": checklist.uuid,
                "checklist_name": checklist.name,
                "questions_count": checklist.questions_count,
                "offerings_count": checklist.offerings_count,
            }
            for checklist in checklists_queryset
        ]

        serializer = serializers.ServiceProviderChecklistSummarySerializer(
            summary_data, many=True
        )
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(
        summary="List project service accounts for a service provider",
        description="""Returns a paginated list of project service accounts that have access to resources managed by the provider.

        This includes:
        - Projects with active resources of the service provider.
        - Service accounts with non-blank usernames.
        """,
        parameters=[
            SERVICE_PROVIDER_UUID,
        ],
    )
)
class ServiceProviderProjectServiceAccountsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.ProjectServiceAccountSerializer
    queryset = models.ProjectServiceAccount.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectServiceAccountFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_SERVICE_ACCOUNTS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        service_provider = self.get_service_provider()

        resources = utils.get_service_provider_resources(service_provider)
        project_ids = resources.values_list("project_id", flat=True)

        return self.queryset.filter(
            project_id__in=project_ids,
        ).exclude(username=None)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }

    def filter_queryset(self, queryset):
        return super().filter_queryset(queryset)


@extend_schema_view(
    list=extend_schema(
        summary="List course project accounts for a service provider",
        description="""Returns a paginated list of course project accounts that have access to resources managed by the provider.

        This includes:
        - Projects with active resources of the service provider.
        - Course accounts with non-blank users.
        """,
        parameters=[
            SERVICE_PROVIDER_UUID,
        ],
    )
)
class ServiceProviderCourseAccountsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = serializers.CourseAccountSerializer
    queryset = models.CourseAccount.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CourseAccountFilter

    def get_service_provider(self):
        service_provider = get_object_or_404(
            models.ServiceProvider, uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_COURSE_ACCOUNTS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

    def get_queryset(self):
        service_provider = self.get_service_provider()

        resources = utils.get_service_provider_resources(service_provider)
        project_ids = resources.values_list("project_id", flat=True)

        return self.queryset.filter(
            project_id__in=project_ids,
        ).exclude(user=None)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }

    def filter_queryset(self, queryset):
        return super().filter_queryset(queryset)


@extend_schema_view(
    list=extend_schema(
        summary="List categories",
        description="Returns a paginated list of marketplace categories.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a category",
        description="Returns details of a specific marketplace category.",
    ),
    create=extend_schema(
        summary="Create a category",
        description="Creates a new marketplace category. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a category",
        description="Updates an existing marketplace category. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a category",
        description="Partially updates an existing marketplace category. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a category",
        description="Deletes a marketplace category. Requires staff permissions.",
    ),
)
class CategoryViewSet(PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.MarketplaceCategorySerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, OrderingFilter)
    filterset_class = filters.CategoryFilter
    ordering_fields = ("title", "group__title")
    ordering = ("group__title", "title")

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List category columns",
        description="Returns a paginated list of category columns used for resource table rendering.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a category column",
        description="Returns details of a specific category column.",
    ),
    create=extend_schema(
        summary="Create a category column",
        description="Creates a new category column. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a category column",
        description="Updates an existing category column. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a category column",
        description="Partially updates an existing category column. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a category column",
        description="Deletes a category column. Requires staff permissions.",
    ),
)
class CategoryColumnsViewSet(PublicViewsetMixin, core_views.ActionsViewSet):
    queryset = models.CategoryColumn.objects.all()
    serializer_class = serializers.CategoryColumnSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryColumnFilter

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List category groups",
        description="Returns a paginated list of category groups.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a category group",
        description="Returns details of a specific category group.",
    ),
    create=extend_schema(
        summary="Create a category group",
        description="Creates a new category group. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a category group",
        description="Updates an existing category group. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a category group",
        description="Partially updates an existing category group. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a category group",
        description="Deletes a category group. Requires staff permissions.",
    ),
)
class CategoryGroupViewSet(PublicViewsetMixin, core_views.ActionsViewSet):
    queryset = models.CategoryGroup.objects.all()
    serializer_class = serializers.CategoryGroupSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryGroupFilter

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


class OfferingGroupViewSet(core_views.ActionsViewSet):
    """Manage logical groups of offerings within a service provider.

    Service providers manage their own groups (read/write scoped via
    ``GenericRoleFilter`` against ``OfferingGroup.Permissions.customer_path``);
    staff have full access. Groups are used to express that several
    offerings (e.g. SLURM partitions) belong to the same backend entity.
    """

    queryset = models.OfferingGroup.objects.all()
    serializer_class = serializers.OfferingGroupSerializer
    lookup_field = "uuid"
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.OfferingGroupFilter

    # Create permission check happens in the serializer (it needs access to
    # the validated ``customer`` field). Object-bound CRUD uses
    # permission_factory with the same paths as offering CRUD so that
    # service-provider owners can manage their own groups.
    update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]


def posix_id_pool_has_no_active_identities(pool: models.PosixIdPool):
    if pool.identities.filter(released_at__isnull=True).exists():
        raise rf_exceptions.ValidationError(
            _("Pool with active identities cannot be deleted.")
        )


class PosixIdPoolViewSet(core_views.ActionsViewSet):
    """Manage POSIX UID/GID pools of a service provider.

    Each provider has one default pool; an offering may carry an override pool.
    A pool reserves a UID range and a GID range and is the sole UID/GID
    allocation mechanism for offering users, robot accounts and groups.
    """

    queryset = (
        models.PosixIdPool.objects.all()
        .order_by("id")
        .select_related("service_provider__customer", "offering__customer")
    )
    serializer_class = serializers.PosixIdPoolSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.PosixIdPoolFilter

    # Create permission checks happen in the serializer (the scope object is
    # only known after validation). GenericRoleFilter cannot span the two scope
    # paths, hence manual queryset scoping below.
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_POSIX_ID_POOL,
            ["customer", "customer.serviceprovider"],
        )
    ]
    destroy_validators = [posix_id_pool_has_no_active_identities]

    def get_queryset(self):
        # Annotate the per-namespace active-identity counts so the list can
        # render utilization without an N+1 stats query per row.
        queryset = (
            super()
            .get_queryset()
            .annotate(
                uid_used=Count(
                    "identities",
                    filter=Q(
                        identities__released_at__isnull=True,
                        identities__uid__isnull=False,
                    ),
                    distinct=True,
                ),
                gid_used=Count(
                    "identities",
                    filter=Q(
                        identities__released_at__isnull=True,
                        identities__gid__isnull=False,
                    ),
                    distinct=True,
                ),
            )
        )
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            return queryset

        customers = get_connected_customers(current_user)
        return queryset.filter(
            Q(service_provider__customer__in=customers)
            | Q(offering__customer__in=customers)
        )

    @extend_schema(
        summary="Pool utilization statistics",
        responses={200: serializers.PosixIdPoolStatsSerializer},
    )
    @action(detail=True, methods=["get"])
    def stats(self, request, uuid=None):
        pool = self.get_object()
        serializer = serializers.PosixIdPoolStatsSerializer(
            posix_ids.get_pool_stats(pool)
        )
        return Response(serializer.data)


class PosixIdentityViewSet(core_views.ReadOnlyActionsViewSet):
    """Read-only audit view of allocated POSIX identities.

    Released values are recycled automatically on the next allocation from the
    same pool and namespace; released rows are retained here as an audit trail.
    """

    queryset = (
        models.PosixIdentity.objects.all()
        .order_by("id")
        .select_related("pool", "offering", "content_type")
        .prefetch_related("consumer")
    )
    serializer_class = serializers.PosixIdentitySerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.PosixIdentityFilter

    def get_queryset(self):
        queryset = super().get_queryset()
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            return queryset

        customers = get_connected_customers(current_user)
        return queryset.filter(
            Q(pool__service_provider__customer__in=customers)
            | Q(pool__offering__customer__in=customers)
        )


class TagViewSet(PublicViewsetMixin, core_views.ActionsViewSet):
    """
    Manage offering tags.

    Staff users have full control.
    Service providers can create tags and modify/delete their own tags.
    All users (including anonymous) can list and retrieve tags.
    """

    queryset = models.Tag.objects.all()
    serializer_class = serializers.TagSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.TagFilter

    # Only service providers and staff can create tags
    create_permissions = [marketplace_permissions.is_service_provider_or_staff]

    # Only tag owner or staff can update/delete
    update_permissions = partial_update_permissions = destroy_permissions = [
        marketplace_permissions.can_manage_tag
    ]


# State transition validators and permissions will be handled by individual methods


def can_update_offering(request, view, obj: models.Offering | None = None):
    offering = obj

    if not offering:
        return

    if request.user.is_staff:
        return

    has_update_permission = any(
        has_permission(request, PermissionEnum.UPDATE_OFFERING, scope)
        for scope in (
            offering,
            offering.customer,
            offering.customer.serviceprovider,
        )
    )

    if config.ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT:
        if has_update_permission:
            return
        raise rf_exceptions.PermissionDenied()
    else:
        if has_update_permission and offering.state == OfferingStates.DRAFT:
            return
        raise rf_exceptions.PermissionDenied()


def validate_offering_update(offering):
    if offering.state == OfferingStates.ARCHIVED:
        raise rf_exceptions.ValidationError(
            _("It is not possible to update archived offering.")
        )


def validate_offering_has_plans(offering):
    if not models.offering_has_plans(offering):
        raise rf_exceptions.ValidationError(
            _("Offering does not have any billing plans.")
        )


def validate_offering_username_generation_policy(offering):
    service_provider_policy = utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
    if (
        offering.plugin_options.get("username_generation_policy")
        == service_provider_policy
    ):
        raise rf_exceptions.ValidationError(
            _(
                f"Invalid generation policy service_provider_policy {service_provider_policy}"
            )
        )


@extend_schema_view(
    list=extend_schema(
        summary="List provider offerings",
        description="Returns a paginated list of offerings for the provider.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a provider offering",
        description="Returns details of a specific provider offering.",
    ),
    create=extend_schema(
        summary="Create a provider offering",
        description="Creates a new provider offering.",
        request=serializers.OfferingCreateSerializer,
        responses={201: serializers.ProviderOfferingDetailsSerializer},
    ),
    destroy=extend_schema(
        summary="Delete a provider offering",
        description="Deletes a provider offering. Only possible for offerings in a Draft state with no associated resources.",
    ),
)
class ProviderOfferingViewSet(
    UserRoleMixin,
    core_views.HistoryViewSetMixin,
    core_views.CreateReversionMixin,
    core_views.UpdateReversionMixin,
    core_views.ActionsViewSet,
):
    """
    This viewset enables uniform implementation of resource import.

    Consider the following example:

    importable_resources_backend_method = 'get_tenants_for_import'
    import_resource_executor = executors.TenantImportExecutor

    It is expected that importable_resources_backend_method returns list of dicts, each of which
    contains two mandatory fields: name and backend_id, and one optional field called extra.
    This optional field should be list of dicts, each of which contains two mandatory fields: name and value.

    Note that there are only 3 mandatory parameters:
    * importable_resources_backend_method
    * importable_resources_serializer_class
    * import_resource_serializer_class
    """

    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    queryset = models.Offering.objects.all()
    serializer_class = serializers.ProviderOfferingDetailsSerializer
    create_serializer_class = serializers.OfferingCreateSerializer
    disabled_actions = ["update", "partial_update"]
    filterset_class = filters.OfferingFilter
    filter_backends = (
        DjangoFilterBackend,
        filters.OfferingCustomersFilterBackend,
        filters.OfferingImportableFilterBackend,
        filters.ExternalOfferingFilterBackend,
    )

    def _check_extra_field_needed(self, field_name):
        return (
            field_name == self.request.query_params.get("o", "")
            or "-" + field_name == self.request.query_params.get("o", "")
            or self.detail
        )

    def get_queryset(self):
        queryset = super().get_queryset()

        # add total_customers
        if self._check_extra_field_needed("total_customers"):
            resources = (
                models.Resource.objects.filter(
                    offering=OuterRef("pk"),
                    state__in=(
                        ResourceStates.OK,
                        ResourceStates.UPDATING,
                        ResourceStates.TERMINATING,
                    ),
                )
                .order_by()
                .values("offering")
            )
            total_customers = resources.annotate(
                total=Count(
                    "project__customer_id",
                    distinct=True,
                    output_field=IntegerField(),
                )
            ).values("total")
            queryset = queryset.annotate(total_customers=Coalesce(total_customers, 0))

        # add total_cost
        if self._check_extra_field_needed("total_cost"):
            items = (
                invoice_models.InvoiceItem.objects.filter(
                    resource__offering=OuterRef("pk"),
                    invoice__year=core_utils.get_last_month().year,
                    invoice__month=core_utils.get_last_month().month,
                )
                .order_by()
                .annotate(
                    price=ExpressionWrapper(
                        F("quantity") * F("unit_price"), output_field=IntegerField()
                    )
                )
                .values("resource__offering")
            )
            total_cost = items.annotate(
                total=Sum(
                    "price",
                    output_field=IntegerField(),
                )
            ).values("total")
            queryset = queryset.annotate(total_cost=Coalesce(total_cost, 0))

        # add total_cost_estimated
        if self._check_extra_field_needed("total_cost_estimated"):
            current_month = datetime.date.today()
            items = (
                invoice_models.InvoiceItem.objects.filter(
                    resource__offering=OuterRef("pk"),
                    invoice__year=current_month.year,
                    invoice__month=current_month.month,
                )
                .order_by()
                .annotate(
                    price=ExpressionWrapper(
                        F("quantity") * F("unit_price"), output_field=IntegerField()
                    )
                )
                .values("resource__offering")
            )
            total_cost = items.annotate(
                total=Sum(
                    "price",
                    output_field=IntegerField(),
                )
            ).values("total")
            queryset = queryset.annotate(total_cost_estimated=Coalesce(total_cost, 0))

        # Prefetch nested SLURM relations to avoid N+1 when the offering detail
        # payload includes partitions/qos_profiles — each partition nests a
        # qos_options -> SlurmOfferingQoS chain and each QoS FKs the offering.
        if self.detail:
            queryset = queryset.prefetch_related(
                "qos_profiles",
                "partitions__qos_options__qos",
            )

        return queryset

    destroy_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.DELETE_OFFERING,
            ["customer"],
        ),
    ]

    def destroy(self, request, *args, **kwargs):
        offering: models.Offering = self.get_object()

        if offering.plugin_options.get(
            "restrict_deletion_with_active_resources", False
        ):
            active_resources_count = (
                models.Resource.objects.filter(offering=offering)
                .exclude(state=ResourceStates.TERMINATED)
                .count()
            )
            if active_resources_count > 0:
                return Response(
                    {
                        "detail": _(
                            "Offering cannot be deleted since it has active resources and deletion restriction is enabled."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        serializer = serializers.ProviderOfferingSerializer(
            offering, many=False, context=self.get_serializer_context()
        )
        if self.request.user.is_staff is not True:
            if serializer.data["resources_count"] != 0:
                return Response(
                    {"detail": _("Offering was not deleted since it has resources.")},
                    status=status.HTTP_403_FORBIDDEN,
                )
            elif offering.state != OfferingStates.DRAFT:
                return Response(
                    {
                        "detail": _(
                            "Offering was not deleted since offering is not in draft state."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            else:
                offering.delete()
                return Response(
                    status=status.HTTP_204_NO_CONTENT,
                )
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary="Activate an offering",
        description="Activates a draft or paused offering, making it available for ordering.",
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        return self._update_state("activate")

    @extend_schema(
        summary="Move an offering to draft",
        description="Moves an active or paused offering back to the draft state for editing.",
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def draft(self, request, uuid=None):
        return self._update_state("draft")

    @extend_schema(
        summary="List access subnets for an offering",
        description="Returns the access subnets consumers defined for the "
        "offering, in two forms: 'expanded' — every subnet with its customer and "
        "offering context; and 'packed' — the same subnets collapsed into the "
        "minimal set of CIDRs (adjacent/overlapping networks merged). Consumer "
        "subnets are defined per (customer, offering) pair and apply to all of "
        "that customer's resources of the offering. Intended for service "
        "providers building an external firewall allow-list. Available to staff, "
        "support, the offering's service manager and the offering customer owner.",
        responses=serializers.OfferingAccessSubnetsSerializer,
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def access_subnets(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        marketplace_permissions.ensure_offering_provider_access(request.user, offering)
        subnets = (
            models.AccessSubnetOfferingScope.objects.filter(offering=offering)
            .exclude(access_subnet__inet__isnull=True)
            # Dormant scopes — the organization terminated its last resource of
            # this offering — are kept but must not reach the allow-list.
            .filter(utils._live_resource_exists())
            .select_related("access_subnet", "access_subnet__customer", "offering")
            .order_by("access_subnet__inet")
        )
        expanded = [self._expand_consumer_subnet(scope) for scope in subnets]
        default_subnets = list(
            models.OfferingAccessSubnet.objects.filter(offering=offering)
            .exclude(inet__isnull=True)
            .values_list("inet", flat=True)
        )
        defaults = [str(inet) for inet in default_subnets]
        # The packed allow-list merges the consumer subnets with the
        # provider-default subnets of the offering.
        consumer_inets = [scope.access_subnet.inet for scope in subnets]
        packed = [
            str(network)
            for network in core_utils.merge_access_subnets(
                consumer_inets + default_subnets
            )
        ]
        serializer = serializers.OfferingAccessSubnetsSerializer(
            {"expanded": expanded, "packed": packed, "defaults": defaults}
        )
        return Response(serializer.data)

    @staticmethod
    def _expand_consumer_subnet(scope):
        subnet = scope.access_subnet
        return {
            "inet": str(subnet.inet),
            "description": subnet.description,
            "is_staff_managed": subnet.is_staff_managed,
            "customer_uuid": subnet.customer.uuid.hex,
            "customer_name": subnet.customer.name,
            "offering_uuid": scope.offering.uuid.hex,
            "offering_name": scope.offering.name,
        }

    @extend_schema(
        summary="Aggregate access subnets across offerings",
        description="Returns the combined access-subnet allow-list of the given "
        "offerings: 'expanded' — every consumer subnet with its customer and "
        "offering context; 'defaults' — the provider-default subnets "
        "of each offering; 'organization_subnets' — organization-level access "
        "subnets of customers owning non-terminated resources of the offerings "
        "(populated only when include_organization_subnets is true); and 'packed' "
        "— all of the above collapsed into the minimal set of CIDRs. Intended for "
        "service providers building an external firewall allow-list spanning "
        "several offerings. The caller must be staff, support, a service manager "
        "of every requested offering or an owner of its customer.",
        parameters=[
            OpenApiParameter(
                name="offering_uuid",
                type=OpenApiTypes.UUID,
                many=True,
                required=True,
                location=OpenApiParameter.QUERY,
                description="UUID of an offering to include. May be repeated.",
            ),
            OpenApiParameter(
                name="include_organization_subnets",
                type=OpenApiTypes.BOOL,
                required=False,
                location=OpenApiParameter.QUERY,
                description="Also merge in the organization-level access subnets "
                "of customers owning non-terminated resources of the offerings.",
            ),
        ],
        responses=serializers.AggregatedAccessSubnetsSerializer,
    )
    @action(detail=False, methods=["get"], filter_backends=[])
    def aggregated_access_subnets(self, request):
        request_serializer = serializers.AggregatedAccessSubnetsRequestSerializer(
            data={
                "offering_uuid": request.query_params.getlist("offering_uuid"),
                "include_organization_subnets": request.query_params.get(
                    "include_organization_subnets", False
                ),
            }
        )
        request_serializer.is_valid(raise_exception=True)
        offering_uuids = request_serializer.validated_data["offering_uuid"]
        include_organization_subnets = request_serializer.validated_data[
            "include_organization_subnets"
        ]

        offerings = list(models.Offering.objects.filter(uuid__in=offering_uuids))
        missing = {u.hex for u in offering_uuids} - {o.uuid.hex for o in offerings}
        if missing:
            raise rf_exceptions.ValidationError(
                {
                    "offering_uuid": _("Offerings not found: %s")
                    % ", ".join(sorted(missing))
                }
            )
        for offering in offerings:
            marketplace_permissions.ensure_offering_provider_access(
                request.user, offering
            )

        data = utils.aggregate_access_subnets(
            offering_uuids=offering_uuids,
            include_organization_subnets=include_organization_subnets,
        )
        expanded = [
            self._expand_consumer_subnet(subnet) for subnet in data["consumer_subnets"]
        ]
        defaults = [
            {
                "inet": str(subnet.inet),
                "description": subnet.description,
                "offering_uuid": subnet.offering.uuid.hex,
                "offering_name": subnet.offering.name,
            }
            for subnet in data["offering_defaults"]
        ]
        organization_subnets = [
            {
                "inet": str(subnet.inet),
                "description": subnet.description,
                "customer_uuid": subnet.customer.uuid.hex,
                "customer_name": subnet.customer.name,
            }
            for subnet in data["organization_subnets"]
        ]
        serializer = serializers.AggregatedAccessSubnetsSerializer(
            {
                "expanded": expanded,
                "packed": data["packed"],
                "defaults": defaults,
                "organization_subnets": organization_subnets,
            }
        )
        return Response(serializer.data)

    @extend_schema(
        summary="List orders for an offering",
        description="Returns a paginated list of orders associated with a specific offering.",
        responses=serializers.OrderDetailsSerializer(many=True),
        filters=True,
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def orders(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        marketplace_permissions.ensure_offering_provider_access(request.user, offering)
        queryset = models.Order.objects.filter(offering=offering)
        filterset = filters.OrderFilter(request.query_params, queryset=queryset)
        queryset = filterset.qs
        # Paginate queryset
        page = self.paginate_queryset(queryset)
        serializer = serializers.OrderDetailsSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="Retrieve a specific order for an offering",
        description="Returns details of a specific order associated with an offering.",
        responses=serializers.OrderDetailsSerializer,
    )
    def order_detail(self, request, uuid=None, order_uuid=None):
        offering: models.Offering = self.get_object()
        marketplace_permissions.ensure_offering_provider_access(request.user, offering)
        try:
            order = models.Order.objects.get(offering=offering, uuid=order_uuid)
        except models.Order.DoesNotExist:
            error_message = _("The order with uuid %s does not exist!" % order_uuid)
            logger.error(error_message)
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = serializers.OrderDetailsSerializer(
            order, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Pause an offering",
        description="Pauses an active offering, preventing new orders from being created.",
        responses=serializers.DetailStateSerializer,
        request=serializers.OfferingPauseSerializer,
    )
    @action(detail=True, methods=["post"])
    def pause(self, request, uuid=None):
        return self._update_state("pause", request)

    pause_serializer_class = serializers.OfferingPauseSerializer

    @extend_schema(
        summary="Unpause an offering",
        description="Resumes a paused offering, making it available for ordering again.",
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def unpause(self, request, uuid=None):
        return self._update_state("unpause")

    @extend_schema(
        summary="Mark an offering as unavailable",
        description="Marks an active offering as unavailable, blocking all operations on its resources.",
        responses=serializers.DetailStateSerializer,
        request=None,
    )
    @action(detail=True, methods=["post"])
    def make_unavailable(self, request, uuid=None):
        return self._update_state("make_unavailable")

    @extend_schema(
        summary="Mark an offering as available",
        description="Marks an unavailable offering as available.",
        responses=serializers.DetailStateSerializer,
        request=None,
    )
    @action(detail=True, methods=["post"])
    def make_available(self, request, uuid=None):
        return self._update_state("make_available")

    @extend_schema(
        summary="Archive an offering",
        description="Archives an offering, making it permanently unavailable for new orders.",
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        return self._update_state("archive")

    @extend_schema(
        summary="Effective POSIX ID pool",
        description=(
            "The POSIX ID pool that governs this offering: its own override "
            "pool if present, otherwise the service provider's default pool. "
            "Returns null when no pool is configured."
        ),
        responses={200: serializers.PosixIdPoolSerializer(allow_null=True)},
    )
    @action(detail=True, methods=["get"])
    def effective_posix_id_pool(self, request, uuid=None):
        offering = self.get_object()
        pool = posix_ids.resolve(offering)
        if pool is None:
            return Response(None)
        serializer = serializers.PosixIdPoolSerializer(
            pool, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    def _update_state(self, action, request=None):
        offering: models.Offering = self.get_object()

        try:
            getattr(offering, action)()
        except TransitionNotAllowed:
            raise rf_exceptions.ValidationError(_("Offering state is invalid."))

        with reversion.create_revision():
            if request:
                serializer = self.get_serializer(
                    offering, data=request.data, partial=True
                )
                serializer.is_valid(raise_exception=True)
                offering: models.Offering = serializer.save()

            offering.save(update_fields=["state"])
            reversion.set_user(self.request.user)
            reversion.set_comment(
                f"Offering state has been updated using method {action}"
            )
        return Response(
            {
                "detail": _("Offering state updated."),
                "state": offering.get_state_display(),
            },
            status=status.HTTP_200_OK,
        )

    pause_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.PAUSE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]
    make_unavailable_permissions = make_available_permissions = pause_permissions

    unpause_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.UNPAUSE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]

    archive_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.ARCHIVE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]

    activate_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.CREATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]

    draft_permissions = [
        marketplace_permissions.can_manage_offering_lifecycle,
        permission_factory(
            PermissionEnum.CREATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]

    # Each validator list is built independently on purpose. Previously these
    # were created via chained assignment and `activate_validators += [...]`,
    # whose in-place mutation leaked validate_offering_has_plans into the
    # pause/archive/destroy/make_unavailable lists too — so e.g. deleting a
    # plan-less draft offering wrongly failed with "Offering does not have any
    # billing plans". Only activate and unpause require plans.
    activate_validators = [
        structure_utils.check_customer_blocked_or_archived,
        validate_offering_has_plans,
    ]
    unpause_validators = [validate_offering_has_plans]
    pause_validators = [structure_utils.check_customer_blocked_or_archived]
    archive_validators = [structure_utils.check_customer_blocked_or_archived]
    destroy_validators = [structure_utils.check_customer_blocked_or_archived]
    make_unavailable_validators = [structure_utils.check_customer_blocked_or_archived]

    update_permissions = [can_update_offering]

    update_validators = [
        validate_offering_update,
        structure_utils.check_customer_blocked_or_archived,
    ]

    def perform_create(self, serializer):
        customer = serializer.validated_data["customer"]
        structure_utils.check_customer_blocked_or_archived(customer)

        super().perform_create(serializer)

    @extend_schema(
        summary="List importable resources",
        description="Returns a paginated list of resources that can be imported for this offering.",
        filters=False,
        request=None,
        responses=serializers.ImportableResourceSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def importable_resources(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        method = plugins.manager.get_importable_resources_backend_method(offering.type)
        if (
            not method
            or not offering.scope
            or not hasattr(offering.scope, "get_backend")
        ):
            raise rf_exceptions.ValidationError(
                "Current offering plugin does not support resource import"
            )

        backend = offering.scope.get_backend()

        try:
            if isinstance(offering.scope, structure_models.BaseResource):
                resources = getattr(backend, method)(offering.scope)
            else:
                resources = getattr(backend, method)()
        except Exception as e:
            resources = []
            logger.error(
                "Listing importable resources of offering %s failed. Error %s",
                offering,
                str(e),
            )

        page = self.paginate_queryset(resources)
        return self.get_paginated_response(page)

    importable_resources_permissions = [permissions.user_can_list_importable_resources]

    import_resource_permissions = [permissions.user_can_list_importable_resources]

    import_resource_serializer_class = serializers.ImportResourceSerializer

    @extend_schema(
        summary="Import a resource",
        description="Imports a backend resource into the marketplace.",
        request=serializers.ImportResourceSerializer,
        responses=serializers.ResourceSerializer,
    )
    @action(detail=True, methods=["post"])
    def import_resource(self, request, uuid=None):
        import_resource_serializer = self.get_serializer(data=request.data)
        import_resource_serializer.is_valid(raise_exception=True)

        plan = import_resource_serializer.validated_data.get("plan", None)
        project = import_resource_serializer.validated_data["project"]
        backend_id = import_resource_serializer.validated_data["backend_id"]
        additional_details = import_resource_serializer.validated_data.get(
            "additional_details", {}
        )

        offering: models.Offering = self.get_object()
        utils.validate_backend_id(backend_id, offering)
        backend = offering.scope.get_backend()
        method = plugins.manager.import_resource_backend_method(offering.type)
        if not method:
            raise rf_exceptions.ValidationError(
                "Current offering plugin does not support resource import"
            )

        resource_model = plugins.manager.get_resource_model(offering.type)

        if isinstance(offering.scope, structure_models.BaseResource):
            field = "tenant"
        else:
            field = "service_settings"

        value = offering.scope

        if resource_model.objects.filter(
            **{field: value}, backend_id=backend_id
        ).exists():
            raise rf_exceptions.ValidationError(
                _("Resource has been imported already.")
            )

        try:
            if isinstance(offering.scope, structure_models.BaseResource):
                resource = getattr(backend, method)(
                    offering.scope, backend_id=backend_id, project=project
                )
            else:
                resource = getattr(backend, method)(
                    backend_id=backend_id, project=project, **additional_details
                )
        except ServiceBackendError as e:
            raise rf_exceptions.ValidationError(str(e))
        else:
            resource_imported.send(
                sender=resource.__class__,
                instance=resource,
                plan=plan,
                offering=offering,
            )

        import_resource_executor = plugins.manager.get_import_resource_executor(
            offering.type
        )

        if import_resource_executor:
            transaction.on_commit(lambda: import_resource_executor.execute(resource))

        marketplace_resource = models.Resource.objects.get(scope=resource)
        resource_serializer = serializers.ResourceSerializer(
            marketplace_resource, context=self.get_serializer_context()
        )

        return Response(data=resource_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Update offering attributes",
        description="Updates the attributes of an offering.",
        request=dict,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def update_attributes(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        if not isinstance(request.data, dict):
            raise rf_exceptions.ValidationError("Dictionary is expected.")
        validate_attributes(request.data, offering.category)
        offering.attributes = request.data
        with reversion.create_revision():
            offering.save(update_fields=["attributes"])
            reversion.set_user(self.request.user)
            reversion.set_comment("Offering attributes have been updated via REST API")
        return Response(status=status.HTTP_200_OK)

    update_attributes_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_ATTRIBUTES,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_attributes_validators = update_validators

    def _update_action(self, request):
        offering: models.Offering = self.get_object()
        serializer = self.get_serializer(offering, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    @staticmethod
    def _extract_validation_errors(exc):
        """Extract flat list of error strings from a DRF ValidationError."""
        messages = []
        detail = exc.detail
        if isinstance(detail, dict):
            for field_errors in detail.values():
                if isinstance(field_errors, list):
                    messages.extend(str(err) for err in field_errors)
                else:
                    messages.append(str(field_errors))
        elif isinstance(detail, list):
            messages.extend(str(err) for err in detail)
        else:
            messages.append(str(detail))
        return messages

    @extend_schema(
        summary="Update offering location",
        description="Updates the geographical location (latitude and longitude) of an offering.",
        request=serializers.OfferingLocationUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_location(self, request, uuid=None):
        return self._update_action(request)

    update_location_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_LOCATION,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_location_validators = update_validators
    update_location_serializer_class = serializers.OfferingLocationUpdateSerializer

    @extend_schema(
        summary="Update offering category",
        description="Updates the category of an offering.",
        request=serializers.OfferingDescriptionUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_description(self, request, uuid=None):
        return self._update_action(request)

    update_description_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_DESCRIPTION,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_description_validators = update_validators
    update_description_serializer_class = (
        serializers.OfferingDescriptionUpdateSerializer
    )

    @extend_schema(
        summary="Update offering overview",
        description="Updates the overview fields of an offering, such as name, description, and getting started guide.",
        request=serializers.OfferingOverviewUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_overview(self, request, uuid=None):
        return self._update_action(request)

    update_overview_permissions = [can_update_offering]
    update_overview_validators = update_validators
    update_overview_serializer_class = serializers.OfferingOverviewUpdateSerializer

    @extend_schema(
        summary="Swap offering type",
        description=(
            "Changes the offering's `type` between Marketplace.Basic and the "
            "site-agent type (Marketplace.Slurm). Both plugins share the same "
            "data shape (the site-agent processors inherit from Basic and only "
            "delegate the send paths to the external agent), so the swap is "
            "safe in either direction. Refused if the offering's current type "
            "is not in the swappable set."
        ),
        request=serializers.OfferingTypeUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_type(self, request, uuid=None):
        return self._update_action(request)

    update_type_permissions = [can_update_offering]
    update_type_validators = update_validators
    update_type_serializer_class = serializers.OfferingTypeUpdateSerializer

    @extend_schema(
        summary="Update offering options",
        description="Updates the order form options for an offering.",
        request=serializers.OfferingOptionsUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_options(self, request, uuid=None):
        return self._update_action(request)

    update_options_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_options_validators = update_validators
    update_options_serializer_class = serializers.OfferingOptionsUpdateSerializer

    @extend_schema(
        summary="Bind / unbind offering to a service profile",
        description=(
            "Sets the offering's `profile` FK. Pass `profile: <uuid>` to bind, "
            "or `profile: null` to unbind. Requires UPDATE_OFFERING permission "
            "on the offering's customer (service-provider owners and staff). "
            "Triggers async reconciliation of RoleAvailability rows on this "
            "offering against the profile's role catalog (or wipes them on "
            "unbind)."
        ),
        request=serializers.OfferingProfileBindSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_profile(self, request, uuid=None):
        offering = self.get_object()
        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING, offering.customer
        ):
            raise rf_exceptions.PermissionDenied(
                "You do not have permission to bind a service profile to this offering."
            )
        ser = serializers.OfferingProfileBindSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        profile_uuid = ser.validated_data.get("profile")
        if profile_uuid is None:
            offering.profile = None
        else:
            try:
                offering.profile = models.OfferingProfile.objects.get(uuid=profile_uuid)
            except models.OfferingProfile.DoesNotExist:
                raise rf_exceptions.NotFound("OfferingProfile not found.")
        offering.save(update_fields=["profile"])
        return Response(
            {
                "profile_uuid": offering.profile.uuid.hex if offering.profile else None,
                "profile_name": offering.profile.name if offering.profile else None,
            }
        )

    set_profile_serializer_class = serializers.OfferingProfileBindSerializer

    @extend_schema(
        summary="Assign or clear the offering group",
        description=(
            "Sets the offering's ``offering_group`` FK. Pass "
            "``offering_group: <uuid>`` to assign a group, or "
            "``offering_group: null`` to clear it. The group must belong to "
            "the same customer as the offering."
        ),
        request=serializers.OfferingGroupAssignSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_offering_group(self, request, uuid=None):
        offering = self.get_object()
        ser = serializers.OfferingGroupAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        offering_group = ser.validated_data["offering_group"]
        if (
            offering_group is not None
            and offering_group.customer_id != offering.customer_id
        ):
            raise rf_exceptions.ValidationError(
                {
                    "offering_group": _(
                        "Offering group must belong to the same customer as the offering."
                    )
                }
            )
        offering.offering_group = offering_group
        offering.save(update_fields=["offering_group"])
        return Response(
            {
                "offering_group_uuid": (
                    offering.offering_group.uuid.hex
                    if offering.offering_group
                    else None
                ),
                "offering_group_title": (
                    offering.offering_group.title if offering.offering_group else None
                ),
            }
        )

    set_offering_group_permissions = [can_update_offering]
    set_offering_group_validators = [validate_offering_update]
    set_offering_group_serializer_class = serializers.OfferingGroupAssignSerializer

    @extend_schema(
        summary="Update offering resource options",
        description="Updates the resource report form options for an offering.",
        request=serializers.OfferingResourceOptionsUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_resource_options(self, request, uuid=None):
        return self._update_action(request)

    update_resource_options_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_resource_options_validators = update_validators
    update_resource_options_serializer_class = (
        serializers.OfferingResourceOptionsUpdateSerializer
    )

    @extend_schema(
        summary="Update offering integration settings",
        description="Updates the backend integration settings for an offering, including plugin options, secret options, and service attributes.",
        request=serializers.OfferingIntegrationUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_integration(self, request, uuid=None):
        return self._update_action(request)

    update_integration_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_integration_validators = update_validators
    update_integration_serializer_class = (
        serializers.OfferingIntegrationUpdateSerializer
    )

    @extend_schema(
        summary="Update offering compliance checklist",
        description="Associates a compliance checklist with an offering.",
        request=serializers.OfferingComplianceChecklistUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_compliance_checklist(self, request, uuid=None):
        return self._update_action(request)

    update_compliance_checklist_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_compliance_checklist_validators = update_validators
    update_compliance_checklist_serializer_class = (
        serializers.OfferingComplianceChecklistUpdateSerializer
    )

    def _update_media(
        self, request: Request, serializer_class: type[Serializer]
    ) -> Response:
        """Helper for updating offering media."""
        offering: models.Offering = self.get_object()
        serializer = serializer_class(instance=offering, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    def _delete_media(self, media_field: str) -> Response:
        """Helper for deleting offering media."""
        offering: models.Offering = self.get_object()
        getattr(offering, media_field).delete()
        offering.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Update offering thumbnail",
        description="Uploads or replaces the thumbnail image for an offering.",
        request=serializers.OfferingThumbnailSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_thumbnail(self, request, uuid=None):
        return self._update_media(request, serializers.OfferingThumbnailSerializer)

    @extend_schema(
        summary="Delete offering thumbnail",
        description="Deletes the thumbnail image of an offering.",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_thumbnail(self, request, uuid=None):
        return self._delete_media("thumbnail")

    @extend_schema(
        summary="Update offering image",
        description="Uploads or replaces the main image for an offering.",
        request=serializers.OfferingImageSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_image(self, request, uuid=None):
        return self._update_media(request, serializers.OfferingImageSerializer)

    @extend_schema(
        summary="Delete offering image",
        description="Deletes the main image of an offering.",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_image(self, request, uuid=None):
        return self._delete_media("image")

    @extend_schema(
        summary="Upload markdown image",
        description=(
            "Uploads an image for embedding in offering markdown descriptions. "
            "Requires ENABLE_MARKDOWN_IMAGE_UPLOAD Constance setting."
        ),
        request=serializers.MarkdownImageUploadSerializer,
        responses={201: serializers.MarkdownImageUploadResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def upload_markdown_image(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file_obj = media_utils.store_markdown_image(serializer.validated_data["image"])
        media_url = request.build_absolute_uri(
            reverse("media", kwargs={"uuid": file_obj.uuid.hex})
        )
        response_serializer = serializers.MarkdownImageUploadResponseSerializer(
            {"url": media_url}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    upload_markdown_image_permissions = [
        marketplace_permissions.markdown_image_upload_is_enabled,
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_DESCRIPTION,
            ["*", "customer", "customer.serviceprovider"],
        ),
    ]
    upload_markdown_image_validators = update_validators
    upload_markdown_image_serializer_class = serializers.MarkdownImageUploadSerializer

    media_permissions = [permissions.user_can_update_thumbnail]
    update_thumbnail_permissions = media_permissions
    delete_thumbnail_permissions = media_permissions
    update_image_permissions = media_permissions
    delete_image_permissions = media_permissions

    @extend_schema(
        summary="Get customers for an offering",
        description="Returns a paginated list of customers who have resources for this offering.",
        responses=serializers.ProviderOfferingCustomerSerializer(many=True),
    )
    @action(detail=True)
    def customers(self, request, uuid):
        offering: models.Offering = self.get_object()
        active_customers = utils.get_active_customers(request, self)
        customer_queryset = utils.get_offering_customers(offering, active_customers)
        serializer_class = serializers.ProviderOfferingCustomerSerializer
        serializer = serializer_class(
            instance=customer_queryset, many=True, context=self.get_serializer_context()
        )
        page = self.paginate_queryset(serializer.data)
        return self.get_paginated_response(page)

    customers_permissions = [structure_permissions.is_owner]

    def get_stats(self, get_queryset, serializer, serializer_context=None):
        offering: models.Offering = self.get_object()
        active_customers = utils.get_active_customers(self.request, self)
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource__offering=offering,
            invoice__customer__in=active_customers,
            invoice__created__gte=start,
            invoice__created__lte=end,
        )
        queryset = get_queryset(invoice_items)
        serializer = serializer(
            instance=queryset, many=True, context=serializer_context
        )
        page = self.paginate_queryset(serializer.data)
        return self.get_paginated_response(page)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start date in format YYYY-MM.",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End date in format YYYY-MM.",
            ),
            OpenApiParameter(
                name="accounting_is_running",
                type=bool,
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses=serializers.ProviderOfferingCostsSerializer(many=True),
        summary="Get costs for an offering",
        description="Returns monthly cost data for an offering within a specified date range.",
    )
    @action(detail=True)
    def costs(self, *args, **kwargs):
        return self.get_stats(
            utils.get_offering_costs, serializers.ProviderOfferingCostsSerializer
        )

    costs_permissions = [structure_permissions.is_owner]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start date in format YYYY-MM.",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End date in format YYYY-MM.",
            ),
        ],
        responses=serializers.OfferingComponentStatSerializer(many=True),
        summary="Get statistics for offering components",
        description="Returns monthly usage statistics for the components of an offering within a specified date range.",
    )
    @action(detail=True)
    def component_stats(self, *args, **kwargs):
        offering: models.Offering = self.get_object()
        offering_components_map = {
            component.type: component for component in offering.components.all()
        }

        def get_offering_component_stats(invoice_items):
            return (
                invoice_items.filter(
                    details__offering_component_type__in=offering_components_map.keys()
                )
                .values(
                    "details__offering_component_type",
                    "invoice__year",
                    "invoice__month",
                )
                .order_by(
                    "details__offering_component_type",
                    "invoice__year",
                    "invoice__month",
                )
                .annotate(total_quantity=Sum("quantity"))
            )

        serializer_context = {
            "offering_components_map": offering_components_map,
        }
        return self.get_stats(
            get_offering_component_stats,
            serializers.OfferingComponentStatSerializer,
            serializer_context,
        )

    component_stats_permissions = [structure_permissions.is_owner]

    @extend_schema(
        summary="Get offering statistics",
        description="Returns basic statistics for an offering, such as the number of active resources and customers.",
        responses={
            200: {
                "type": "object",
                "properties": {
                    "resources_count": {"type": "integer"},
                    "customers_count": {"type": "integer"},
                },
            }
        },
    )
    @action(detail=True)
    def stats(self, *args, **kwargs):
        offering: models.Offering = self.get_object()
        resources_count = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=ResourceStates.TERMINATED)
            .count()
        )
        customers_count = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=ResourceStates.TERMINATED)
            .values("project__customer")
            .distinct()
            .count()
        )
        return Response(
            {
                "resources_count": resources_count,
                "customers_count": customers_count,
            },
            status=status.HTTP_200_OK,
        )

    stats_permissions = [structure_permissions.is_owner]

    @extend_schema(
        summary="Get offering resource and user state counters",
        description="Returns resource and offering-user counts grouped by state for the given offering.",
        responses=serializers.OfferingStateCountersSerializer,
    )
    @action(detail=True)
    def state_counters(self, request, uuid=None):
        offering: models.Offering = self.get_object()

        resource_counts = (
            models.Resource.objects.filter(offering=offering)
            .values("state")
            .annotate(count=Count("id"))
            .order_by("state")
        )

        user_counts = (
            models.OfferingUser.objects.filter(offering=offering)
            .values("state")
            .annotate(count=Count("id"))
            .order_by("state")
        )

        resource_state_map = dict(ResourceStates.CHOICES)
        user_state_map = dict(OfferingUserStates.CHOICES)

        data = {
            "resources": [
                {
                    "state": resource_state_map.get(item["state"], str(item["state"])),
                    "count": item["count"],
                }
                for item in resource_counts
            ],
            "users": [
                {
                    "state": user_state_map.get(item["state"], str(item["state"])),
                    "count": item["count"],
                }
                for item in user_counts
            ],
        }
        serializer = serializers.OfferingStateCountersSerializer(instance=data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    state_counters_permissions = [structure_permissions.is_owner]

    @extend_schema(
        summary="Update organization groups for offering",
        description="Sets the list of organization groups that can access this offering.",
        request=serializers.OrganizationGroupsSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_organization_groups(self, request, uuid):
        offering: models.Offering = self.get_object()
        serializer = serializers.OrganizationGroupsSerializer(
            instance=offering, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_organization_groups_permissions = [structure_permissions.is_owner]
    update_organization_groups_validators = update_validators

    @extend_schema(
        summary="Delete organization groups for offering",
        description="Removes all organization group associations from this offering, making it accessible to all.",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        offering.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions
    delete_organization_groups_validators = update_validators

    @extend_schema(
        summary="Update tags for offering",
        description="Sets the list of tags for this offering.",
        request=serializers.TagsSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_tags(self, request, uuid):
        offering: models.Offering = self.get_object()
        serializer = serializers.TagsSerializer(
            instance=offering, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_tags_permissions = [structure_permissions.is_owner]
    update_tags_validators = update_validators

    @extend_schema(
        summary="Delete tags for offering",
        description="Removes all tag associations from this offering.",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_tags(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        offering.tags.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_tags_permissions = update_tags_permissions
    delete_tags_validators = update_validators

    @extend_schema(
        summary="Add an access endpoint to an offering",
        description="Adds a new access endpoint (URL) to an offering.",
        request=serializers.NestedEndpointSerializer,
        responses={201: serializers.EndpointUUIDSerializer},
    )
    @action(detail=True, methods=["post"])
    def add_endpoint(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        url = serializer.validated_data["url"]
        self._validate_endpoint_domain(offering, url)

        endpoint = models.OfferingAccessEndpoint.objects.create(
            offering=offering,
            url=url,
            name=serializer.validated_data["name"],
        )

        return Response(
            {"uuid": endpoint.uuid},
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _validate_endpoint_domain(offering: models.Offering, url: str) -> None:
        """
        Validate that the endpoint URL's domain is within the allowed domains
        configured on the service provider.
        """
        try:
            service_provider = offering.customer.serviceprovider
        except models.ServiceProvider.DoesNotExist:
            return

        allowed_domains = service_provider.allowed_domains
        if not allowed_domains:
            return

        hostname = (urlparse(url).hostname or "").lower()
        for domain in allowed_domains:
            if hostname == domain.lower() or hostname.endswith("." + domain.lower()):
                return

        raise ValidationError(
            _(
                "Endpoint URL domain '%(hostname)s' is not in the list of allowed domains "
                "for this service provider. Allowed domains: %(domains)s."
            )
            % {
                "hostname": hostname,
                "domains": ", ".join(allowed_domains),
            }
        )

    add_endpoint_permissions = [
        permission_factory(
            PermissionEnum.ADD_OFFERING_ENDPOINT,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    add_endpoint_serializer_class = serializers.NestedEndpointSerializer
    add_endpoint_validators = update_validators

    @extend_schema(
        summary="Delete an access endpoint from an offering",
        description="Deletes an existing access endpoint from an offering by its UUID.",
        request=serializers.EndpointUUIDSerializer,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_endpoint(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering.endpoints.filter(uuid=serializer.validated_data["uuid"]).delete()
        return Response(
            status=status.HTTP_204_NO_CONTENT,
        )

    delete_endpoint_serializer_class = serializers.EndpointUUIDSerializer
    delete_endpoint_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING_ENDPOINT,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    delete_endpoint_validators = update_validators

    @extend_schema(
        summary="List offerings grouped by provider",
        description="Returns a paginated list of active, shared offerings grouped by their service provider.",
        responses=serializers.OfferingGroupsSerializer(many=True),
    )
    @action(detail=False, permission_classes=[], filter_backends=[DjangoFilterBackend])
    def groups(self, *args, **kwargs):
        OFFERING_LIMIT = 4
        qs = self.filter_queryset(
            self.get_queryset().filter(shared=True, state=OfferingStates.ACTIVE)
        )
        customer_ids = self.paginate_queryset(
            qs.order_by("customer__name")
            .values_list("customer_id", flat=True)
            .distinct()
        )
        customers = {
            customer.id: customer
            for customer in structure_models.Customer.objects.filter(
                id__in=customer_ids
            )
        }
        return self.get_paginated_response(
            data=[
                {
                    "customer_name": customers[customer_id].name,
                    "customer_uuid": customers[customer_id].uuid.hex,
                    "offerings": [
                        {
                            "offering_name": offering.name,
                            "offering_uuid": offering.uuid.hex,
                        }
                        for offering in qs.filter(customer_id=customer_id)[
                            :OFFERING_LIMIT
                        ]
                    ],
                }
                for customer_id in customer_ids
            ]
        )

    @extend_schema(
        summary="Get GLauth user configuration",
        description="""
        This endpoint provides a configuration file for GLauth.
        It is intended to be used by an external agent to synchronize user data from Waldur to GLauth.

        Example output format:
        ```
        [[users]]
          name = "johndoe"
          givenname="John"
          sn="Doe"
          mail = "john.doe@example.com"
          ...
        [[groups]]
          name = "group1"
          gidnumber = 1001
        ```
        """,
        request=None,
        responses=str,
        parameters=[],
    )
    @action(
        detail=True,
        methods=["GET"],
        renderer_classes=[PlainTextRenderer],
    )
    def glauth_users_config(self, request, uuid=None):
        """
        This endpoint provides a config file for GLauth
        Example: https://github.com/glauth/glauth/blob/master/v2/sample-simple.cfg
        It is assumed that the config is used by an external agent,
        which synchronizes data from Waldur to GLauth
        """
        offering: models.Offering = self.get_object()
        if not offering.plugin_options.get(
            "service_provider_can_create_offering_user", False
        ):
            logger.warning(
                "Offering %s doesn't have feature service_provider_can_create_offering_user enabled, skipping GLauth config generation",
                offering,
            )
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Offering %s doesn't have feature service_provider_can_create_offering_user enabled"
                % offering,
            )

        _stamp_glauth_integration_status(offering, request)

        response_text = _render_glauth_toml(offering, resource_filter=None)
        return Response(response_text)

    glauth_users_config_permissions = [structure_permissions.is_offering_manager]

    @extend_schema(
        summary="Get structured GLauth tree for an offering",
        description=(
            "Returns the same set of users, groups and robot accounts as "
            "`glauth_users_config`, but as a structured JSON tree suitable "
            "for navigation in admin UIs. Source of truth for the TOML "
            "endpoint."
        ),
        request=None,
        responses={status.HTTP_200_OK: serializers.GlauthTreeSerializer},
        parameters=[],
    )
    @action(detail=True, methods=["GET"])
    def glauth_tree(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        if not offering.plugin_options.get(
            "service_provider_can_create_offering_user", False
        ):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data=(
                    "Offering %s doesn't have feature "
                    "service_provider_can_create_offering_user enabled" % offering
                ),
            )
        _stamp_glauth_integration_status(offering, request)
        tree = utils.build_glauth_tree(offering)
        serializer = serializers.GlauthTreeSerializer(_strip_internal(tree))
        return Response(serializer.data)

    glauth_tree_permissions = [structure_permissions.is_offering_manager]

    @extend_schema(
        summary="Check user access to offering resources",
        description="Checks if a specified user has access to any non-terminated resource of this offering.",
        request=None,
        parameters=[
            OpenApiParameter(
                name="username",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Username of the user to check.",
                required=True,
            ),
        ],
        filters=False,
    )
    @extend_schema(
        responses={status.HTTP_200_OK: serializers.UserHasResourceAccessSerializer}
    )
    @action(detail=True, methods=["GET"])
    def user_has_resource_access(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        username = request.query_params.get("username")
        if username is None:
            raise rf_exceptions.ValidationError(
                _("Username is missing in query parameters.")
            )

        try:
            user = core_models.User.objects.get(username=username)
        except core_models.User.DoesNotExist:
            error_message = _("The user with username %s does not exist!" % username)
            logger.error(error_message)
            raise rf_exceptions.ValidationError(error_message)

        has_access = utils.is_user_related_to_offering(offering, user)

        return Response(
            {"has_access": has_access},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.UpdateOfferingComponent,
        responses=None,
        summary="Update an offering component",
        description="Updates the properties of a specific component within an offering.",
    )
    @action(detail=True, methods=["post"])
    def update_offering_component(self, request, uuid=None):
        offering: models.Offering = self.get_object()

        component_to_update_uuid = request.data.get("uuid")

        if component_to_update_uuid:
            offering_component = offering.components.filter(
                uuid=component_to_update_uuid
            ).first()

            if offering_component:
                # Store original component type to detect changes
                original_type = offering_component.type
                new_type = request.data.get("type")

                serializer = self.get_serializer(
                    instance=offering_component, data=request.data, partial=True
                )
                serializer.is_valid(raise_exception=True)

                # If component type is being changed, migrate connected objects
                if new_type and new_type != original_type:
                    logger.info(
                        f"Component type change detected: {original_type} -> {new_type}"
                    )

                    try:
                        with transaction.atomic():
                            # Save the component with new type first
                            serializer.save()

                            # Migrate connected objects
                            self._migrate_component_connected_objects(
                                offering=offering,
                                old_component_type=original_type,
                                new_component_type=new_type,
                                logger=logger,
                            )

                            logger.info(
                                f"Successfully migrated component {original_type} -> {new_type}"
                            )
                    except Exception as e:
                        logger.error(f"Error during component migration: {e}")
                        return Response(
                            {
                                "details": _(
                                    "An error occurred during component migration."
                                )
                            },
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )

                else:
                    # Normal update without type change
                    serializer.save()

                return Response(status=status.HTTP_200_OK)
            else:
                return Response(status=status.HTTP_404_NOT_FOUND)
        else:
            return Response(
                {"details": _("UUID for offering component was not provided.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

    def _migrate_component_connected_objects(
        self, offering, old_component_type, new_component_type, logger
    ):
        """
        Migrate connected objects when component type changes.
        Updates Resource limits and InvoiceItem details.
        """
        # 1. Update Resource limits for resources of this offering
        resources_updated = 0
        for resource in models.Resource.objects.filter(offering=offering):
            if old_component_type in resource.limits:
                old_value = resource.limits[old_component_type]
                resource.limits[new_component_type] = resource.limits.pop(
                    old_component_type
                )
                resource.save(update_fields=["limits"])
                resources_updated += 1
                logger.info(
                    f"Updated Resource {resource.uuid}: {old_component_type}={old_value} -> {new_component_type}={old_value}"
                )

        # 2. Update InvoiceItem details for historical billing data
        invoice_items_updated = 0
        invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource__offering=offering,
            details__offering_component_type=old_component_type,
        )

        for item in invoice_items:
            item.details["offering_component_type"] = new_component_type
            item.save(update_fields=["details"])
            invoice_items_updated += 1
            logger.info(
                f"Updated InvoiceItem {item.uuid}: offering_component_type {old_component_type} -> {new_component_type}"
            )

        logger.info(
            f"Migration summary: Updated {resources_updated} resource limits, {invoice_items_updated} invoice items"
        )

    update_offering_component_serializer_class = serializers.UpdateOfferingComponent
    update_offering_component_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_offering_component_validators = update_validators

    @extend_schema(
        summary="Remove an offering component",
        description="Removes a custom component from an offering. Built-in components cannot be removed.",
        request=serializers.RemoveOfferingComponentSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def remove_offering_component(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        resources_exist = models.Resource.objects.filter(offering=offering).exists()

        component_to_remove_uuid = request.data.get("uuid")
        if not component_to_remove_uuid:
            return Response(
                {
                    "details": _(
                        "UUID for offering component to remove was not provided."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        offering_component = offering.components.filter(
            uuid=component_to_remove_uuid
        ).first()

        if not offering_component:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if resources_exist:
            return Response(
                {
                    "details": _(
                        "The component %s cannot be removed because it is already used"
                    )
                    % offering_component.name
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        builtin_components = plugins.manager.get_components(offering.type)
        valid_types = {component.type for component in builtin_components}
        if offering_component.type in valid_types:
            return Response(
                {
                    "details": _(
                        "The component %s cannot be removed because it is builtin"
                    )
                    % offering_component.type
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        offering_component.delete()
        return Response(status=status.HTTP_200_OK)

    remove_offering_component_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    remove_offering_component_validators = update_validators

    @extend_schema(
        request=serializers.SwitchBillingModeSerializer,
        responses={200: None},
        summary="Switch billing mode for builtin components",
        description="Switches all builtin components between monthly (LIMIT), "
        "prepaid (ONE_TIME + is_prepaid), and usage-based billing modes. "
        "Works for any offering type that has registered builtin components.",
    )
    @action(detail=True, methods=["post"])
    def switch_billing_mode(self, request, uuid=None):
        offering: models.Offering = self.get_object()

        if not offering.components.exists():
            return Response(
                {
                    "detail": _(
                        "Billing mode switching requires at least one component."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Prevent switching billing mode when active resources exist
        # to avoid billing inconsistencies
        active_resources = models.Resource.objects.filter(
            offering=offering,
        ).exclude(state__in=[ResourceStates.CREATING, ResourceStates.TERMINATED])
        if active_resources.exists():
            return Response(
                {
                    "detail": _(
                        "Cannot switch billing mode while there are active resources. "
                        "All resources must be terminated first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = serializers.SwitchBillingModeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mode = serializer.validated_data["billing_mode"]

        # Determine which components to switch:
        # - For offerings with builtin types (OpenStack, Rancher), switch only builtin + volume types
        # - For generic offerings (site agent), switch all components
        builtin_types = plugins.manager.get_component_types(offering.type)
        if builtin_types:
            target_types = set(builtin_types)
            if offering.type == OPENSTACK_TENANT_OFFERING:
                from waldur_openstack.utils import is_valid_volume_type_name

                for comp in offering.components.all():
                    if is_valid_volume_type_name(comp.type):
                        target_types.add(comp.type)
            target_components = offering.components.filter(type__in=target_types)
        else:
            target_components = offering.components.all()

        if mode == "prepaid":
            target_components.update(
                billing_type=BillingTypes.ONE_TIME,
                is_prepaid=True,
            )
            offering.plugin_options["is_resource_termination_date_required"] = True
            offering.save(update_fields=["plugin_options"])
            if offering.type == OPENSTACK_TENANT_OFFERING:
                self._restore_openstack_measured_units(target_components)
        elif mode == "usage":
            target_components.update(
                billing_type=BillingTypes.USAGE,
                is_prepaid=False,
            )
            # For OpenStack, clear the termination date requirement
            # that was set by prepaid mode. For generic offerings,
            # the provider may have set it independently — don't touch it.
            if offering.type == OPENSTACK_TENANT_OFFERING:
                offering.plugin_options.pop(
                    "is_resource_termination_date_required", None
                )
                offering.save(update_fields=["plugin_options"])
                self._set_openstack_usage_measured_units(target_components)
        else:
            target_components.update(
                billing_type=BillingTypes.LIMIT,
                is_prepaid=False,
                limit_period=LimitPeriods.MONTH,
            )
            if offering.type == OPENSTACK_TENANT_OFFERING:
                offering.plugin_options.pop(
                    "is_resource_termination_date_required", None
                )
                offering.save(update_fields=["plugin_options"])
                self._restore_openstack_measured_units(target_components)

        return Response(status=status.HTTP_200_OK)

    # Deterministic measured_unit mappings for OpenStack billing modes.
    # Usage mode tracks component-hours; limit/prepaid uses raw units.
    OPENSTACK_USAGE_UNITS = {"cores": "core-hours"}
    OPENSTACK_LIMIT_UNITS = {"cores": "cores"}
    # RAM, storage, and volume types all use GB / GB-hours.
    OPENSTACK_USAGE_DEFAULT_UNIT = "GB-hours"
    OPENSTACK_LIMIT_DEFAULT_UNIT = "GB"

    @classmethod
    def _set_openstack_usage_measured_units(cls, target_components):
        for comp in target_components:
            comp.measured_unit = cls.OPENSTACK_USAGE_UNITS.get(
                comp.type, cls.OPENSTACK_USAGE_DEFAULT_UNIT
            )
            comp.save(update_fields=["measured_unit"])

    @classmethod
    def _restore_openstack_measured_units(cls, target_components):
        for comp in target_components:
            comp.measured_unit = cls.OPENSTACK_LIMIT_UNITS.get(
                comp.type, cls.OPENSTACK_LIMIT_DEFAULT_UNIT
            )
            comp.save(update_fields=["measured_unit"])

    switch_billing_mode_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    switch_billing_mode_validators = update_validators

    @extend_schema(
        request=serializers.OfferingComponentSerializer,
        responses={status.HTTP_201_CREATED: None},
        summary="Create an offering component",
        description="Adds a new custom component to an offering.",
    )
    @action(detail=True, methods=["post"])
    def create_offering_component(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        component_data = request.data
        serializer: serializers.OfferingComponentSerializer = self.get_serializer(
            data=component_data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(offering=offering)
        return Response(status=status.HTTP_201_CREATED)

    create_offering_component_serializer_class = serializers.OfferingComponentSerializer
    create_offering_component_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    create_offering_component_validators = update_validators

    @extend_schema(
        summary="Synchronize offering service settings",
        description="Schedules a synchronization task to pull the latest data for the offering's service settings from the backend.",
        responses={202: None},
        request=None,
    )
    @action(detail=True, methods=["post"])
    def sync(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        if not offering.scope or not isinstance(
            offering.scope, structure_models.ServiceSettings
        ):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Offering does not have service settings.",
            )
        if not SupportedServices.has_service_type(offering.scope.type):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Plugin does not support this operation.",
            )
        if offering.scope.state not in (
            CoreStates.OK,
            CoreStates.ERRED,
        ):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Offering service should be in OK or ERRED state.",
            )
        transaction.on_commit(
            lambda: ServiceSettingsPullExecutor.execute(offering.scope)
        )
        return Response(
            status=status.HTTP_202_ACCEPTED, data="Offering sync has been scheduled."
        )

    sync_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        request=serializers.OfferingBackendMetadataSerializer,
        responses=None,
        summary="Set offering backend metadata",
        description="Updates the backend-specific metadata for an offering.",
    )
    @action(detail=True, methods=["POST"])
    def set_backend_metadata(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        offering_data = request.data
        serializer = self.get_serializer(offering, data=offering_data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            status=status.HTTP_200_OK,
            data="Offering backend metadata has been updated.",
        )

    set_backend_metadata_serializer_class = (
        serializers.OfferingBackendMetadataSerializer
    )

    set_backend_metadata_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        request=None,
        responses=structure_serializers.ProjectSerializer(many=True),
        filters=False,
        summary="List customer projects for an offering",
        description="Returns a paginated list of projects that have consumed resources of this offering.",
    )
    @action(detail=True, methods=["GET"])
    def list_customer_projects(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("project_id", flat=True)
        )
        projects = structure_models.Project.objects.filter(id__in=project_ids)
        projects = structure_serializers.ProjectSerializer.eager_load(projects, request)
        page = self.paginate_queryset(projects)

        serializer = structure_serializers.ProjectSerializer(
            instance=page,
            many=True,
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        responses=structure_serializers.UserSerializer(many=True),
        request=None,
        filters=False,
        summary="List customer users for an offering",
        description="Returns a paginated list of users who have access to resources of this offering.",
    )
    @action(detail=True, methods=["GET"])
    def list_customer_users(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("project_id", flat=True)
        )
        ctype = ContentType.objects.get_for_model(structure_models.Project)
        user_ids = get_user_ids(ctype, project_ids)

        # Filter users based on consent if enforcement is enabled globally and offering requires consent
        users = core_models.User.objects.filter(id__in=user_ids)

        if (
            config.ENFORCE_USER_CONSENT_FOR_OFFERINGS
            and offering.has_terms_of_service()
        ):
            users = users.filter(
                offering_consents__offering=offering,
                offering_consents__revocation_date__isnull=True,
            )

        page = self.paginate_queryset(users)
        serializer = structure_serializers.UserSerializer(
            instance=page,
            many=True,
            context={"request": request, "view": self},
        )
        result = self.get_paginated_response(serializer.data)
        # Flush buffered GDPR data access logs as a single bulk INSERT
        entries = getattr(self, "_data_access_log_entries", None)
        if entries:
            bulk_log_user_data_access(entries, request.user, request)
            del self._data_access_log_entries
        return result

    list_customer_projects_permissions = list_customer_users_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(
        responses={status.HTTP_200_OK: StatusSerializer},
        request=None,
        summary="Refresh offering user usernames",
        description="Triggers a refresh of usernames for all non-restricted users associated with this offering, based on the current username generation policy.",
    )
    @action(detail=True, methods=["post"])
    def refresh_offering_usernames(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        offering_users = models.OfferingUser.objects.filter(
            is_restricted=False,
            offering=offering,
        )

        for offering_user in offering_users:
            new_username = utils.generate_username(
                offering_user.user, offering_user.offering
            )
            if new_username != offering_user.username:
                logger.info("Updating %s username to %s", offering_user, new_username)
                offering_user.username = new_username
                # Call save() without update_fields to trigger state transition logic
                # This ensures state is updated from CREATION_REQUESTED to OK when username becomes available
                offering_user.save()

        return Response(
            status=status.HTTP_200_OK,
            data={"status": _("Offering user usernames have been changed.")},
        )

    refresh_offering_usernames_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    refresh_offering_usernames_validators = [
        core_validators.StateValidator(OfferingStates.ACTIVE),
        validate_offering_username_generation_policy,
    ]

    @extend_schema(
        summary="Synchronize offering resources",
        description="Requests connected site agents to run a full reconciliation "
        "of all resources belonging to this offering: recreate missing backend "
        "accounts, restore user associations and re-apply resource limits. "
        "Useful when the provider backend has lost state, e.g. a wiped SLURM database.",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def sync_resources(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        if offering.type != SITE_AGENT_OFFERING:
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Only site-agent based offerings support resource synchronization.",
            )
        if not utils.publish_offering_resources_sync_request(offering, request.user):
            return Response(
                status=status.HTTP_409_CONFLICT,
                data="No site agent is subscribed to resource synchronization "
                "events for this offering.",
            )
        return Response(
            status=status.HTTP_202_ACCEPTED,
            data={"status": _("Resource synchronization has been requested.")},
        )

    sync_resources_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    sync_resources_validators = [
        core_validators.StateValidator(OfferingStates.ACTIVE, OfferingStates.PAUSED),
    ]

    @extend_schema(
        summary="List customer service accounts for an offering",
        description="Returns a paginated list of customer-level service accounts for customers who have resources of this offering.",
        responses=serializers.CustomerServiceAccountSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def list_customer_service_accounts(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(
                offering=offering,
            )
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("project_id", flat=True)
            .distinct()
        )
        customer_ids = (
            structure_models.Project.objects.filter(
                id__in=project_ids,
            )
            .values_list("customer_id", flat=True)
            .distinct()
        )
        service_accounts = models.CustomerServiceAccount.objects.filter(
            customer_id__in=customer_ids,
        )
        page = self.paginate_queryset(service_accounts)
        serializer = serializers.CustomerServiceAccountSerializer(
            instance=page,
            many=True,
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="List project service accounts for an offering",
        description="Returns a paginated list of project-level service accounts for projects that have resources of this offering.",
        responses=serializers.ProjectServiceAccountSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def list_project_service_accounts(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(
                offering=offering,
            )
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("project_id", flat=True)
            .distinct()
        )
        service_accounts = models.ProjectServiceAccount.objects.filter(
            project_id__in=project_ids,
        )
        page = self.paginate_queryset(service_accounts)
        serializer = serializers.ProjectServiceAccountSerializer(
            instance=page,
            many=True,
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="List course accounts for an offering",
        description="Returns a paginated list of course accounts for projects that have resources of this offering.",
        responses=serializers.CourseAccountSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def list_course_accounts(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(
                offering=offering,
            )
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("project_id", flat=True)
            .distinct()
        )
        course_accounts = models.CourseAccount.objects.filter(
            project_id__in=project_ids,
        ).select_related("project__customer", "user")
        page = self.paginate_queryset(course_accounts)
        serializer = serializers.CourseAccountSerializer(
            instance=page,
            many=True,
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="Move an offering",
        description="Moves an offering to a different service provider. Requires staff permissions.",
        request=serializers.MoveOfferingSerializer,
        responses=serializers.PublicOfferingDetailsSerializer,
    )
    @action(detail=True, methods=["post"])
    def move_offering(self, request, uuid=None):
        offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target_customer = serializer.validated_data["customer"]
        preserve_permissions = serializer.validated_data["preserve_permissions"]

        utils.move_offering(
            offering, target_customer, request.user, preserve_permissions
        )
        serialized_offering = serializers.PublicOfferingDetailsSerializer(
            offering, context={"view": self, "request": request}
        )

        return Response(serialized_offering.data, status=status.HTTP_200_OK)

    move_offering_serializer_class = serializers.MoveOfferingSerializer
    move_offering_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Get Terms of Service consent statistics",
        description="Returns comprehensive Terms of Service consent statistics for this offering, including user counts, consent rates, and historical data.",
        responses={200: serializers.ToSConsentDashboardSerializer},
        filters=False,
    )
    @action(detail=True, methods=["get"])
    def tos_stats(self, request, uuid=None):
        """Return comprehensive ToS consent statistics for this offering."""

        offering = self.get_object()

        active_users_count = offering.get_quota_usage("active_users_count")
        total_users_count = offering.get_quota_usage("total_users_count")
        accepted_consents_count = offering.get_quota_usage("accepted_consents_count")
        revoked_consents_count = offering.get_quota_usage("revoked_consents_count")
        total_consents_count = offering.get_quota_usage("total_consents_count")

        active_users_percentage = 0.0
        if total_users_count > 0:
            active_users_percentage = round(
                (active_users_count / total_users_count) * 100, 2
            )

        revoked_consents_over_time = list(
            analytics_models.DailyQuotaHistory.objects.filter(
                scope=offering, name="revoked_consents_count"
            )
            .values("date", "usage")
            .order_by("date")
        )

        tos_version_adoption = list(
            models.UserOfferingConsent.objects.filter(
                offering=offering, revocation_date__isnull=True
            )
            .values("version")
            .annotate(users_count=Count("user", distinct=True))
            .order_by("-users_count")
        )
        active_users_over_time = list(
            analytics_models.DailyQuotaHistory.objects.filter(
                scope=offering, name="active_users_count"
            )
            .values("date", "usage")
            .order_by("date")
        )
        accepted_consents_over_time = list(
            analytics_models.DailyQuotaHistory.objects.filter(
                scope=offering, name="accepted_consents_count"
            )
            .values("date", "usage")
            .order_by("date")
        )
        dashboard_data = {
            "active_users_count": active_users_count,
            "total_users_count": total_users_count,
            "active_users_percentage": active_users_percentage,
            "accepted_consents_count": accepted_consents_count,
            "revoked_consents_count": revoked_consents_count,
            "total_consents_count": total_consents_count,
            "revoked_consents_over_time": [
                {"date": record["date"].isoformat(), "count": record["usage"]}
                for record in revoked_consents_over_time
            ],
            "tos_version_adoption": [
                {
                    "version": stat["version"] or "Unknown",
                    "users_count": stat["users_count"],
                }
                for stat in tos_version_adoption
            ],
            "active_users_over_time": [
                {"date": record["date"].isoformat(), "count": record["usage"]}
                for record in active_users_over_time
            ],
            "accepted_consents_over_time": [
                {"date": record["date"].isoformat(), "count": record["usage"]}
                for record in accepted_consents_over_time
            ],
        }

        serializer = serializers.ToSConsentDashboardSerializer(dashboard_data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Add a software catalog to an offering",
        description="Associates a software catalog with an offering and configures enabled CPU architectures.",
        request=serializers.OfferingSoftwareCatalogSerializer,
        responses={201: serializers.SoftwareCatalogUUIDSerializer},
    )
    @action(detail=True, methods=["post"])
    def add_software_catalog(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        data = request.data.copy()
        data["offering"] = offering.uuid.hex
        serializer = serializers.OfferingSoftwareCatalogSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"uuid": serializer.instance.uuid},
            status=status.HTTP_201_CREATED,
        )

    add_software_catalog_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    add_software_catalog_serializer_class = (
        serializers.OfferingSoftwareCatalogSerializer
    )

    @extend_schema(
        summary="Update software catalog configuration",
        description="Updates the configuration of a software catalog associated with an offering, such as enabled architectures or partition.",
        request=serializers.OfferingSoftwareCatalogUpdateSerializer,
        responses={200: serializers.OfferingSoftwareCatalogSerializer},
    )
    @action(
        detail=True,
        methods=["patch"],
    )
    def update_software_catalog(self, request, uuid=None):
        offering = self.get_object()
        offering_catalog_uuid = request.data.get("offering_catalog_uuid")
        try:
            offering_catalog = models.OfferingSoftwareCatalog.objects.get(
                uuid=offering_catalog_uuid, offering=offering
            )
        except models.OfferingSoftwareCatalog.DoesNotExist:
            return Response(
                {"error": "Software catalog association not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = serializers.OfferingSoftwareCatalogUpdateSerializer(
            offering_catalog, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated_catalog = serializer.save()

        response_serializer = serializers.OfferingSoftwareCatalogSerializer(
            updated_catalog, context=self.get_serializer_context()
        )
        return Response(response_serializer.data)

    update_software_catalog_serializer_class = (
        serializers.OfferingSoftwareCatalogUpdateSerializer
    )

    update_software_catalog_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Remove a software catalog from an offering",
        description="Disassociates a software catalog from an offering.",
        request=serializers.RemoveSoftwareCatalogSerializer,
        responses={204: None},
    )
    @action(
        detail=True,
        methods=["post"],
    )
    def remove_software_catalog(self, request, uuid=None):
        self.get_object()
        offering_catalog_uuid = request.data.get("offering_catalog_uuid")
        try:
            offering_catalog = models.OfferingSoftwareCatalog.objects.get(
                uuid=offering_catalog_uuid
            )
        except models.OfferingSoftwareCatalog.DoesNotExist:
            return Response(
                {"error": "Software catalog association not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        offering_catalog.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    remove_software_catalog_serializer_class = (
        serializers.RemoveSoftwareCatalogSerializer
    )

    remove_software_catalog_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Add a partition to an offering",
        description="Adds a new partition configuration to an offering.",
        request=serializers.OfferingPartitionSerializer,
        responses={201: serializers.OfferingPartitionSerializer},
    )
    @action(detail=True, methods=["post"])
    def add_partition(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        data = request.data.copy()
        data["offering"] = offering.uuid.hex
        serializer = serializers.OfferingPartitionSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"uuid": serializer.instance.uuid},
            status=status.HTTP_201_CREATED,
        )

    add_partition_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    add_partition_serializer_class = serializers.OfferingPartitionSerializer

    @extend_schema(
        summary="Update a partition of an offering",
        description="Updates the configuration of an existing partition associated with an offering.",
        request=serializers.OfferingPartitionUpdateSerializer,
        responses={200: serializers.OfferingPartitionSerializer},
    )
    @action(
        detail=True,
        methods=["patch"],
    )
    def update_partition(self, request, uuid=None):
        offering = self.get_object()
        partition_uuid = request.data.get("partition_uuid")
        try:
            partition = models.OfferingPartition.objects.get(
                uuid=partition_uuid, offering=offering
            )
        except models.OfferingPartition.DoesNotExist:
            return Response(
                {"error": "Partition not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = serializers.OfferingPartitionUpdateSerializer(
            partition, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated_partition = serializer.save()
        response_serializer = serializers.OfferingPartitionSerializer(
            updated_partition, context=self.get_serializer_context()
        )
        return Response(response_serializer.data)

    update_partition_serializer_class = serializers.OfferingPartitionUpdateSerializer

    update_partition_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Remove a partition from an offering",
        description="Removes a partition configuration from an offering.",
        request=serializers.RemovePartitionSerializer,
        responses={204: None},
    )
    @action(
        detail=True,
        methods=["post"],
    )
    def remove_partition(self, request, uuid=None):
        self.get_object()
        partition_uuid = request.data.get("partition_uuid")
        try:
            partition = models.OfferingPartition.objects.get(uuid=partition_uuid)
        except models.OfferingPartition.DoesNotExist:
            return Response(
                {"error": "Partition not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        partition.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    remove_partition_serializer_class = serializers.RemovePartitionSerializer

    remove_partition_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Add a QoS profile to an offering",
        description="Adds a new Quality of Service profile to an offering.",
        request=serializers.OfferingQoSSerializer,
        responses={201: serializers.OfferingQoSSerializer},
    )
    @action(detail=True, methods=["post"])
    def add_qos(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        data = request.data.copy()
        data["offering"] = offering.uuid.hex
        serializer = serializers.OfferingQoSSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"uuid": serializer.instance.uuid},
            status=status.HTTP_201_CREATED,
        )

    add_qos_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    add_qos_serializer_class = serializers.OfferingQoSSerializer

    @extend_schema(
        summary="Update a QoS profile of an offering",
        description="Updates an existing Quality of Service profile of an offering.",
        request=serializers.OfferingQoSUpdateSerializer,
        responses={200: serializers.OfferingQoSSerializer},
    )
    @action(detail=True, methods=["patch"])
    def update_qos(self, request, uuid=None):
        offering = self.get_object()
        qos_uuid = request.data.get("qos_uuid")
        try:
            qos = models.SlurmOfferingQoS.objects.get(uuid=qos_uuid, offering=offering)
        except models.SlurmOfferingQoS.DoesNotExist:
            return Response(
                {"error": "QoS profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = serializers.OfferingQoSUpdateSerializer(
            qos, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated_qos = serializer.save()
        response_serializer = serializers.OfferingQoSSerializer(
            updated_qos, context=self.get_serializer_context()
        )
        return Response(response_serializer.data)

    update_qos_serializer_class = serializers.OfferingQoSUpdateSerializer

    update_qos_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Remove a QoS profile from an offering",
        description="Removes a Quality of Service profile from an offering.",
        request=serializers.RemoveQoSSerializer,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def remove_qos(self, request, uuid=None):
        offering = self.get_object()
        qos_uuid = request.data.get("qos_uuid")
        try:
            qos = models.SlurmOfferingQoS.objects.get(uuid=qos_uuid, offering=offering)
        except models.SlurmOfferingQoS.DoesNotExist:
            return Response(
                {"error": "QoS profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        qos.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    remove_qos_serializer_class = serializers.RemoveQoSSerializer

    remove_qos_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Set the QoS allow-list of a partition",
        description="Replaces the QoS allow-list (SLURM AllowQos gate) of a "
        "partition. An empty list permits all of the offering's QoS.",
        request=serializers.SetPartitionQoSSerializer,
        responses={200: serializers.OfferingPartitionSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_partition_qos(self, request, uuid=None):
        offering = self.get_object()
        serializer = serializers.SetPartitionQoSSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        partition = serializer.validated_data["partition_uuid"]
        qos_options = serializer.validated_data["qos_options"]
        if partition.offering_id != offering.id:
            return Response(
                {"error": "Partition does not belong to this offering"},
                status=status.HTTP_404_NOT_FOUND,
            )
        for item in qos_options:
            if item["qos_uuid"].offering_id != offering.id:
                return Response(
                    {"error": "QoS profile does not belong to this offering"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        with transaction.atomic():
            partition.qos_options.all().delete()
            for item in qos_options:
                models.SlurmPartitionQoS.objects.create(
                    partition=partition,
                    qos=item["qos_uuid"],
                    is_default=item["is_default"],
                )
        response_serializer = serializers.OfferingPartitionSerializer(
            partition, context=self.get_serializer_context()
        )
        return Response(response_serializer.data)

    set_partition_qos_serializer_class = serializers.SetPartitionQoSSerializer

    set_partition_qos_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Check if backend_id is unique",
        description="Checks if the provided backend_id has been used in resources of this offering or all offerings of the same customer. Returns true if unique, false if already used.",
        request=serializers.CheckUniqueBackendIDSerializer,
        responses={200: serializers.CheckUniqueBackendIDResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def check_unique_backend_id(self, request, uuid=None):
        offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        backend_id = serializer.validated_data["backend_id"]
        check_all_offerings = serializer.validated_data.get(
            "check_all_offerings", False
        )
        use_offering_rules = serializer.validated_data.get("use_offering_rules", False)

        if use_offering_rules and backend_id:
            errors = []
            is_valid_format = None

            rules = offering.backend_id_rules or {}

            # Check format if rules are configured
            if rules.get("format", {}).get("regex"):
                try:
                    utils.validate_backend_id_format(backend_id, offering)
                    is_valid_format = True
                except rf_exceptions.ValidationError as e:
                    is_valid_format = False
                    errors.extend(self._extract_validation_errors(e))

            # Check uniqueness: use offering's scope if configured,
            # otherwise fall back to check_all_offerings toggle
            uniqueness_scope = rules.get("uniqueness", {}).get("scope")
            if uniqueness_scope:
                is_unique = True
                try:
                    utils.validate_backend_id_uniqueness(backend_id, offering)
                except rf_exceptions.ValidationError as e:
                    is_unique = False
                    errors.extend(self._extract_validation_errors(e))
            else:
                if check_all_offerings:
                    resources_query = models.Resource.objects.filter(
                        offering__customer=offering.customer,
                        backend_id=backend_id,
                    )
                else:
                    resources_query = models.Resource.objects.filter(
                        offering=offering, backend_id=backend_id
                    )
                is_unique = not resources_query.exists()

            return Response(
                {
                    "is_unique": is_unique,
                    "is_valid_format": is_valid_format,
                    "errors": errors,
                }
            )

        # Original behavior (use_offering_rules=False)
        if check_all_offerings:
            resources_query = models.Resource.objects.filter(
                offering__customer=offering.customer, backend_id=backend_id
            )
        else:
            resources_query = models.Resource.objects.filter(
                offering=offering, backend_id=backend_id
            )

        # Include all resources regardless of state (including terminated)
        is_unique = not resources_query.exists()

        return Response({"is_unique": is_unique})

    check_unique_backend_id_serializer_class = (
        serializers.CheckUniqueBackendIDSerializer
    )

    check_unique_backend_id_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Update offering backend_id rules",
        description="Configure validation rules for resource backend_id: format regex and uniqueness scope.",
        request=serializers.OfferingBackendIdRulesUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_backend_id_rules(self, request, uuid=None):
        return self._update_action(request)

    update_backend_id_rules_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_backend_id_rules_validators = update_validators
    update_backend_id_rules_serializer_class = (
        serializers.OfferingBackendIdRulesUpdateSerializer
    )

    @extend_schema(
        summary="Export offering data",
        description="Exports an offering and all its connected parts to YAML format. Allows configuration of which components to include in the export.",
        request=serializers.OfferingExportParametersSerializer,
        responses=serializers.OfferingExportResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def export_offering(self, request, uuid=None):
        """Export offering data with configurable parameters."""

        offering: models.Offering = self.get_object()
        serializer = serializers.OfferingExportParametersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        # Build export data based on parameters
        export_data = self._build_offering_export_data(offering, params)
        exported_components = self._get_exported_components_list(offering, params)

        response_data = {
            "offering_uuid": offering.uuid,
            "offering_name": offering.name,
            "export_data": export_data,
            "exported_components": exported_components,
            "export_timestamp": timezone.now(),
        }

        return response.Response(
            serializers.OfferingExportResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )

    def _build_offering_export_data(self, offering, params):
        """Build export data based on configuration parameters."""
        # Start with core offering data
        export_data = {
            "offering": {
                "name": offering.name,
                "description": offering.description,
                "full_description": offering.full_description,
                "vendor_details": offering.vendor_details,
                "getting_started": offering.getting_started,
                "integration_guide": offering.integration_guide,
                "type": offering.type,
                "shared": offering.shared,
                "billable": offering.billable,
                "state": offering.get_state_display(),
                "category_name": offering.category.title if offering.category else None,
                "country": offering.country,
                "latitude": float(offering.latitude) if offering.latitude else None,
                "longitude": float(offering.longitude) if offering.longitude else None,
                "access_url": offering.access_url,
                "paused_reason": offering.paused_reason,
            }
        }

        # Conditionally add offering fields based on parameters
        if params.get("include_attributes", True):
            export_data["offering"]["attributes"] = offering.attributes

        if params.get("include_options", True):
            export_data["offering"]["options"] = offering.options

        if params.get("include_resource_options", True):
            export_data["resource_options"] = offering.resource_options

        if params.get("include_plugin_options", True):
            export_data["plugin_options"] = offering.plugin_options

        if params.get("include_secret_options", False):
            export_data["secret_options"] = offering.secret_options

        # Add related entities based on parameters
        if params.get("include_components", True):
            export_data["components"] = self._export_offering_components(offering)

        if params.get("include_plans", True):
            export_data["plans"] = self._export_offering_plans(offering)

        if params.get("include_screenshots", True):
            export_data["screenshots"] = self._export_offering_screenshots(offering)

        if params.get("include_files", True):
            export_data["files"] = self._export_offering_files(offering)

        if params.get("include_endpoints", True):
            export_data["endpoints"] = self._export_offering_endpoints(offering)

        if params.get("include_organization_groups", True):
            export_data["organization_groups"] = self._export_organization_groups(
                offering
            )

        if params.get("include_terms_of_service", True):
            export_data["terms_of_service"] = self._export_terms_of_service(offering)

        return export_data

    def _export_offering_components(self, offering):
        """Export offering components."""
        components = []
        for component in offering.components.all():
            components.append(
                {
                    "type": component.type,
                    "name": component.name,
                    "description": component.description,
                    "billing_type": component.billing_type,
                    "measured_unit": component.measured_unit,
                    "unit_factor": float(component.unit_factor)
                    if component.unit_factor
                    else None,
                    "limit_period": component.limit_period,
                    "limit_amount": component.limit_amount,
                    "article_code": component.article_code,
                    "backend_id": component.backend_id,
                }
            )
        return components

    def _export_offering_plans(self, offering):
        """Export offering plans."""
        plans = []
        for plan in offering.plans.all():
            plan_data = {
                "name": plan.name,
                "description": plan.description,
                "unit_price": float(plan.unit_price) if plan.unit_price else 0,
                "unit": plan.unit,
                "archived": plan.archived,
                "max_amount": plan.max_amount,
                "article_code": plan.article_code,
                "backend_id": plan.backend_id,
                "components": [],
            }

            # Add plan components
            for plan_component in plan.components.all():
                plan_data["components"].append(
                    {
                        "component_type": plan_component.component.type
                        if plan_component.component
                        else None,
                        "amount": plan_component.amount,
                        "price": float(plan_component.price)
                        if plan_component.price
                        else 0,
                        "future_price": float(plan_component.future_price)
                        if plan_component.future_price
                        else None,
                    }
                )

            plans.append(plan_data)
        return plans

    def _export_offering_screenshots(self, offering):
        """Export offering screenshots."""
        screenshots = []
        for screenshot in offering.screenshots.all():
            screenshot_data = {
                "name": screenshot.name,
                "description": screenshot.description,
            }

            # Include base64 encoded image content
            if screenshot.image:
                try:
                    with screenshot.image.open("rb") as image_file:
                        image_content = image_file.read()
                        screenshot_data["image_content"] = base64.b64encode(
                            image_content
                        ).decode("utf-8")
                        screenshot_data["image_filename"] = screenshot.image.name.split(
                            "/"
                        )[-1]
                        # Try to determine content type from file extension
                        if screenshot.image.name.lower().endswith(".png"):
                            screenshot_data["content_type"] = "image/png"
                        elif screenshot.image.name.lower().endswith((".jpg", ".jpeg")):
                            screenshot_data["content_type"] = "image/jpeg"
                        elif screenshot.image.name.lower().endswith(".gif"):
                            screenshot_data["content_type"] = "image/gif"
                        elif screenshot.image.name.lower().endswith(".svg"):
                            screenshot_data["content_type"] = "image/svg+xml"
                        elif screenshot.image.name.lower().endswith(".webp"):
                            screenshot_data["content_type"] = "image/webp"
                        else:
                            screenshot_data["content_type"] = "image/png"  # Default
                except Exception:
                    # If file access fails, fall back to URL
                    screenshot_data["image_url"] = screenshot.image.url

            screenshots.append(screenshot_data)
        return screenshots

    def _export_offering_files(self, offering):
        """Export offering files."""
        files = []
        for file_obj in offering.files.all():
            file_data = {
                "name": file_obj.name,
            }

            # Include base64 encoded file content
            if file_obj.file:
                try:
                    with file_obj.file.open("rb") as file_content:
                        content = file_content.read()
                        file_data["file_content"] = base64.b64encode(content).decode(
                            "utf-8"
                        )
                        file_data["filename"] = file_obj.file.name.split("/")[-1]
                        # Try to determine content type from file extension
                        filename_lower = file_obj.file.name.lower()
                        if filename_lower.endswith(".pdf"):
                            file_data["content_type"] = "application/pdf"
                        elif filename_lower.endswith((".txt", ".md")):
                            file_data["content_type"] = "text/plain"
                        elif filename_lower.endswith(".json"):
                            file_data["content_type"] = "application/json"
                        elif filename_lower.endswith((".yml", ".yaml")):
                            file_data["content_type"] = "application/x-yaml"
                        else:
                            file_data["content_type"] = (
                                "application/octet-stream"  # Default binary
                            )
                except Exception:
                    # If file access fails, fall back to URL
                    file_data["file_url"] = file_obj.file.url

            files.append(file_data)
        return files

    def _export_offering_endpoints(self, offering):
        """Export offering access endpoints."""
        endpoints = []
        for endpoint in offering.endpoints.all():
            endpoints.append(
                {
                    "name": endpoint.name,
                    "url": endpoint.url,
                }
            )
        return endpoints

    def _export_organization_groups(self, offering):
        """Export organization groups associations."""
        groups = []
        for group in offering.organization_groups.all():
            groups.append(
                {
                    "name": group.name,
                    "parent_name": group.parent.name if group.parent else None,
                }
            )
        return groups

    def _export_terms_of_service(self, offering):
        """Export terms of service configurations."""
        terms_configs = []
        for terms_config in offering.terms_of_service_configs.all():
            terms_configs.append(
                {
                    "terms_of_service": terms_config.terms_of_service,
                    "terms_of_service_link": terms_config.terms_of_service_link,
                    "version": terms_config.version,
                    "is_active": terms_config.is_active,
                    "requires_reconsent": terms_config.requires_reconsent,
                    "grace_period_days": terms_config.grace_period_days,
                }
            )
        return terms_configs

    def _get_exported_components_list(self, offering, params):
        """Get list of exported component names."""
        exported_components = []

        if params.get("include_components", True):
            exported_components.extend([c.type for c in offering.components.all()])

        if params.get("include_plans", True):
            exported_components.append("plans")

        if params.get("include_screenshots", True):
            exported_components.append("screenshots")

        if params.get("include_files", True):
            exported_components.append("files")

        if params.get("include_endpoints", True):
            exported_components.append("endpoints")

        if params.get("include_organization_groups", True):
            exported_components.append("organization_groups")

        if params.get("include_terms_of_service", True):
            exported_components.append("terms_of_service")

        return exported_components

    export_offering_serializer_class = serializers.OfferingExportParametersSerializer
    export_offering_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    @extend_schema(
        summary="Import offering data",
        description="Imports an offering and all its connected parts from YAML format. Allows configuration of which components to import and how to handle conflicts. Imported offerings are always created in DRAFT state for security.",
        request=serializers.OfferingImportParametersSerializer,
        responses=serializers.OfferingImportResponseSerializer,
    )
    def check_import_offering_permissions(request, view, obj=None):
        """Check if user has permission to create offerings via import."""
        serializer = serializers.OfferingImportParametersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        # Determine target customer
        customer = params.get("customer")
        if not customer:
            try:
                customer = request.user.customer_permissions.first().customer
            except (AttributeError, IndexError):
                raise rf_exceptions.ValidationError(
                    "No target customer specified or found"
                )

        # Check if user has permission to create offerings for this customer
        if not has_permission(request, PermissionEnum.CREATE_OFFERING, customer):
            raise rf_exceptions.PermissionDenied(
                "You do not have permission to create offerings for this customer."
            )

        # Import offerings are always created in DRAFT state for security

    import_offering_permissions = [check_import_offering_permissions]

    @extend_schema(
        summary="Import offering data",
        description="Imports an offering and all its connected parts from YAML format. Allows configuration of which components to import and how to handle conflicts. Imported offerings are always created in DRAFT state for security.",
        request=serializers.OfferingImportParametersSerializer,
        responses=serializers.OfferingImportResponseSerializer,
    )
    @action(detail=False, methods=["post"])
    def import_offering(self, request):
        """Import offering data with configurable parameters."""
        serializer = serializers.OfferingImportParametersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        # Parse YAML data
        try:
            if isinstance(params["offering_data"], str):
                import_data = yaml.safe_load(params["offering_data"])
            else:
                import_data = params["offering_data"]
        except yaml.YAMLError as e:
            raise rf_exceptions.ValidationError(f"Invalid YAML data: {str(e)}")

        warnings = []

        with transaction.atomic():
            # Create or update offering
            offering, created, offering_warnings = self._import_offering_data(
                import_data, params, request.user
            )
            warnings.extend(offering_warnings)

            # Import related components based on parameters
            imported_components = []

            if params.get("import_components", True) and "components" in import_data:
                component_warnings = self._import_offering_components(
                    offering, import_data["components"]
                )
                warnings.extend(component_warnings)
                imported_components.extend(
                    [c["type"] for c in import_data["components"]]
                )

            if params.get("import_plans", True) and "plans" in import_data:
                plan_warnings = self._import_offering_plans(
                    offering, import_data["plans"]
                )
                warnings.extend(plan_warnings)
                imported_components.append("plans")

            if params.get("import_screenshots", True) and "screenshots" in import_data:
                screenshot_warnings = self._import_offering_screenshots(
                    offering, import_data["screenshots"]
                )
                warnings.extend(screenshot_warnings)
                imported_components.append("screenshots")

            if params.get("import_files", True) and "files" in import_data:
                file_warnings = self._import_offering_files(
                    offering, import_data["files"]
                )
                warnings.extend(file_warnings)
                imported_components.append("files")

            if params.get("import_endpoints", True) and "endpoints" in import_data:
                endpoint_warnings = self._import_offering_endpoints(
                    offering, import_data["endpoints"]
                )
                warnings.extend(endpoint_warnings)
                imported_components.append("endpoints")

            if (
                params.get("import_organization_groups", True)
                and "organization_groups" in import_data
            ):
                group_warnings = self._import_organization_groups(
                    offering, import_data["organization_groups"]
                )
                warnings.extend(group_warnings)
                imported_components.append("organization_groups")

            if (
                params.get("import_terms_of_service", True)
                and "terms_of_service" in import_data
            ):
                terms_warnings = self._import_terms_of_service(
                    offering, import_data["terms_of_service"]
                )
                warnings.extend(terms_warnings)
                imported_components.append("terms_of_service")

        response_data = {
            "imported_offering_uuid": offering.uuid,
            "imported_offering_name": offering.name,
            "imported_components": imported_components,
            "warnings": warnings,
            "import_timestamp": timezone.now(),
        }

        return response.Response(
            serializers.OfferingImportResponseSerializer(response_data).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def _import_offering_data(self, import_data, params, user):
        """Import core offering data."""
        offering_data = import_data.get("offering", {})
        warnings = []
        created = False

        # Determine target customer
        customer = params.get("customer")
        if not customer:
            try:
                customer = user.customer_permissions.first().customer
            except (AttributeError, IndexError):
                raise rf_exceptions.ValidationError(
                    "No target customer specified or found"
                )

        # Determine target category - prioritize explicit parameter over export data
        category = params.get("category")  # This gets the explicitly provided category

        if not category:
            # If no explicit category provided, try to use category from export data
            category_name = offering_data.get("category_name")
            if category_name:
                try:
                    category = models.Category.objects.get(title=category_name)
                except models.Category.DoesNotExist:
                    warnings.append(
                        f"Category with name '{category_name}' not found, using first available category"
                    )
                    category = models.Category.objects.first()
                except models.Category.MultipleObjectsReturned:
                    raise rf_exceptions.ValidationError(
                        f"Multiple categories match title '{category_name}'. Resolve "
                        "the duplicate category titles, or specify the target category "
                        "explicitly via the 'category' parameter."
                    )

        # If we still don't have a category, try to get the first available one
        if not category:
            category = models.Category.objects.first()
            if category:
                warnings.append(
                    "No category specified or found in export data, using first available category"
                )

        if not category:
            raise rf_exceptions.ValidationError("No target category specified or found")

        # Check if offering exists
        offering_name = offering_data.get("name")
        existing_offering = None

        if offering_name:
            existing_offering = models.Offering.objects.filter(
                name=offering_name, customer=customer
            ).first()

        if existing_offering:
            if not params.get("overwrite_existing", False):
                raise rf_exceptions.ValidationError(
                    f"Offering '{offering_name}' already exists. Set overwrite_existing=True to update it."
                )
            offering = existing_offering
        else:
            offering = models.Offering(customer=customer, category=category)
            created = True

        # Update offering fields
        offering.name = offering_data.get("name", offering.name)
        offering.description = offering_data.get("description", offering.description)
        offering.full_description = offering_data.get(
            "full_description", offering.full_description
        )
        offering.vendor_details = offering_data.get(
            "vendor_details", offering.vendor_details
        )
        offering.getting_started = offering_data.get(
            "getting_started", offering.getting_started
        )
        offering.integration_guide = offering_data.get(
            "integration_guide", offering.integration_guide
        )
        offering.type = offering_data.get("type", offering.type)
        offering.shared = offering_data.get("shared", offering.shared)
        offering.billable = offering_data.get("billable", offering.billable)
        offering.country = offering_data.get("country", offering.country)
        offering.latitude = offering_data.get("latitude", offering.latitude)
        offering.longitude = offering_data.get("longitude", offering.longitude)
        offering.access_url = offering_data.get("access_url", offering.access_url)
        offering.paused_reason = offering_data.get(
            "paused_reason", offering.paused_reason
        )

        # Always set imported offerings to DRAFT state for security
        # Users must use proper state transition actions (activate, pause, etc.) after import
        offering.state = models.Offering.States.DRAFT

        # Set optional project
        project = params.get("project")
        if project:
            offering.project = project

        # Update JSON fields based on import parameters
        # Note: plugin_options, secret_options, and resource_options are exported
        # at the import_data level (sibling to "offering"), not inside offering_data
        if (
            params.get("import_plugin_options", True)
            and "plugin_options" in import_data
        ):
            offering.plugin_options = import_data["plugin_options"]

        if (
            params.get("import_secret_options", False)
            and "secret_options" in import_data
        ):
            offering.secret_options = import_data["secret_options"]

        if "attributes" in offering_data:
            offering.attributes = offering_data["attributes"]

        if "options" in offering_data:
            offering.options = offering_data["options"]

        if "resource_options" in import_data:
            offering.resource_options = import_data["resource_options"]

        offering.save()
        return offering, created, warnings

    def _import_offering_components(self, offering, components_data):
        """Import offering components."""
        warnings = []

        for component_data in components_data:
            component, created = models.OfferingComponent.objects.get_or_create(
                offering=offering,
                type=component_data["type"],
                defaults={
                    "name": component_data.get("name", ""),
                    "description": component_data.get("description", ""),
                    "billing_type": component_data.get("billing_type", ""),
                    "measured_unit": component_data.get("measured_unit", ""),
                    "unit_factor": component_data.get("unit_factor", 1),
                    "limit_period": component_data.get("limit_period")
                    or LimitPeriods.MONTH,
                    "limit_amount": component_data.get("limit_amount"),
                    "article_code": component_data.get("article_code", ""),
                    "backend_id": component_data.get("backend_id", ""),
                },
            )

            if not created:
                # Update existing component
                component.name = component_data.get("name", component.name)
                component.description = component_data.get(
                    "description", component.description
                )
                component.billing_type = component_data.get(
                    "billing_type", component.billing_type
                )
                component.measured_unit = component_data.get(
                    "measured_unit", component.measured_unit
                )
                component.unit_factor = component_data.get(
                    "unit_factor", component.unit_factor
                )
                component.limit_period = component_data.get(
                    "limit_period", component.limit_period
                )
                component.limit_amount = component_data.get(
                    "limit_amount", component.limit_amount
                )
                component.article_code = component_data.get(
                    "article_code", component.article_code
                )
                component.backend_id = component_data.get(
                    "backend_id", component.backend_id
                )
                component.save()

        return warnings

    def _import_offering_plans(self, offering, plans_data):
        """Import offering plans."""
        warnings = []

        for plan_data in plans_data:
            plan, created = models.Plan.objects.get_or_create(
                offering=offering,
                name=plan_data["name"],
                defaults={
                    "description": plan_data.get("description", ""),
                    "unit_price": plan_data.get("unit_price", 0),
                    "unit": plan_data.get("unit", ""),
                    "archived": plan_data.get("archived", False),
                    "max_amount": plan_data.get("max_amount"),
                    "article_code": plan_data.get("article_code", ""),
                    "backend_id": plan_data.get("backend_id", ""),
                },
            )

            if not created:
                # Update existing plan
                plan.description = plan_data.get("description", plan.description)
                plan.unit_price = plan_data.get("unit_price", plan.unit_price)
                plan.unit = plan_data.get("unit", plan.unit)
                plan.archived = plan_data.get("archived", plan.archived)
                plan.max_amount = plan_data.get("max_amount", plan.max_amount)
                plan.article_code = plan_data.get("article_code", plan.article_code)
                plan.backend_id = plan_data.get("backend_id", plan.backend_id)
                plan.save()

            # Import plan components
            for component_data in plan_data.get("components", []):
                component_type = component_data.get("component_type")
                if component_type:
                    try:
                        component = offering.components.get(type=component_type)
                        plan_component, created = (
                            models.PlanComponent.objects.get_or_create(
                                plan=plan,
                                component=component,
                                defaults={
                                    "amount": component_data.get("amount", 0),
                                    "price": component_data.get("price", 0),
                                    "future_price": component_data.get("future_price"),
                                },
                            )
                        )

                        if not created:
                            plan_component.amount = component_data.get(
                                "amount", plan_component.amount
                            )
                            plan_component.price = component_data.get(
                                "price", plan_component.price
                            )
                            plan_component.future_price = component_data.get(
                                "future_price", plan_component.future_price
                            )
                            plan_component.save()

                    except models.OfferingComponent.DoesNotExist:
                        warnings.append(
                            f"Component type '{component_type}' not found for plan '{plan.name}'"
                        )

        return warnings

    def _import_offering_screenshots(self, offering, screenshots_data):
        """Import offering screenshots."""
        warnings = []

        for screenshot_data in screenshots_data:
            screenshot_name = screenshot_data.get("name", "")

            # Check if we have base64 content to import
            if "image_content" in screenshot_data:
                try:
                    # Decode base64 content
                    image_content = base64.b64decode(screenshot_data["image_content"])
                    filename = screenshot_data.get(
                        "image_filename", f"{screenshot_name}.png"
                    )
                    screenshot_data.get("content_type", "image/png")

                    # Create screenshot with content
                    screenshot, created = models.Screenshot.objects.get_or_create(
                        offering=offering,
                        name=screenshot_name,
                        defaults={
                            "description": screenshot_data.get("description", ""),
                        },
                    )

                    # Save the image content
                    screenshot.image.save(
                        filename, ContentFile(image_content), save=True
                    )

                    if not created:
                        screenshot.description = screenshot_data.get(
                            "description", screenshot.description
                        )
                        screenshot.save()

                except Exception as e:
                    warnings.append(
                        f"Failed to import screenshot '{screenshot_name}': {str(e)}"
                    )
            else:
                # No base64 content, just create metadata entry
                screenshot, created = models.Screenshot.objects.get_or_create(
                    offering=offering,
                    name=screenshot_name,
                    defaults={
                        "description": screenshot_data.get("description", ""),
                    },
                )
                if not created:
                    screenshot.description = screenshot_data.get(
                        "description", screenshot.description
                    )
                    screenshot.save()

                if "image_url" in screenshot_data:
                    warnings.append(
                        f"Screenshot '{screenshot_name}' imported without content (URL reference only)"
                    )

        return warnings

    def _import_offering_files(self, offering, files_data):
        """Import offering files."""
        warnings = []

        for file_data in files_data:
            file_name = file_data.get("name", "")

            # Check if we have base64 content to import
            if "file_content" in file_data:
                try:
                    # Decode base64 content
                    content = base64.b64decode(file_data["file_content"])
                    filename = file_data.get("filename", file_name)
                    file_data.get("content_type", "application/octet-stream")

                    # Create or update file with content
                    offering_file, created = models.OfferingFile.objects.get_or_create(
                        offering=offering, name=file_name, defaults={}
                    )

                    # Save the file content
                    offering_file.file.save(filename, ContentFile(content), save=True)

                except Exception as e:
                    warnings.append(f"Failed to import file '{file_name}': {str(e)}")
            else:
                # No base64 content, just create metadata entry
                offering_file, created = models.OfferingFile.objects.get_or_create(
                    offering=offering, name=file_name, defaults={}
                )

                if "file_url" in file_data:
                    warnings.append(
                        f"File '{file_name}' imported without content (URL reference only)"
                    )

        return warnings

    def _import_offering_endpoints(self, offering, endpoints_data):
        """Import offering access endpoints."""
        warnings = []

        for endpoint_data in endpoints_data:
            endpoint, created = models.OfferingAccessEndpoint.objects.get_or_create(
                offering=offering,
                name=endpoint_data["name"],
                defaults={
                    "url": endpoint_data.get("url", ""),
                },
            )

            if not created:
                endpoint.url = endpoint_data.get("url", endpoint.url)
                endpoint.save()

        return warnings

    def _import_organization_groups(self, offering, groups_data):
        """Import organization groups associations."""
        warnings = []

        for group_data in groups_data:
            group_name = group_data.get("name")
            if group_name:
                try:
                    group = structure_models.OrganizationGroup.objects.get(
                        name=group_name
                    )
                    offering.organization_groups.add(group)
                except structure_models.OrganizationGroup.DoesNotExist:
                    warnings.append(
                        f"Organization group with name '{group_name}' not found"
                    )

        return warnings

    def _import_terms_of_service(self, offering, terms_data):
        """Import terms of service configurations."""
        warnings = []

        for terms_config_data in terms_data:
            # Deactivate existing active terms if we're importing a new active one
            if terms_config_data.get("is_active", False):
                offering.terms_of_service_configs.filter(is_active=True).update(
                    is_active=False
                )

            models.OfferingTermsOfService.objects.create(
                offering=offering,
                terms_of_service=terms_config_data.get("terms_of_service", ""),
                terms_of_service_link=terms_config_data.get("terms_of_service_link", "")
                or "",
                version=terms_config_data.get("version", ""),
                is_active=terms_config_data.get("is_active", False),
                requires_reconsent=terms_config_data.get("requires_reconsent", False),
                grace_period_days=terms_config_data.get("grace_period_days", 60),
            )

        return warnings

    import_offering_serializer_class = serializers.OfferingImportParametersSerializer

    # User attribute config actions
    @extend_schema(
        summary="Get user attribute config",
        description="Returns the user attribute configuration for this offering, "
        "which determines which user attributes are exposed to the service provider.",
        responses={200: serializers.OfferingUserAttributeConfigSerializer},
    )
    @action(detail=True, methods=["get"], url_path="user-attribute-config")
    def user_attribute_config(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        try:
            config = offering.user_attribute_config
        except models.OfferingUserAttributeConfig.DoesNotExist:
            # Return default config (unsaved instance with model defaults)
            config = models.OfferingUserAttributeConfig(offering=offering)

        serializer = serializers.OfferingUserAttributeConfigSerializer(
            config, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    user_attribute_config_permissions = [structure_permissions.is_owner]

    @extend_schema(
        summary="Update user attribute config",
        description="Creates or updates the user attribute configuration for this offering. "
        "This determines which user attributes are shared with the service provider.",
        request=serializers.OfferingUserAttributeConfigSerializer,
        responses={200: serializers.OfferingUserAttributeConfigSerializer},
    )
    @action(
        detail=True,
        methods=["post", "put", "patch"],
        url_path="update-user-attribute-config",
    )
    def update_user_attribute_config(self, request, uuid=None):
        offering: models.Offering = self.get_object()

        try:
            config = offering.user_attribute_config
            serializer = serializers.OfferingUserAttributeConfigSerializer(
                config,
                data=request.data,
                partial=request.method == "PATCH",
                context=self.get_serializer_context(),
            )
        except models.OfferingUserAttributeConfig.DoesNotExist:
            # Create new config
            data = {**request.data, "offering": str(offering.uuid)}
            serializer = serializers.OfferingUserAttributeConfigSerializer(
                data=data,
                context=self.get_serializer_context(),
            )

        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    update_user_attribute_config_permissions = [structure_permissions.is_owner]
    update_user_attribute_config_validators = update_validators

    @extend_schema(
        request=None,
        summary="Delete user attribute config",
        description="Deletes the user attribute configuration for this offering. "
        "The offering will fall back to system defaults.",
        responses={204: None},
    )
    @action(detail=True, methods=["delete"], url_path="delete-user-attribute-config")
    def delete_user_attribute_config(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        try:
            offering.user_attribute_config.delete()
        except models.OfferingUserAttributeConfig.DoesNotExist:
            pass  # Already deleted or never existed
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_user_attribute_config_permissions = [structure_permissions.is_owner]
    delete_user_attribute_config_validators = update_validators


@extend_schema_view(
    list=extend_schema(
        summary="List public offerings",
        description="Returns a paginated list of public offerings. The list is filtered to show only offerings that are active or paused and available for ordering by the current user. If anonymous access is enabled, it shows shared offerings available to unauthenticated users.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a public offering",
        description="Returns the details of a specific public offering. Access is granted if the offering is available for ordering by the current user or if anonymous access is enabled.",
    ),
)
class PublicOfferingViewSet(rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.Offering.objects.filter()
    lookup_field = "uuid"
    serializer_class = serializers.PublicOfferingDetailsSerializer
    filterset_class = filters.OfferingFilter
    permission_classes = []

    def get_queryset(self):
        user = self.request.user
        return proposal_managers.annotate_offerings_open_for_proposals(
            self.queryset.filter_by_ordering_availability_for_user(user).select_related(
                # get_filtered_plans reads offering.parent; the details serializer
                # walks customer and category.
                "parent",
                "customer",
                "category",
            )
        )

    @extend_schema(
        summary="List plans for an offering",
        description="Returns a list of plans available for a specific offering. The plans are filtered based on the current user's permissions and organization group memberships.",
        responses=serializers.BasePublicPlanSerializer(many=True),
        filters=False,
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def plans(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        return Response(
            serializers.PublicOfferingDetailsSerializer(
                context=self.get_serializer_context()
            ).get_filtered_plans(offering),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Retrieve a specific plan for an offering",
        description="Returns the details of a specific plan if it is available to the current user for the given offering.",
        responses=serializers.BasePublicPlanSerializer,
    )
    def plan_detail(self, request, uuid=None, plan_uuid=None):
        offering: models.Offering = self.get_object()

        try:
            plan = utils.get_plans_available_for_user(
                offering=offering,
                user=request.user,
            ).get(uuid=plan_uuid)
            serializer = serializers.BasePublicPlanSerializer(
                plan, context=self.get_serializer_context()
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        except models.Plan.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)


@extend_schema_view(
    list=extend_schema(
        summary="List Datacite referrals for offerings",
        description="Returns a paginated list of Datacite referrals associated with marketplace offerings. Referrals represent relationships between an offering (identified by a DOI) and other research outputs, such as publications or datasets. The list must be filtered by the offering's scope.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a specific Datacite referral",
        description="Returns the details of a single Datacite referral record, identified by its UUID. Details include the related identifier (PID), the type of relationship, and metadata about the related work.",
    ),
)
class OfferingReferralsViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    queryset = pid_models.DataciteReferral.objects.all()
    serializer_class = serializers.OfferingReferralSerializer
    lookup_field = "uuid"
    filter_backends = (
        filters.OfferingReferralScopeFilterBackend,
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.OfferingReferralFilter


class ConsumerResourceProjectViewSet(UserRoleMixin, core_views.ActionsViewSet):
    """
    Manage sub-projects within a resource (consumer perspective).

    Resource projects represent sub-entities (e.g. Rancher projects within a cluster).
    Enabled per-offering via the ``enable_resource_projects`` plugin option.

    Filter by resource using ``?resource_uuid={uuid}`` query parameter.
    """

    queryset = models.ResourceProject.available_objects.all().select_related("resource")
    serializer_class = serializers.ResourceProjectSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ResourceProjectFilter

    def _include_removed(self):
        return self.request.query_params.get("include_removed", "").lower() in (
            "true",
            "1",
            "yes",
        )

    def get_queryset(self):
        # The recover action operates on soft-deleted rows by definition, and
        # `?include_removed=true` lets a "show removed" UI list them. In both
        # cases we switch from available_objects to the all-rows manager.
        if self.action == "recover" or self._include_removed():
            qs = models.ResourceProject.objects.all().select_related("resource")
        else:
            qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        resources = models.Resource.objects.all().filter_for_user(user)
        # Defense-in-depth: union direct ResourceProject role-holders so an
        # invitee with only a ResourceProject role sees their project even
        # when Resource.filter_for_user logic changes upstream.
        return qs.filter(
            Q(resource__in=resources) | Q(id__in=get_user_resource_project_ids(user))
        ).distinct()

    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE,
            ["resource.project", "resource.project.customer"],
        )
    ]

    def perform_create(self, serializer):
        resource = serializer.validated_data["resource"]
        if not has_permission(
            self.request,
            PermissionEnum.UPDATE_RESOURCE,
            resource.project,
        ) and not has_permission(
            self.request,
            PermissionEnum.UPDATE_RESOURCE,
            resource.project.customer,
        ):
            raise PermissionDenied()
        resource_project = serializer.save(created_by=self.request.user)
        log.log_resource_project_created(resource_project)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="force",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Staff-only: when true, hard-delete the resource project "
                    "instead of soft-deleting it."
                ),
            ),
        ],
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance: models.ResourceProject):
        force = self.request.query_params.get("force", "").lower() in (
            "true",
            "1",
            "yes",
        )
        if force:
            if not self.request.user.is_staff:
                raise PermissionDenied(
                    "Force-delete (?force=true) requires staff permissions."
                )
            log.log_resource_project_removed(instance)
            instance.delete(soft=False)
        else:
            instance.delete(soft=True, terminated_by=self.request.user)
            log.log_resource_project_removed(instance)

    @extend_schema(
        request=serializers.ResourceProjectRecoverySerializer,
        responses=serializers.ResourceProjectSerializer,
        summary="Recover a soft-deleted resource project",
        description=(
            "Flips is_removed back to False on a previously soft-deleted "
            "resource project. Optionally restores the team members captured "
            "at soft-delete time, or sends them new invitations. "
            "Pass ?include_removed=true on the lookup so the soft-deleted "
            "row can be resolved."
        ),
    )
    @action(detail=True, methods=["post"])
    def recover(self, request, uuid=None):
        # get_queryset() returns the all-rows manager for `recover`, so the
        # standard DRF lookup correctly resolves the soft-deleted row.
        rp: models.ResourceProject = self.get_object()
        rp_ct = ContentType.objects.get_for_model(rp)
        if not rp.is_removed:
            raise rf_exceptions.ValidationError(
                "Resource project is not soft-deleted; nothing to recover."
            )
        if rp.resource.state in (
            ResourceStates.TERMINATING,
            ResourceStates.TERMINATED,
        ):
            raise rf_exceptions.ValidationError(
                "Cannot recover: the parent resource is being or has been terminated."
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        restore_team_members = serializer.validated_data["restore_team_members"]
        send_invitations = serializer.validated_data[
            "send_invitations_to_previous_members"
        ]
        if (restore_team_members or send_invitations) and not rp.termination_metadata:
            raise rf_exceptions.ValidationError(
                "This resource project was soft-deleted before metadata was "
                "captured. Only bare recovery is available; team-member "
                "restoration and invitations require a soft-delete performed "
                "after the recovery feature shipped."
            )

        with transaction.atomic():
            try:
                rp.is_removed = False
                rp.removed_date = None
                rp.removed_by = None
                rp.save(update_fields=["is_removed", "removed_date", "removed_by"])
            except IntegrityError:
                raise rf_exceptions.ValidationError(
                    "Cannot recover: another resource project with the same "
                    "name already exists on this resource."
                )

            restored_user_roles: list[UserRole] = []
            sent_invitations: list[Invitation] = []
            user_roles_data = (rp.termination_metadata or {}).get("user_roles", [])

            if restore_team_members:
                for role_data in user_roles_data:
                    if role_data.get("is_restored"):
                        continue
                    user = User.objects.filter(
                        username=role_data["user_username"]
                    ).first()
                    if not user or not user.is_active:
                        continue
                    role = Role.objects.filter(
                        name=role_data["role_name"], content_type=rp_ct
                    ).first()
                    if role is None:
                        continue
                    expiration_time = (
                        datetime.datetime.fromisoformat(
                            role_data["original_expiration_time"]
                        )
                        if role_data.get("original_expiration_time")
                        else None
                    )
                    if expiration_time and expiration_time < timezone.now():
                        continue
                    if UserRole.objects.filter(
                        user=user,
                        role=role,
                        content_type=rp_ct,
                        object_id=rp.id,
                        is_active=True,
                    ).exists():
                        continue
                    user_role = add_user(
                        scope=rp,
                        user=user,
                        role=role,
                        created_by=request.user,
                        expiration_time=expiration_time,
                    )
                    restored_user_roles.append(user_role)
                    role_data["is_restored"] = True
                    role_data["restored_at"] = timezone.now().isoformat()
                    role_data["restored_by"] = request.user.username
            elif send_invitations:
                for role_data in user_roles_data:
                    if role_data.get("invitation_sent"):
                        continue
                    role = Role.objects.filter(
                        name=role_data["role_name"], content_type=rp_ct
                    ).first()
                    if role is None:
                        continue
                    email = role_data.get("user_email")
                    if not email:
                        continue
                    duplicates = get_invitation_duplicates(
                        rp, [{"email": email, "role": role}]
                    )
                    if duplicates:
                        existing_uuid = duplicates[0]["existing_invitation_uuid"]
                        role_data["invitation_sent"] = True
                        role_data["invitation_sent_at"] = timezone.now().isoformat()
                        role_data["invitation_sent_by"] = request.user.username
                        role_data["existing_invitation_uuid"] = str(existing_uuid)
                        existing = Invitation.objects.filter(uuid=existing_uuid).first()
                        if existing is not None:
                            sent_invitations.append(existing)
                        continue
                    invitation = Invitation.objects.create(
                        email=email,
                        role=role,
                        scope=rp,
                        customer=rp.customer,
                        created_by=request.user,
                        state=InvitationState.PENDING,
                    )
                    sent_invitations.append(invitation)
                    role_data["invitation_sent"] = True
                    role_data["invitation_sent_at"] = timezone.now().isoformat()
                    role_data["invitation_sent_by"] = request.user.username
                    role_data["invitation_uuid"] = str(invitation.uuid)

            if restore_team_members or send_invitations:
                rp.save(update_fields=["termination_metadata"])

        logger.info(
            "%s recovered resource project %s (restored=%d, invited=%d)",
            request.user.full_name,
            rp.uuid,
            len(restored_user_roles),
            len(sent_invitations),
        )
        log.log_resource_project_recovered(rp)

        body = serializers.ResourceProjectSerializer(
            rp, context={"request": request}
        ).data
        body["recovery_info"] = {
            "restored_users_count": len(restored_user_roles),
            "sent_invitations_count": len(sent_invitations),
        }
        return Response(body, status=status.HTTP_200_OK)

    recover_serializer_class = serializers.ResourceProjectRecoverySerializer
    recover_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE,
            ["resource.project", "resource.project.customer"],
        )
    ]


class ProviderResourceProjectViewSet(UserRoleMixin, core_views.ActionsViewSet):
    """
    Manage sub-projects within a resource (provider perspective).

    Provides state management actions for provisioning workflow.
    Filter by resource using ``?resource={uuid}`` query parameter.
    """

    queryset = models.ResourceProject.available_objects.all().select_related("resource")
    serializer_class = serializers.ResourceProjectSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ResourceProjectFilter
    disabled_actions = ["create", "destroy"]
    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            # Offering-scoped roles (e.g. a site agent running as
            # OFFERING.MANAGER) manage sub-project state, so accept the
            # offering scope alongside the owning customer.
            ["resource.offering", "resource.offering.customer"],
        )
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        provider_resources = models.Resource.objects.all().filter_for_service_provider(
            user
        )
        return qs.filter(resource__in=provider_resources)

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer})
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        project = self.get_object()
        serializer = serializers.ResourceProjectBackendIdSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project.backend_id = serializer.validated_data["backend_id"]
        project.save(update_fields=["backend_id"])
        return Response({"status": "backend_id updated"}, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses={200: StatusSerializer})
    @action(detail=True, methods=["post"])
    def set_state_ok(self, request, uuid=None):
        project = self.get_object()
        try:
            project.set_state_ok()
        except TransitionNotAllowed as exc:
            raise ValidationError(
                f"Cannot transition resource project from {project.get_state_display()} to OK."
            ) from exc
        # Clear any prior error_message so a stale failure doesn't
        # linger after recovery.
        project.error_message = ""
        project.save(update_fields=["state", "error_message"])
        return Response({"status": "state set to OK"}, status=status.HTTP_200_OK)

    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer})
    @action(detail=True, methods=["post"])
    def set_state_erred(self, request, uuid=None):
        project = self.get_object()
        # Validate via the per-action serializer so the OpenAPI schema
        # advertises the optional ``error_message`` field (the SDK
        # generator wires it into a typed body model). Reading
        # request.data.get(...) directly works at runtime but produces
        # an SDK with the unrelated ResourceProjectRequest as the body
        # type, forcing callers to smuggle the field via additional
        # properties.
        serializer = serializers.ResourceProjectErrorMessageSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        project.error_message = serializer.validated_data["error_message"]
        try:
            project.set_state_erred()
        except TransitionNotAllowed as exc:
            raise ValidationError(
                f"Cannot transition resource project from {project.get_state_display()} to Erred."
            ) from exc
        project.save(update_fields=["state", "error_message"])
        return Response({"status": "state set to Erred"}, status=status.HTTP_200_OK)

    set_backend_id_serializer_class = serializers.ResourceProjectBackendIdSerializer
    set_state_erred_serializer_class = serializers.ResourceProjectErrorMessageSerializer


class OfferingRoleViewSet(core_views.ActionsViewSet):
    """
    Manage roles available for an offering's resources and resource projects.

    Service providers create custom roles (e.g., "Cluster Admin", "Project Member")
    that can be assigned to users of their offering's resources.
    """

    queryset = permission_models.Role.objects.filter(
        is_system_role=False,
    ).select_related("content_type")
    serializer_class = serializers.OfferingRoleSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingRoleFilter

    def get_queryset(self):
        qs = super().get_queryset()
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        return qs.filter(availability__content_type=offering_ct).distinct()

    def _check_update_permission(self, instance):
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        availabilities = instance.availability.filter(content_type=offering_ct)
        offering_ids = list(availabilities.values_list("object_id", flat=True))
        if not offering_ids:
            if not self.request.user.is_staff:
                raise PermissionDenied()
            return
        for offering in models.Offering.objects.filter(id__in=offering_ids):
            if not has_permission(
                self.request,
                PermissionEnum.UPDATE_OFFERING,
                offering.customer,
            ):
                raise PermissionDenied()

    @staticmethod
    def _reject_if_profile_bound(offering, action: str):
        if offering.profile_id is None:
            return
        raise rf_exceptions.ValidationError(
            f"Cannot {action} role for offering {offering.name}: its role catalog "
            f"is managed by service profile '{offering.profile.name}'."
        )

    def perform_create(self, serializer):
        offering = serializer.validated_data.pop("offering")
        if not has_permission(
            self.request,
            PermissionEnum.UPDATE_OFFERING,
            offering.customer,
        ):
            raise PermissionDenied()
        self._reject_if_profile_bound(offering, "create")
        role = serializer.save()
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        permission_models.RoleAvailability.objects.get_or_create(
            role=role,
            content_type=offering_ct,
            object_id=offering.id,
        )

    def perform_update(self, serializer):
        self._check_update_permission(serializer.instance)
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        for ra in serializer.instance.availability.filter(content_type=offering_ct):
            offering = models.Offering.objects.filter(id=ra.object_id).first()
            if offering:
                self._reject_if_profile_bound(offering, "update")
        serializer.save()

    def perform_destroy(self, instance):
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        availabilities = instance.availability.filter(content_type=offering_ct)
        if availabilities.exists():
            offering_ids = availabilities.values_list("object_id", flat=True)
            offerings = models.Offering.objects.filter(id__in=offering_ids)
            for offering in offerings:
                if not has_permission(
                    self.request,
                    PermissionEnum.UPDATE_OFFERING,
                    offering.customer,
                ):
                    raise PermissionDenied()
                self._reject_if_profile_bound(offering, "delete")
        elif not self.request.user.is_staff:
            raise PermissionDenied()
        instance.delete()


class OfferingProfileViewSet(core_views.ActionsViewSet):
    """Service profile = logical grouping of offerings sharing a role catalog.

    Maintained by staff. Read-open to authenticated users (so service
    providers can pick a profile when configuring an offering). Adding or
    removing roles triggers async reconciliation of RoleAvailability rows
    on every offering bound to the profile.
    """

    queryset = models.OfferingProfile.objects.prefetch_related("roles", "offerings")
    serializer_class = serializers.OfferingProfileSerializer
    lookup_field = "uuid"

    def _check_staff(self):
        if not self.request.user.is_staff:
            raise PermissionDenied("Only staff can manage service profiles.")

    def perform_create(self, serializer):
        self._check_staff()
        serializer.save()

    def perform_update(self, serializer):
        self._check_staff()
        serializer.save()

    def perform_destroy(self, instance):
        self._check_staff()
        instance.delete()

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.OfferingProfileSerializer}
    )
    @action(detail=True, methods=["post"])
    def add_role(self, request, uuid=None):
        self._check_staff()
        profile = self.get_object()
        ser = serializers.OfferingProfileRoleAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            role = permission_models.Role.objects.get(uuid=ser.validated_data["role"])
        except permission_models.Role.DoesNotExist:
            raise rf_exceptions.NotFound("Role not found.")
        profile.roles.add(role)
        return Response(
            serializers.OfferingProfileSerializer(
                profile, context={"request": request}
            ).data
        )

    add_role_serializer_class = serializers.OfferingProfileRoleAssignSerializer

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.OfferingProfileSerializer}
    )
    @action(detail=True, methods=["post"])
    def remove_role(self, request, uuid=None):
        self._check_staff()
        profile = self.get_object()
        ser = serializers.OfferingProfileRoleAssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            role = permission_models.Role.objects.get(uuid=ser.validated_data["role"])
        except permission_models.Role.DoesNotExist:
            raise rf_exceptions.NotFound("Role not found.")
        profile.roles.remove(role)
        return Response(
            serializers.OfferingProfileSerializer(
                profile, context={"request": request}
            ).data
        )

    remove_role_serializer_class = serializers.OfferingProfileRoleAssignSerializer


class OfferingPermissionViewSet(rf_viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.OfferingPermissionSerializer
    filterset_class = filters.OfferingPermissionFilter

    def get_queryset(self):
        return filter_offering_permissions(self.request.user)


class OfferingPermissionLogViewSet(rf_viewsets.ReadOnlyModelViewSet):
    serializer_class = serializers.OfferingPermissionSerializer
    filterset_class = filters.OfferingPermissionFilter

    def get_queryset(self):
        return filter_offering_permissions(self.request.user, is_active=False)


class PlanUsageReporter:
    """
    This class provides aggregate counts of how many plans of a
    certain type for each offering is used.
    """

    def __init__(self, view, request):
        self.view = view
        self.request = request

    def get_report(self):
        plans = models.Plan.objects.exclude(offering__billable=False)

        query = self.parse_query()
        if query:
            plans = self.apply_filters(query, plans)

        resources = models.Resource.objects.filter(plan_id=OuterRef("pk")).exclude(
            state=ResourceStates.TERMINATED
        )
        remaining = ExpressionWrapper(
            F("limit") - F("usage"), output_field=PositiveSmallIntegerField()
        )
        plans = plans.annotate(
            usage=SubqueryCount(resources), limit=F("max_amount")
        ).annotate(remaining=remaining)
        plans = self.apply_ordering(plans)

        return self.serialize(plans)

    def parse_query(self):
        if self.request.query_params:
            serializer = serializers.PlanUsageRequestSerializer(
                data=self.request.query_params
            )
            serializer.is_valid(raise_exception=True)
            return serializer.validated_data
        return None

    def apply_filters(self, query, plans):
        if query.get("offering_uuid"):
            plans = plans.filter(offering__uuid=query.get("offering_uuid"))

        if query.get("customer_provider_uuid"):
            plans = plans.filter(
                offering__customer__uuid=query.get("customer_provider_uuid")
            )

        return plans

    def apply_ordering(self, plans):
        param = (
            self.request.query_params and self.request.query_params.get("o") or "-usage"
        )
        return order_with_nulls(plans, param)

    def serialize(self, plans):
        page = self.view.paginate_queryset(plans)
        serializer = serializers.PlanUsageResponseSerializer(page, many=True)
        return self.view.get_paginated_response(serializer.data)


def can_manage_plan(plan):
    if not plugins.manager.can_manage_plans(plan.offering.type):
        raise rf_exceptions.ValidationError(
            _("It is not possible to update plan for this offering type.")
        )


def validate_plan_update(plan):
    if models.Resource.objects.filter(plan=plan).exists():
        raise rf_exceptions.ValidationError(
            _("It is not possible to update plan because it is used by resources.")
        )


def validate_plan_archive(plan):
    if plan.archived:
        raise rf_exceptions.ValidationError(_("Plan is already archived."))


@extend_schema_view(
    list=extend_schema(
        summary="List provider plans",
        description="Returns a paginated list of plans managed by the provider. The list is filtered based on the current user's access to the offering's customer.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a provider plan",
        description="Returns details of a specific plan.",
    ),
    create=extend_schema(
        summary="Create a provider plan",
        description="Creates a new billing plan for an offering.",
        request=serializers.ProviderPlanDetailsSerializer,
        responses={201: serializers.ProviderPlanDetailsSerializer},
    ),
    update=extend_schema(
        summary="Update a provider plan",
        description="Updates an existing plan. Note: A plan cannot be updated if it is already used by resources.",
    ),
    partial_update=extend_schema(
        summary="Partially update a provider plan",
        description="Partially updates an existing plan. Note: A plan cannot be updated if it is already used by resources.",
    ),
    destroy=extend_schema(
        summary="Delete a provider plan",
        description="Deletes a plan. This is a hard delete and should be used with caution.",
    ),
)
class ProviderPlanViewSet(
    core_views.HistoryViewSetMixin,
    core_views.UpdateReversionMixin,
    core_views.ActionsViewSet,
):
    lookup_field = "uuid"
    queryset = models.Plan.objects.all()
    serializer_class = serializers.ProviderPlanDetailsSerializer
    filterset_class = filters.PlanFilter
    filter_backends = (DjangoFilterBackend, filters.ProviderPlanFilterBackend)

    destroy_permissions = [structure_permissions.is_staff]
    update_validators = partial_update_validators = [validate_plan_update]

    update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_PLAN,
            ["offering.customer"],
        )
    ]

    def perform_destroy(self, instance):
        user = self.request.user
        logger.info(
            f"Plan {instance.name} from {instance.offering.name} deleted by user {user}."
        )
        event_logger.emit(
            f"Plan {instance.name} from {instance.offering.name} deleted by user {user}.",
            event_type=EventType.MARKETPLACE_PLAN_DELETED,
            event_context={
                "plan": instance,
            },
            scopes=get_plan_scopes(instance),
        )
        super().perform_destroy(instance)

    @extend_schema(
        summary="Update plan component prices",
        description="Updates the prices for one or more components of a specific plan. If the plan is already in use by resources, this action updates the `future_price`, which will be applied from the next billing period. Otherwise, the current `price` is updated directly.",
        request=serializers.PricesUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_prices(self, request, uuid):
        plan: models.Plan = self.get_object()
        serializer = serializers.PricesUpdateSerializer(
            data=request.data, instance=plan
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_prices_permissions = update_permissions
    update_prices_validators = [can_manage_plan]

    @extend_schema(
        summary="Update plan component quotas",
        description="Updates the quotas (fixed amounts) for one or more components of a specific plan. This is only applicable for components with a 'fixed-price' billing type.",
        request=serializers.QuotasUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_quotas(self, request, uuid):
        plan: models.Plan = self.get_object()
        serializer = serializers.QuotasUpdateSerializer(
            data=request.data, instance=plan
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_quotas_permissions = update_permissions
    update_quotas_validators = [can_manage_plan]

    @extend_schema(
        summary="Update plan component discounts",
        description="""
        Update volume discount configuration for plan components.

        This endpoint allows updating discount thresholds and rates for multiple
        plan components in a single request. Discounts are applied automatically
        when limit quantities meet or exceed the threshold.

        The discount configuration affects future billing:
        - Creates separate invoice items showing the discount.
        - Can be enabled or disabled per component.
        """,
        request=serializers.DiscountsUpdateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_discounts(self, request, uuid):
        plan: models.Plan = self.get_object()
        serializer = serializers.DiscountsUpdateSerializer(
            data=request.data, instance=plan
        )
        serializer.is_valid(raise_exception=True)
        updated_components = serializer.save()

        # Log the discount update
        logger.info(
            f"Discounts updated for plan {plan.name} (UUID: {plan.uuid}) "
            f"by user {request.user.username}. "
            f"Updated {len(updated_components)} component(s)."
        )

        return Response(status=status.HTTP_200_OK)

    update_discounts_permissions = update_permissions
    update_discounts_validators = [can_manage_plan]

    archive_permissions = [
        permission_factory(
            PermissionEnum.ARCHIVE_OFFERING_PLAN,
            ["offering.customer"],
        )
    ]

    archive_validators = [validate_plan_archive]

    @extend_schema(
        summary="Archive a plan",
        description="Marks a plan as archived. Archived plans cannot be used for provisioning new resources, but existing resources will continue to be billed according to this plan.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        plan: models.Plan = self.get_object()
        with reversion.create_revision():
            plan.archived = True
            plan.save(update_fields=["archived"])
            reversion.set_user(self.request.user)
            reversion.set_comment("Plan has been archived.")
        return Response(
            {"detail": _("Plan has been archived.")}, status=status.HTTP_200_OK
        )

    @extend_schema(
        summary="Get plan usage statistics",
        description="Returns aggregated statistics on how many resources are currently using each plan. Can be filtered by offering or service provider.",
        parameters=[
            OpenApiParameter(
                name="offering_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by offering UUID.",
            ),
            OpenApiParameter(
                name="customer_provider_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by offering customer provider UUID.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                name="o",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Ordering field. Available options: `usage`, `limit`, `remaining`, and their descending counterparts (e.g., `-usage`).",
            ),
        ],
        responses=serializers.PlanUsageResponseSerializer(many=True),
    )
    @action(detail=False)
    def usage_stats(self, request):
        return PlanUsageReporter(self, request).get_report()

    @extend_schema(
        summary="Update organization groups for a plan",
        description="Sets the list of organization groups that are allowed to access this plan. If the list is empty, the plan is accessible to all.",
        request=serializers.OrganizationGroupsSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_organization_groups(self, request, uuid):
        plan: models.Plan = self.get_object()
        serializer = serializers.OrganizationGroupsSerializer(
            instance=plan, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_organization_groups_permissions = update_permissions

    @extend_schema(
        summary="Remove all organization groups from a plan",
        description="Removes all organization group associations from this plan, making it accessible to all users (subject to offering-level restrictions).",
        request=None,
        responses={204: None},
    )
    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        plan: models.Plan = self.get_object()
        plan.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions


@extend_schema_view(
    list=extend_schema(
        summary="List plan components",
        description="Returns a paginated list of all plan components. A plan component defines the pricing and quotas for an offering component within a billing plan. The list is filtered based on the current user's access permissions and organization group memberships.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a plan component",
        description="Returns the details of a specific plan component, including its pricing, quotas, and associated offering and plan information.",
    ),
)
class PlanComponentViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    """
    Provides read-only access to plan components.

    A plan component links an offering component (e.g., CPU, RAM) to a specific plan,
    defining its price and fixed quota (if applicable). This endpoint allows users to
    view the component details for available plans.
    """

    queryset = models.PlanComponent.objects.filter()
    serializer_class = serializers.PlanComponentSerializer
    filterset_class = filters.PlanComponentFilter
    lookup_field = "pk"

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.is_anonymous:
            return queryset.filter(
                plan__offering__shared=True, plan__organization_groups__isnull=True
            )
        elif user.is_staff or user.is_support:
            return queryset
        else:
            return queryset.filter(
                Q(plan__organization_groups__isnull=True)
                | Q(plan__organization_groups__in=get_organization_groups(user))
            )


class ScreenshotViewSet(
    core_views.CreateReversionMixin,
    core_views.UpdateReversionMixin,
    core_views.ActionsViewSet,
):
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    queryset = models.Screenshot.objects.all().order_by("offering__name")
    serializer_class = serializers.ScreenshotSerializer
    filterset_class = filters.ScreenshotFilter

    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING_SCREENSHOT,
            ["offering.customer"],
        )
    ]


class PluginViewSet(views.APIView):
    permission_classes = ()
    authentication_classes = ()

    @extend_schema(
        summary="List available marketplace plugins and their components",
        description="""
        Returns a list of all registered marketplace plugins (offering types) and the components
        associated with each. This endpoint is public and does not require authentication.

        Each plugin entry includes:
        - `offering_type`: A unique identifier for the plugin.
        - `components`: A list of components provided by the plugin, each with its `type`, `name`, `measured_unit`, and `billing_type`.
        - `available_limits`: A list of component types that support user-defined limits for this plugin.
        """,
        responses={
            200: serializers.PluginOfferingTypeSerializer(many=True),
        },
        examples=[
            OpenApiExample(
                name="Example Response for Marketplace Plugins",
                value=[
                    {
                        "offering_type": "Marketplace.Slurm",
                        "components": [
                            {
                                "type": "cpu",
                                "name": "CPU",
                                "measured_unit": "hours",
                                "billing_type": "usage",
                            },
                            {
                                "type": "gpu",
                                "name": "GPU",
                                "measured_unit": "hours",
                                "billing_type": "usage",
                            },
                            {
                                "type": "ram",
                                "name": "RAM",
                                "measured_unit": "GB-hours",
                                "billing_type": "usage",
                            },
                        ],
                        "available_limits": [],
                    },
                ],
            )
        ],
    )
    def get(self, request):
        offering_types = plugins.manager.get_offering_types()
        payload = []
        for offering_type in offering_types:
            components = [
                dict(
                    type=component.type,
                    name=component.name,
                    measured_unit=component.measured_unit,
                    billing_type=component.billing_type,
                )
                for component in plugins.manager.get_components(offering_type)
            ]
            payload.append(
                dict(
                    offering_type=offering_type,
                    components=components,
                    available_limits=plugins.manager.get_available_limits(
                        offering_type
                    ),
                )
            )
        return Response(payload, status=status.HTTP_200_OK)


class OfferingTypeValidator:
    def __init__(self, *valid_types):
        self.valid_types = valid_types

    def __call__(self, order: models.Order):
        if order.offering.type not in self.valid_types:
            raise rf_exceptions.MethodNotAllowed(
                _(
                    "The order's offering with %s type does not support this action"
                    % order.offering.type
                )
            )


def _validate_offering_supports_retry(order: models.Order):
    if not plugins.manager.supports_order_retry(order.offering.type):
        raise rf_exceptions.MethodNotAllowed(
            _("Retry is not supported for offerings of type %s" % order.offering.type)
        )


_RESOURCE_FAILURE_EVENTS = {
    OrderTypes.CREATE: (
        EventType.MARKETPLACE_RESOURCE_CREATE_FAILED,
        "Resource {resource_name} creation has failed.",
    ),
    OrderTypes.UPDATE: (
        EventType.MARKETPLACE_RESOURCE_UPDATE_FAILED,
        "Resource {resource_name} update has failed.",
    ),
    OrderTypes.TERMINATE: (
        EventType.MARKETPLACE_RESOURCE_TERMINATE_FAILED,
        "Resource {resource_name} deletion has failed.",
    ),
}


def _emit_resource_failure_event(order, resource):
    event_info = _RESOURCE_FAILURE_EVENTS.get(order.type)
    if event_info:
        event_type, message = event_info
        event_context = {"resource": resource}
        # The reason otherwise lives only on the order, so a resource's activity
        # log read "deletion has failed" and the consumer had to open the order
        # to learn it was, say, a backend refusing to remove a user that still
        # owns buckets. The activity log is where people look first, and unlike
        # the resource's own error_message it keeps one entry per attempt.
        if order.error_message:
            message = f"{message.rstrip('.')}: {{error_message}}"
            event_context["error_message"] = order.error_message
        event_logger.emit(
            message,
            event_type=event_type,
            event_context=event_context,
            scopes=log.get_resource_scopes(resource),
            level="error",
        )


@extend_schema_view(
    list=extend_schema(
        summary="List orders",
        description="Returns a paginated list of orders accessible to the current user. Orders are visible to service consumers (project/customer members with appropriate permissions) and service providers.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an order",
        description="Returns the details of a specific order.",
    ),
    create=extend_schema(
        summary="Create an order",
        description="Creates a new order to provision a resource. The order will be placed in a pending state and may require approval depending on the offering and user permissions.",
        request=serializers.OrderCreateSerializer,
        responses={201: serializers.OrderDetailsSerializer},
        examples=[
            OpenApiExample(
                name="Create a resource from a public offering",
                summary="Example of creating a new resource.",
                value={
                    "offering": "http://testserver/api/marketplace-public-offerings/a1b2c3d4e5f678901234567890abcdef/",
                    "project": "http://testserver/api/projects/b2c3d4e5f678901234567890abcdef12/",
                    "plan": "http://testserver/api/marketplace-public-offerings/a1b2c3d4e5f678901234567890abcdef/plans/c3d4e5f678901234567890abcdef1234/",
                    "attributes": {
                        "name": "My New Virtual Machine",
                        "cores": 2,
                        "ram_gb": 4,
                        "storage_gb": 50,
                    },
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Delete a pending order",
        description="Deletes an order that is still in a pending state (e.g., `pending-consumer` or `pending-provider`). Executing or completed orders cannot be deleted.",
    ),
)
class OrderViewSet(
    ConnectedResourceDetailsMixin, ConnectedOfferingDetailsMixin, BaseMarketplaceView
):
    queryset = models.Order.objects.select_related(
        "resource",
        "project",
        "project__customer",
        "offering",
        "offering__customer",
        "offering__category",
        "plan",
        "old_plan",
        "created_by",
        "consumer_reviewed_by",
        "provider_reviewed_by",
    ).all()
    filter_backends = (DjangoFilterBackend,)
    serializer_class = serializers.OrderDetailsSerializer
    create_serializer_class = serializers.OrderCreateSerializer
    update_serializer_class = serializers.OrderUpdateSerializer
    partial_update_serializer_class = serializers.OrderUpdateSerializer
    filterset_class = filters.OrderFilter
    disabled_actions = []

    def get_serializer_class(self):
        if self.action in ["create"]:
            return self.create_serializer_class
        if self.action in ["update"]:
            return self.update_serializer_class
        if self.action in ["partial_update"]:
            return self.partial_update_serializer_class
        if self.action == "approve_by_provider":
            return self.approve_by_provider_serializer_class
        return super().get_serializer_class()

    def get_queryset(self):
        """
        Orders are available to both service provider and service consumer.
        """
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset

        connected_projects = get_connected_projects_by_permission(
            user, PermissionEnum.LIST_ORDERS
        )
        connected_customers = get_connected_customers_by_permission(
            user, PermissionEnum.LIST_ORDERS
        )
        connected_offerings = get_connected_offerings_by_permission(
            user, PermissionEnum.LIST_ORDERS
        )

        # Use a subquery to find matching order IDs to avoid
        # SELECT DISTINCT across all select_related columns (Fixes PUHURI-PORTALS-ETK)
        order_ids = Subquery(
            models.Order.objects.filter(
                Q(project__in=connected_projects)
                | Q(project__customer__in=connected_customers)
                | Q(offering__customer__in=connected_customers)
                | Q(offering__in=connected_offerings)
            ).values("id")
        )
        return self.queryset.filter(id__in=order_ids)

    approve_by_consumer_validators = [
        structure_utils.check_customer_blocked_or_archived,
        structure_utils.check_project_end_date,
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER, state_enum=OrderStates
        ),
    ]

    @staticmethod
    def check_create_permissions(request, view, obj=None):
        user = request.user
        if user.is_staff or user.is_support:
            return
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data.get("project")
        offering = serializer.validated_data.get("offering")
        if project and offering:
            marketplace_permissions.check_offering_restriction(user, project, offering)
        if project and marketplace_permissions.has_project_permission(
            request, PermissionEnum.CREATE_ORDER, project
        ):
            return
        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]

    approve_by_consumer_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["project", "project.customer"],
        )
    ]

    update_permissions = partial_update_permissions = approve_by_consumer_permissions
    update_validators = partial_update_validators = approve_by_consumer_validators

    def list(self, request, *args, **kwargs):
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.ORDER_PROCESSING
        )
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.EVENT_PROCESSING
        )
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.ORDER_PROCESSING
        )
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.EVENT_PROCESSING
        )
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Approve an order (consumer)",
        description="Approves a pending order from the consumer's side (e.g., project manager, customer owner). This transitions the order to the next state, which could be pending provider approval or executing.",
        request=None,
        responses=serializers.OrderInfoResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def approve_by_consumer(self, request, uuid=None):
        order: models.Order = self.get_object()
        if (
            order.type != OrderTypes.TERMINATE
            and order.offering.plugin_options.get("require_purchase_order_upload")
            and not order.attachment
        ):
            raise rf_exceptions.ValidationError(
                _("Purchase order is required for approval.")
            )

        with transaction.atomic():
            order = (
                models.Order.objects.select_for_update(of=("self",))
                .select_related("project", "offering", "plan")
                .get(pk=order.pk)
            )
            if order.state != OrderStates.PENDING_CONSUMER:
                raise rf_exceptions.ValidationError(
                    _("Order is not pending consumer review.")
                )
            order.review_by_consumer(request.user)
            outcome = order_approval.transition_order_from_consumer_approval(
                order, request.user
            )

        messages = {
            "pending_project": "Order is pending project activation.",
            "pending_provider": "Order is pending provider approval.",
            "pending_start_date": "Order is pending start date.",
            "executing": "Order has been approved and is being processed.",
        }
        response_serializer = serializers.OrderInfoResponseSerializer(
            {"detail": messages[outcome]}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    approve_by_provider_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_PROVIDER, state_enum=OrderStates
        ),
    ]

    approve_by_provider_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]
    approve_by_provider_serializer_class = serializers.OrderApproveByProviderSerializer

    @extend_schema(
        summary="Approve an order (provider)",
        description="Approves a pending order from the provider's side. This typically transitions the order to the executing state.",
        responses=serializers.OrderInfoResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def approve_by_provider(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attributes = serializer.validated_data.get("attributes")
        if attributes:
            order.attributes.update(attributes)
            order.save(update_fields=["attributes"])
        order.review_by_provider(request.user)

        # After provider approval, check for the order's own start_date
        if (
            config.ENABLE_ORDER_START_DATE
            and order.start_date
            and order.start_date > timezone.now().date()
        ):
            order.state = OrderStates.PENDING_START_DATE
            order.save(update_fields=["state"])
            logger.info(
                "Order %s (%s) is pending start date %s after provider approval.",
                order,
                order.id,
                order.start_date,
            )
            response_serializer = serializers.OrderInfoResponseSerializer(
                {"detail": "Order is pending start date."}
            )
            return Response(response_serializer.data, status=status.HTTP_200_OK)

        order.set_state_executing()
        order.save(update_fields=["state"])
        logger.info(
            "Processing order %s (%s) after provider approval, resource %s",
            order,
            order.id,
            order.resource,
        )
        tasks.process_order_on_commit(order, request.user)
        response_serializer = serializers.OrderInfoResponseSerializer(
            {"detail": "Order has been approved and is being processed."}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    def _check_provider_consumer_messaging_enabled(order):
        if not order.offering.plugin_options.get("enable_provider_consumer_messaging"):
            raise IncorrectStateException(
                _("Provider-consumer messaging is not enabled for this offering.")
            )

    set_provider_info_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_PROVIDER, state_enum=OrderStates
        ),
        _check_provider_consumer_messaging_enabled,
    ]

    set_provider_info_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]
    set_provider_info_serializer_class = serializers.OrderProviderInfoSerializer

    @extend_schema(
        summary="Set provider info on order",
        description="Allows a service provider to send a message with an optional URL and file attachment to the consumer on a pending order.",
        responses=serializers.OrderInfoResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def set_provider_info(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_fields = []
        for field in (
            "provider_message",
            "provider_message_url",
            "provider_message_attachment",
        ):
            if field in serializer.validated_data:
                value = serializer.validated_data[field]
                if (
                    field == "provider_message_attachment"
                    and order.provider_message_attachment
                ):
                    order.provider_message_attachment.delete(save=False)
                setattr(order, field, value)
                update_fields.append(field)

        if update_fields:
            order.save(update_fields=update_fields)

        transaction.on_commit(
            lambda: tasks.notify_consumer_about_provider_info.delay(order.uuid.hex)
        )

        response_serializer = serializers.OrderInfoResponseSerializer(
            {"detail": "Provider info has been saved."}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    set_consumer_info_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_PROVIDER, state_enum=OrderStates
        ),
        _check_provider_consumer_messaging_enabled,
    ]

    set_consumer_info_permissions = [
        permission_factory(
            PermissionEnum.SET_CONSUMER_ORDER_INFO,
            ["project", "project.customer"],
        )
    ]
    set_consumer_info_serializer_class = serializers.OrderConsumerInfoSerializer

    @extend_schema(
        summary="Set consumer info on order",
        description="Allows a consumer to respond to a provider's message with an optional message and file attachment on a pending order.",
        responses=serializers.OrderInfoResponseSerializer,
    )
    @action(detail=True, methods=["post"])
    def set_consumer_info(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        update_fields = []
        for field in ("consumer_message", "consumer_message_attachment"):
            if field in serializer.validated_data:
                value = serializer.validated_data[field]
                if (
                    field == "consumer_message_attachment"
                    and order.consumer_message_attachment
                ):
                    order.consumer_message_attachment.delete(save=False)
                setattr(order, field, value)
                update_fields.append(field)

        if update_fields:
            order.save(update_fields=update_fields)

        transaction.on_commit(
            lambda: tasks.notify_provider_about_consumer_info.delay(order.uuid.hex)
        )

        response_serializer = serializers.OrderInfoResponseSerializer(
            {"detail": "Consumer info has been saved."}
        )
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    reject_by_consumer_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER, state_enum=OrderStates
        ),
    ]

    reject_by_consumer_permissions = [permissions.user_can_reject_order_as_consumer]

    reject_by_consumer_serializer_class = serializers.OrderErrorDetailsSerializer

    @extend_schema(
        summary="Reject an order (consumer)",
        description="Rejects a pending order from the consumer's side. This moves the order to the 'rejected' state.",
        request=serializers.OrderErrorDetailsSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def reject_by_consumer(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if permissions.order_should_not_be_reviewed_by_consumer(order):
            raise rf_exceptions.ValidationError(
                "Review of order by consumer is not required."
            )
        if order.consumer_reviewed_by:
            raise rf_exceptions.ValidationError(
                "Order is already reviewed by consumer."
            )
        order.error_message = serializer.validated_data.get("error_message", "")
        order.error_traceback = serializer.validated_data.get("error_traceback", "")
        order.consumer_rejection_comment = serializer.validated_data.get(
            "consumer_rejection_comment", ""
        )
        order.review_by_consumer(request.user)
        order.reject()
        order.save()
        return Response(status=status.HTTP_200_OK)

    reject_by_provider_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_PROVIDER, state_enum=OrderStates
        ),
    ]

    reject_by_provider_permissions = [
        permission_factory(
            PermissionEnum.REJECT_ORDER,
            ["offering.customer"],
        )
    ]

    reject_by_provider_serializer_class = serializers.OrderProviderRejectionSerializer

    @extend_schema(
        summary="Reject an order (provider)",
        description="Rejects a pending order from the provider's side. This moves the order to the 'rejected' state.",
        request=serializers.OrderProviderRejectionSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def reject_by_provider(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order.provider_rejection_comment = serializer.validated_data.get(
            "provider_rejection_comment", ""
        )
        order.review_by_provider(request.user)
        order.reject()
        order.save()
        return Response(status=status.HTTP_200_OK)

    cancel_permissions = [
        permission_factory(
            PermissionEnum.CANCEL_ORDER,
            ["project", "project.customer"],
        )
    ]

    cancel_validators = [
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.PENDING_START_DATE,
            OrderStates.EXECUTING,
            state_enum=OrderStates,
        ),
        OfferingTypeValidator(BASIC_OFFERING, SUPPORT_OFFERING),
    ]

    @extend_schema(
        summary="Cancel an order",
        description="Cancels an order. This is typically only possible for certain offering types (e.g., basic support) and in specific states (pending or executing).",
        request=None,
        responses={202: None},
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.cancel()
        order.save(update_fields=["state"])
        return Response(status=status.HTTP_202_ACCEPTED)

    set_state_executing_validators = [
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.ERRED,
            state_enum=OrderStates,
        ),
        OfferingTypeValidator(SITE_AGENT_OFFERING),
    ]

    set_state_executing_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Set order state to executing (agent)",
        description="Used by external agents (e.g., site agent) to manually transition the order state to 'executing'. This is only applicable for specific offering types.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_state_executing(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.set_state_executing()
        order.save(update_fields=["state"])
        return Response(status=status.HTTP_200_OK)

    set_state_done_validators = [
        core_validators.StateValidator(
            OrderStates.EXECUTING,
            state_enum=OrderStates,
        ),
        OfferingTypeValidator(SITE_AGENT_OFFERING, BASIC_OFFERING, SUPPORT_OFFERING),
    ]

    set_state_done_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Set order state to done (agent)",
        description="Used by external agents (e.g., site agent) to manually transition the order state to 'done'. This is only applicable for specific offering types.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_state_done(self, request, uuid=None):
        order: models.Order = self.get_object()
        callbacks.sync_order_state(order, OrderStates.DONE)
        return Response(status=status.HTTP_200_OK)

    set_state_erred_validators = [
        OfferingTypeValidator(SITE_AGENT_OFFERING),
    ]

    set_state_erred_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer", "offering.customer.serviceprovider", "offering"],
        )
    ]

    @extend_schema(
        summary="Set order state to erred (agent)",
        description="Used by external agents to report a failure during order processing. An error message and traceback can be provided.",
        request=serializers.OrderErrorDetailsSerializer,
        responses={200: None},
        examples=[
            OpenApiExample(
                "Report an error",
                value={
                    "error_message": "Failed to connect to the backend.",
                    "error_traceback": "Traceback(...)",
                },
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def set_state_erred(self, request, uuid=None):
        order: models.Order = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            order.set_state_erred()
            order.error_message = serializer.validated_data["error_message"]
            order.error_traceback = serializer.validated_data["error_traceback"]
            order.save(update_fields=["state", "error_message", "error_traceback"])

            resource = order.resource
            if resource:
                resource = models.Resource.objects.select_for_update().get(
                    pk=resource.pk
                )
                if order.type == OrderTypes.TERMINATE:
                    if resource.state != ResourceStates.OK:
                        resource.set_state_ok()
                        resource.save(update_fields=["state"])
                else:
                    # update_resource_state_on_order_rejection_error_or_cancellation
                    # already ran as a side effect of order.save() above and may have
                    # resolved a failed Create order to Terminated (when the resource
                    # never got a backend_id, i.e. nothing was ever provisioned).
                    # Don't clobber that decision back to Erred.
                    if resource.state not in (
                        ResourceStates.ERRED,
                        ResourceStates.TERMINATED,
                    ):
                        resource.set_state_erred()
                        resource.save(update_fields=["state"])

                _emit_resource_failure_event(order, resource)

        return Response(status=status.HTTP_200_OK)

    set_state_erred_serializer_class = serializers.OrderErrorDetailsSerializer

    retry_validators = [
        core_validators.StateValidator(OrderStates.ERRED, state_enum=OrderStates),
        _validate_offering_supports_retry,
    ]

    retry_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Retry an erred order",
        description="Resets an erred order and its resource back to an active state so that the order can be reprocessed.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, uuid=None):
        order: models.Order = self.get_object()

        with transaction.atomic():
            order = models.Order.objects.select_for_update().get(pk=order.pk)

            if order.state != OrderStates.ERRED:
                raise rf_exceptions.ValidationError(
                    _("Order must be in erred state to retry.")
                )

            resource = order.resource
            resource = models.Resource.objects.select_for_update().get(pk=resource.pk)

            try:
                if order.type == OrderTypes.CREATE:
                    resource.set_state_creating()
                elif order.type == OrderTypes.UPDATE:
                    resource.set_state_updating()
                elif order.type == OrderTypes.TERMINATE:
                    resource.set_state_terminating()
                else:
                    raise rf_exceptions.ValidationError(
                        _("Retry is not supported for %(type)s orders.")
                        % {"type": order.get_type_display()}
                    )
            except TransitionNotAllowed:
                raise rf_exceptions.ValidationError(
                    _(
                        "Cannot retry: resource state %(state)s does not allow transition."
                    )
                    % {"state": resource.get_state_display()}
                )

            resource.error_message = ""
            resource.error_traceback = ""
            resource.save(update_fields=["state", "error_message", "error_traceback"])

            order.set_state_executing()
            order.error_message = ""
            order.error_traceback = ""
            order.completed_at = None
            order.save(
                update_fields=[
                    "state",
                    "error_message",
                    "error_traceback",
                    "completed_at",
                ]
            )

        tasks.process_order_on_commit(order, request.user)

        return Response(status=status.HTTP_200_OK)

    destroy_permissions = [
        permission_factory(
            PermissionEnum.DESTROY_ORDER,
            ["project", "project.customer"],
        )
    ]

    destroy_validators = [
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            state_enum=OrderStates,
        ),
        structure_utils.check_customer_blocked_or_archived,
    ]

    @extend_schema(
        summary="Unlink an order (staff only)",
        description="Forcefully deletes an order from the database without affecting the backend resource. This is a staff-only administrative action used to clean up stuck or invalid orders.",
        request=None,
        responses={204: None, 403: None},
    )
    @action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        if not request.user.is_staff:
            raise PermissionDenied()
        order: models.Order = self.get_object()
        logger.info("Starting unlink for order %s", order.uuid)
        event_logger.emit(
            "Order {order_uuid} for resource {resource_name} has been unlinked. Type: {type}",
            event_type=EventType.MARKETPLACE_ORDER_UNLINKED,
            event_context={
                "order": order,
                "type": order.get_type_display(),
                "resource_name": order.resource.name,
            },
            scopes=log.get_order_scopes(order),
        )
        try:
            order.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            logger.error(
                "Error unlinking the order. Error: %s",
                str(e),
            )

    @extend_schema(
        summary="Update order attachment",
        description="Allows uploading or replacing a file attachment (e.g., a purchase order) for a pending order.",
        request=serializers.OrderAttachmentSerializer,
        responses={200: serializers.OrderAttachmentSerializer},
    )
    @action(
        detail=True,
        methods=["POST"],
    )
    def update_attachment(self, request, uuid=None):
        order: models.Order = self.get_object()
        serializer = self.get_serializer(order, data=request.data)
        serializer.is_valid(raise_exception=True)

        # If an old file exists, delete it before saving the new one
        if order.attachment:
            order.attachment.delete(save=False)

        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Delete order attachment",
        description="Allows deleting an attachment from a pending order.",
        request=None,
        responses={204: None},
    )
    @action(
        detail=True,
        methods=["POST"],
    )
    def delete_attachment(self, request, uuid=None):
        order: models.Order = self.get_object()

        if not order.attachment:
            return Response(status=status.HTTP_404_NOT_FOUND)

        order.attachment.delete(save=False)  # Delete file from storage
        order.attachment = None
        order.save(update_fields=["attachment"])

        return Response(status=status.HTTP_204_NO_CONTENT)

    update_attachment_serializer_class = serializers.OrderAttachmentSerializer

    attachment_validators = [
        core_validators.StateValidator(
            OrderStates.PENDING_PROJECT,
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            state_enum=OrderStates,
        ),
    ]

    update_attachment_validators = attachment_validators
    delete_attachment_validators = attachment_validators

    @extend_schema(
        summary="Set order backend ID",
        description="Allows a service provider or staff to set or update the backend ID associated with an order. This is useful for linking the order to an external system's identifier.",
        request=serializers.OrderBackendIDSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
        examples=[
            OpenApiExample(
                "Success",
                value={"status": "Order backend_id has been changed."},
                response_only=True,
            )
        ],
    )
    @action(detail=True, methods=["POST"])
    def set_backend_id(self, request, uuid=None):
        order = cast(models.Order, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data["backend_id"]
        old_backend_id = order.backend_id
        if new_backend_id != old_backend_id:
            order.backend_id = serializer.validated_data["backend_id"]
            order.save()
            logger.info(
                "%s has changed order %s backend_id from %s to %s",
                request.user.full_name,
                order.uuid.hex,
                old_backend_id,
                new_backend_id,
            )

            return Response(
                {"status": _("Order backend_id has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Order backend_id is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_backend_id_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_ID,
            ["offering", "offering.customer"],
        )
    ]

    set_backend_id_serializer_class = serializers.OrderBackendIDSerializer

    set_backend_id_validators = [
        structure_utils.check_customer_blocked_or_archived,
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List resources",
        description="Returns a paginated list of resources accessible to the current user.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a resource",
        description="Returns details of a specific resource.",
    ),
    update=extend_schema(
        summary="Update a resource",
        description="Updates the name, description, or end date of a resource. Requires appropriate permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a resource",
        description="Partially updates the name, description, or end date of a resource. Requires appropriate permissions.",
    ),
)
class OfferingAccessSubnetViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingAccessSubnet.objects.all().order_by("inet")
    serializer_class = serializers.OfferingAccessSubnetSerializer
    lookup_field = "uuid"
    filterset_class = filters.OfferingAccessSubnetFilter
    filter_backends = (DjangoFilterBackend,)
    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING_ACCESS_SUBNET,
            ["offering", "offering.customer", "offering.customer.serviceprovider"],
        )
    ]
    update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_ACCESS_SUBNET,
            ["offering", "offering.customer", "offering.customer.serviceprovider"],
        )
    ]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.is_staff or user.is_support:
            return qs
        connected_customers = get_connected_customers(user)
        connected_offerings = get_connected_offerings(user)
        return qs.filter(
            Q(offering__customer__in=connected_customers)
            | Q(offering__in=connected_offerings)
        )


class BaseResourceViewSet(
    ConnectedOfferingDetailsMixin,
    core_views.HistoryViewSetMixin,
    core_views.ActionsViewSet,
):
    queryset = models.Resource.objects.all()
    filter_backends = (DjangoFilterBackend, filters.ResourceScopeFilterBackend)
    filterset_class = filters.ResourceFilter
    lookup_field = "uuid"
    serializer_class = serializers.ResourceSerializer
    disabled_actions = ["create", "destroy"]
    update_serializer_class = partial_update_serializer_class = (
        serializers.ResourceUpdateSerializer
    )
    # Use resource-specific serializer for history endpoints
    history_serializer_class = serializers.ResourceVersionSerializer
    update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE,
            ["project", "project.customer", "offering", "offering.customer"],
        )
    ]

    def ensure_resource_operations_allowed(self, resource: models.Resource) -> None:
        if resource.offering.state == OfferingStates.UNAVAILABLE:
            raise rf_exceptions.ValidationError(_("Offering is unavailable."))

    def get_object(self):
        resource = super().get_object()
        if self.request.method not in SAFE_METHODS:
            self.ensure_resource_operations_allowed(resource)
        return resource

    def perform_update(self, serializer):
        """Wrap resource updates with reversion tracking."""
        with reversion.create_revision():
            serializer.save()
            reversion.set_user(self.request.user)
            reversion.set_comment("Updated via REST API")

    def list(self, request, *args, **kwargs):
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.USAGE_REPORTING
        )
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.RESOURCE_SYNC
        )
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.EVENT_PROCESSING
        )
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.RESOURCE_SYNC
        )
        utils.refresh_integration_agent_status(
            request, models.IntegrationStatus.AgentTypes.EVENT_PROCESSING
        )
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        summary="Get resource details",
        description="Returns the detailed representation of the backend resource associated with the marketplace resource. The format of the response depends on the resource type.",
        filters=False,
        responses={
            200: OpenApiTypes.OBJECT,
            404: OpenApiTypes.NONE,
            204: OpenApiTypes.NONE,
        },
    )
    @action(detail=True, methods=["get"])
    def details(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        if not resource.scope:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if isinstance(resource.scope, models.Resource):
            serializer = serializers.ResourceSerializer(
                instance=resource.scope, context=self.get_serializer_context()
            )
            return Response(serializer.data, status=status.HTTP_200_OK)

        serializer_class = get_model_serializer(resource.scope)
        if not serializer_class:
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = serializer_class(
            instance=resource.scope, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Unlink a resource (staff only)",
        description="Forcefully deletes a marketplace resource and its related plugin resource from the database. This action does not schedule operations on the backend and is intended for cleaning up resources stuck in transitioning states. Requires staff permissions.",
        request=None,
        responses={204: None, 403: None},
    )
    @action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        """
        Delete marketplace resource and related plugin resource from the database without scheduling operations on backend
        and without checking current state of the resource. It is intended to be used
        for removing resource stuck in transitioning state.
        """
        resource: models.Resource = self.get_object()
        event_logger.emit(
            "Resource {resource_name} has been unlinked.",
            event_type=EventType.MARKETPLACE_RESOURCE_UNLINKED,
            event_context={"resource": resource},
            scopes=log.get_resource_scopes(resource),
        )
        logger.info("Starting unlink for resource %s", resource.uuid)
        try:
            if resource.scope:
                resource.scope.delete()
            resource.delete()
            logger.debug("Resource %s has been unlinked", resource.uuid)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as e:
            logger.error(
                "Error during resource unlink. Error %s",
                resource,
                str(e),
            )

    unlink_permissions = [structure_permissions.is_staff]

    def create_resource_order(
        self,
        request,
        resource: models.Resource,
        switch_price=None,
        **kwargs,
    ):
        self.ensure_resource_operations_allowed(resource)
        with transaction.atomic():
            order = models.Order(
                project=resource.project,
                created_by=request.user,
                resource=resource,
                offering=resource.offering,
                **kwargs,
            )
            serializers.validate_order(order, request)
            order.init_cost()

            # If a one-time charge (like a renewal fee) is provided,
            # it should be the primary cost for an UPDATE order.
            if order.type == OrderTypes.UPDATE and switch_price is not None:
                # For renewals, the cost is *only* the switch price.
                # For plan switches, it might be the plan's switch_price + estimate.
                order.cost = switch_price

            order.save()

        return Response({"order_uuid": order.uuid.hex}, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Terminate a resource",
        description="Creates a marketplace order to terminate the resource. This action is asynchronous and may require approval.",
        request=serializers.ResourceTerminateSerializer,
        responses=serializers.OrderUUIDSerializer,
    )
    @action(detail=True, methods=["post"])
    def terminate(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attributes = serializer.validated_data.get("attributes", {})

        pending_order = utils.get_pending_consumer_terminate_order(resource)
        if pending_order:
            if not permissions.user_can_approve_order_as_consumer(
                request.user, pending_order
            ):
                raise ValidationError(_("Pending order for resource already exists."))
            self.ensure_resource_operations_allowed(resource)
            structure_utils.check_customer_blocked_or_archived(
                pending_order.project.customer
            )
            order = order_approval.confirm_pending_terminate_order(
                pending_order, request.user
            )
            return Response({"order_uuid": order.uuid.hex}, status=status.HTTP_200_OK)

        return self.create_resource_order(
            request=request,
            resource=resource,
            type=OrderTypes.TERMINATE,
            attributes=attributes,
        )

    @extend_schema(responses={status.HTTP_200_OK: serializers.OrderUUIDSerializer})
    @action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        if not resource.offering.plugin_options.get("can_restore_resource"):
            raise ValidationError(
                _("Restoring resource is not supported for this offering type.")
            )

        resource.set_state_creating()
        resource.save(update_fields=["state"])

        return self.create_resource_order(
            request=request,
            resource=resource,
            type=OrderTypes.RESTORE,
            attributes=resource.attributes,
            plan=resource.plan,
            limits=resource.limits,
        )

    restore_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]

    restore_validators = [
        core_validators.StateValidator(
            ResourceStates.TERMINATED,
            state_enum=ResourceStates,
        ),
    ]

    terminate_serializer_class = serializers.ResourceTerminateSerializer

    terminate_permissions = [permissions.user_can_terminate_resource]

    terminate_validators = [permissions.validate_resource_terminate_state]

    @extend_schema(
        summary="List resource plan periods",
        description="Returns a list of active and future plan periods for the resource. Each period includes the plan details and current component usage.",
        request=None,
        responses=serializers.ResourcePlanPeriodSerializer(many=True),
        filters=False,
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def plan_periods(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        qs = models.ResourcePlanPeriod.objects.filter(resource=resource)
        qs = qs.filter(Q(end=None) | Q(end__gte=month_start(timezone.now())))
        serializer = serializers.ResourcePlanPeriodSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Move a resource to another project",
        description="Moves a resource and its associated data to a different project. Requires staff permissions.",
        request=serializers.MoveResourceSerializer,
        responses={status.HTTP_200_OK: serializers.ResourceSerializer},
    )
    @action(detail=True, methods=["post"])
    def move_resource(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        request_serializer = serializers.MoveResourceSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        request_serializer.is_valid(raise_exception=True)
        project = request_serializer.validated_data["project"]
        try:
            utils.move_resource(resource, project)
        except utils.MoveResourceException as exception:
            error_message = str(exception)
            return JsonResponse({"error_message": error_message}, status=409)

        response_serializer = serializers.ResourceSerializer(
            resource, context=self.get_serializer_context()
        )

        return Response(response_serializer.data, status=status.HTTP_200_OK)

    move_resource_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Set resource slug",
        description="Updates the slug for a resource. Requires staff permissions.",
        request=serializers.ResourceSlugSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_slug(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_slug = serializer.validated_data["slug"]
        old_slug = resource.slug
        if new_slug != old_slug:
            resource.slug = serializer.validated_data["slug"]
            with reversion.create_revision():
                resource.save()
                reversion.set_user(request.user)
                reversion.set_comment(f"Slug changed from '{old_slug}' to '{new_slug}'")
            logger.info(
                "%s has changed slug from %s to %s",
                request.user.full_name,
                old_slug,
                new_slug,
            )

            return Response(
                {"status": _("Resource slug has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource slug is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_slug_permissions = [structure_permissions.is_staff]

    set_slug_serializer_class = serializers.ResourceSlugSerializer

    @extend_schema(
        summary="Set downscaled flag for resource",
        description="Sets the 'downscaled' flag for a resource. Requires staff permissions.",
        request=serializers.ResourceDownscaledSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_downscaled(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_downscaled = serializer.validated_data["downscaled"]
        old_downscaled = resource.downscaled
        if new_downscaled != old_downscaled:
            resource.downscaled = new_downscaled
            with reversion.create_revision():
                resource.save()
                reversion.set_user(request.user)
                reversion.set_comment(
                    f"Downscaled changed from {old_downscaled} to {new_downscaled}"
                )
            logger.info(
                "%s has changed downscaled from %s to %s for resource %s",
                request.user.full_name,
                old_downscaled,
                new_downscaled,
                resource.uuid,
            )
            return Response(
                {"status": _("Resource downscaled flag has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource downscaled flag is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_downscaled_permissions = [structure_permissions.is_staff]

    set_downscaled_serializer_class = serializers.ResourceDownscaledSerializer

    @extend_schema(
        summary="Set paused flag for resource",
        description="Sets the 'paused' flag for a resource. Requires staff permissions.",
        request=serializers.ResourcePausedSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_paused(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_paused = serializer.validated_data["paused"]
        old_paused = resource.paused
        if new_paused != old_paused:
            resource.paused = new_paused
            with reversion.create_revision():
                resource.save()
                reversion.set_user(request.user)
                reversion.set_comment(
                    f"Paused changed from {old_paused} to {new_paused}"
                )
            logger.info(
                "%s has changed paused from %s to %s for resource %s",
                request.user.full_name,
                old_paused,
                new_paused,
                resource.uuid,
            )
            return Response(
                {"status": _("Resource paused flag has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource paused flag is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_paused_permissions = [structure_permissions.is_staff]

    set_paused_serializer_class = serializers.ResourcePausedSerializer

    @extend_schema(
        summary="Set restrict member access flag",
        description="Sets the 'restrict_member_access' flag for a resource. Requires staff permissions.",
        request=serializers.ResourceRestrictMemberAccessSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_restrict_member_access(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_restrict = serializer.validated_data["restrict_member_access"]
        old_restrict = resource.restrict_member_access
        if new_restrict != old_restrict:
            resource.restrict_member_access = new_restrict
            with reversion.create_revision():
                resource.save()
                reversion.set_user(request.user)
                reversion.set_comment(
                    f"Restrict member access changed from {old_restrict} to {new_restrict}"
                )
            logger.info(
                "%s has changed restrict_member_access from %s to %s for resource %s",
                request.user.full_name,
                old_restrict,
                new_restrict,
                resource.uuid,
            )
            return Response(
                {"status": _("Resource restrict_member_access flag has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource restrict_member_access flag is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_restrict_member_access_permissions = [structure_permissions.is_staff]

    set_restrict_member_access_serializer_class = (
        serializers.ResourceRestrictMemberAccessSerializer
    )

    @extend_schema(
        summary="Adjust resource start and end dates (staff only)",
        description=(
            "Updates both the originating order's start_date and the resource's "
            "end_date in one atomic operation. Intended for helpdesk-style prepaid "
            "offerings where staff need to shift the service window forward. "
            "Does not regenerate invoices, issue credits, or send notifications."
        ),
        request=serializers.AdjustResourceDatesSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def adjust_dates(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start_date = serializer.validated_data["start_date"]
        end_date = serializer.validated_data["end_date"]
        comment = serializer.validated_data.get("comment", "")

        allowed_states = (
            ResourceStates.CREATING,
            ResourceStates.UPDATING,
            ResourceStates.OK,
            ResourceStates.ERRED,
        )
        if resource.state not in allowed_states:
            raise rf_exceptions.ValidationError(
                _(
                    "Dates can only be adjusted on resources in OK, ERRED, "
                    "CREATING, or UPDATING state."
                )
            )

        if not resource.offering.components.filter(is_prepaid=True).exists():
            raise rf_exceptions.ValidationError(
                _("Action is only available for prepaid resources.")
            )

        with transaction.atomic():
            resource = models.Resource.objects.select_for_update().get(pk=resource.pk)
            creation_order = (
                models.Order.objects.select_for_update()
                .filter(resource=resource, type=OrderTypes.CREATE)
                .order_by("created")
                .first()
            )
            with reversion.create_revision():
                if creation_order is not None:
                    creation_order.start_date = start_date
                    creation_order.save(update_fields=["start_date"])
                resource.end_date = end_date
                resource.end_date_requested_by = request.user
                resource.save(update_fields=["end_date", "end_date_requested_by"])
                reversion.set_user(request.user)
                reversion.set_comment(
                    comment
                    or f"Staff adjusted dates: start_date={start_date}, end_date={end_date}"
                )

        template = (
            "End date of marketplace resource %(resource_name)s has been"
            " adjusted by staff. End date: %(end_date)s. User: %(user)s."
        )
        log.log_resource_end_date_has_been_updated(resource, request.user, template)
        logger.info(
            "%s adjusted dates of resource %s to start=%s end=%s",
            request.user.full_name,
            resource.uuid,
            start_date,
            end_date,
        )
        return Response(
            {"status": _("Resource dates have been adjusted.")},
            status=status.HTTP_200_OK,
        )

    adjust_dates_permissions = [structure_permissions.is_staff]

    adjust_dates_serializer_class = serializers.AdjustResourceDatesSerializer

    def _set_end_date(self, request, is_staff_action):
        resource: models.Resource = self.get_object()
        if not is_staff_action:
            check_end_date_change_for_prepaid(resource, request)
        serializer = serializers.ResourceEndDateByProviderSerializer(
            data=request.data, instance=resource, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        transaction.on_commit(
            lambda: tasks.notify_about_resource_termination.delay(
                resource.uuid.hex, request.user.uuid.hex, is_staff_action
            )
        )

        if not is_staff_action:
            template = (
                "End date of marketplace resource %(resource_name)s has been updated by provider."
                " End date: %(end_date)s."
                " User: %(user)s."
            )
        else:
            template = (
                "End date of marketplace resource %(resource_name)s has been updated by staff."
                " End date: %(end_date)s."
                " User: %(user)s."
            )
        log.log_resource_end_date_has_been_updated(resource, request.user, template)

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Set end date of the resource by staff",
        description="Deprecated: Use set_end_date instead. Allows a staff user to set or update the end date for a resource.",
        request=serializers.ResourceEndDateByProviderSerializer,
        responses={status.HTTP_200_OK: None},
        deprecated=True,
    )
    @action(detail=True, methods=["post"])
    def set_end_date_by_staff(self, request, uuid=None):
        return self._set_end_date(request, True)

    set_end_date_by_staff_permissions = [structure_permissions.is_staff]

    def _set_end_date_v2(self, request, template):
        resource: models.Resource = self.get_object()
        check_end_date_change_for_prepaid(resource, request)

        serializer = serializers.ResourceEndDateSerializer(
            data=request.data, instance=resource, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        transaction.on_commit(
            lambda: tasks.notify_about_resource_termination.delay(
                resource.uuid.hex, request.user.uuid.hex, False
            )
        )
        log.log_resource_end_date_has_been_updated(resource, request.user, template)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get GLauth user configuration for a resource",
        description="""
        This endpoint provides a GLauth configuration file for the users associated with the project of this resource.
        It is intended for use by an external agent to synchronize user data from Waldur to GLauth.
        """,
        request=None,
        responses={status.HTTP_200_OK: str},
        parameters=[],
    )
    @action(
        detail=True,
        methods=["get"],
        renderer_classes=[PlainTextRenderer],
    )
    def glauth_users_config(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        offering = resource.offering

        if not offering.plugin_options.get(
            "service_provider_can_create_offering_user", False
        ):
            logger.warning(
                "Offering %s doesn't have feature service_provider_can_create_offering_user enabled, skipping GLauth config generation",
                offering,
            )
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data="Offering %s doesn't have feature service_provider_can_create_offering_user enabled"
                % offering,
            )

        _stamp_glauth_integration_status(offering, request)
        response_text = _render_glauth_toml(offering, resource_filter=resource)
        return Response(response_text)

    @extend_schema(
        summary="Get structured GLauth tree for a resource",
        description=(
            "Structured JSON tree (offering, groups, users, robot accounts) "
            "scoped to one resource's project. Source of truth for the "
            "`glauth_users_config` TOML on this viewset."
        ),
        request=None,
        responses={status.HTTP_200_OK: serializers.GlauthTreeSerializer},
        parameters=[],
    )
    @action(detail=True, methods=["get"])
    def glauth_tree(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        offering = resource.offering
        if not offering.plugin_options.get(
            "service_provider_can_create_offering_user", False
        ):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data=(
                    "Offering %s doesn't have feature "
                    "service_provider_can_create_offering_user enabled" % offering
                ),
            )
        _stamp_glauth_integration_status(offering, request)
        tree = utils.build_glauth_tree(offering, resource_filter=resource)
        serializer = serializers.GlauthTreeSerializer(_strip_internal(tree))
        return Response(serializer.data)

    @extend_schema(
        summary="List offerings for sub-resources",
        description="Returns a list of offerings that can be provisioned as sub-resources of the current resource.",
        filters=False,
        responses=serializers.SubresourceOfferingSerializer(many=True),
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def offering_for_subresources(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        # scope is None for unlinked/terminated resources and offerings
        # without backend integration — GenericKeyMixin managers cannot
        # filter by scope=None.
        if not resource.scope:
            return Response([])

        scope = (
            structure_models.ServiceSettings.objects.filter(
                scope=resource.scope
            ).first()
            or resource.scope
        )

        offerings = models.Offering.objects.filter(scope=scope)
        result = [
            {"uuid": offering.uuid.hex, "type": offering.type} for offering in offerings
        ]
        return Response(result)

    @extend_schema(
        summary="Pull resource data",
        description="Schedules a task to pull the latest data for the resource from its backend.",
        request=None,
        responses={
            202: {
                "type": "object",
                "properties": {"detail": {"type": "string"}},
            }
        },
    )
    @action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        pull_executor = plugins.manager.get_pull_resource_executor(
            resource.offering.type
        )
        if not pull_executor:
            return Response(
                {"detail": _("Pull operation is not implemented.")},
                status=status.HTTP_409_CONFLICT,
            )

        match resource.scope:
            case scope if (
                scope is None or resource.offering.type == SITE_AGENT_OFFERING
            ):
                # 1. Case when Waldur doesn't have direct access to the resource backend
                # 2. Case when the resource scope used to be managed by Waldur
                # and now is managed by the Site Agent plugin
                pull_executor.execute(resource)
            case scope if isinstance(scope, structure_models.BaseResource):
                # Case when Waldur has direct access to the backend resource
                pull_executor.execute(scope)

        return Response(
            {"detail": _("Pull operation was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_validators = [
        core_validators.StateValidator(ResourceStates.OK, ResourceStates.ERRED),
        structure_views.check_resource_backend_id,
    ]

    @extend_schema(
        summary="Update resource options",
        description="Updates the options of a resource. If the offering is configured to create orders for option changes, a new UPDATE order will be created. Otherwise, the options are updated directly.",
        request=serializers.ResourceOptionsSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
            status.HTTP_201_CREATED: serializers.OrderUUIDSerializer,
            status.HTTP_409_CONFLICT: None,
        },
    )
    @action(detail=True, methods=["post"])
    def update_options(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data, instance=resource)
        serializer.is_valid(raise_exception=True)

        # Check if offering requires order creation for option changes
        if resource.offering.plugin_options.get(
            "create_orders_on_resource_option_change"
        ):
            # Store old options for comparison
            old_options = resource.options or {}
            new_options = serializer.validated_data.get("options", {})

            # Create order for option change
            return self.create_resource_order(
                request=request,
                resource=resource,
                plan=resource.plan,
                type=OrderTypes.UPDATE,
                attributes={"old_options": old_options, "new_options": new_options},
            )
        else:
            # Direct update without order
            serializer.save()
            return Response(
                {"status": _("Resource options are submitted")},
                status=status.HTTP_200_OK,
            )

    update_options_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_OPTIONS,
            ["project", "project.customer", "offering.customer"],
        ),
        permissions.check_order_creation_permission_for_options,
    ]
    update_options_serializer_class = serializers.ResourceOptionsSerializer
    update_options_validators = [
        core_validators.StateValidator(ResourceStates.OK, state_enum=ResourceStates),
    ]


def check_prepaid_resource(resource):
    if not resource.offering.components.filter(is_prepaid=True).exists():
        raise ValidationError(_("This action is only available for prepaid resources."))


def check_end_date_change_for_prepaid(resource, request):
    """For prepaid resources, only staff can manually change the end date."""
    if (
        resource.offering.components.filter(is_prepaid=True).exists()
        and not request.user.is_staff
    ):
        raise ValidationError(
            _(
                "Only staff can manually change the termination date of a prepaid resource. "
                "Use the renewal action to extend the subscription period."
            )
        )


@extend_schema_view(
    list=extend_schema(
        summary="List consumer resources",
        description="Returns a paginated list of resources accessible to the current user as a service consumer.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a consumer resource",
        description="Returns details of a specific resource accessible to the consumer.",
    ),
    update=extend_schema(
        summary="Update a consumer resource",
        description="Updates the name, description, or end date of a resource.",
    ),
    partial_update=extend_schema(
        summary="Partially update a consumer resource",
        description="Partially updates the name, description, or end date of a resource.",
    ),
)
class ConsumerResourceViewSet(UserRoleMixin, BaseResourceViewSet):
    # Conceal resources whose offering opted into subnet-based concealment from
    # callers outside the resource's access subnets (consumer API only).
    filter_backends = BaseResourceViewSet.filter_backends + (
        filters.ResourceAccessSubnetConcealmentFilterBackend,
    )

    def get_queryset(self):
        queryset = self.queryset.filter_for_service_consumer(self.request.user)
        queryset = filter_queryset_by_user_ip(queryset, self.request)
        # Avoid N+1 queries when serializing offering fields (image, thumbnail, etc.)
        queryset = queryset.select_related(
            "offering",
            "offering__category",
            "offering__customer",
            "offering__parent",
            "project",
            "project__customer",
            "plan",
        )
        # Lets the portal offer key management without knowing the backend, at the
        # cost of one subquery rather than a query per row.
        queryset = queryset.annotate(
            has_api_keys_annotation=Exists(
                models.ResourceApiKey.objects.filter(resource=OuterRef("pk"))
            )
        )
        return queryset

    @extend_schema(
        summary="Get resource team",
        description=(
            "Returns project users for this resource, including project roles and "
            "offering-specific usernames. Use has_consent=true to list only users "
            "with active Terms of Service consent for the offering."
        ),
        request=None,
        responses=serializers.ProjectUserSerializer(many=True),
        filters=False,
        parameters=[
            OpenApiParameter(
                name="has_consent",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description="When true, return only users who have active consent for this offering.",
                required=False,
            ),
        ],
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def team(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        users = resource.project.get_users()
        if request.query_params.get("has_consent", "").lower() == "true":
            users = utils.filter_users_with_active_offering_consent(
                users, resource.offering
            )
        return utils.build_resource_team_response(resource, request, users)

    def get_user_roles_queryset(self, scope, user=None):
        """Return UserRoles scoped to this resource AND all its resource projects."""
        resource_ct = ContentType.objects.get_for_model(scope)
        project_ct = ContentType.objects.get_for_model(models.ResourceProject)
        project_ids = scope.projects.values_list("id", flat=True)

        qs = UserRole.objects.filter(
            Q(content_type=resource_ct, object_id=scope.id)
            | Q(content_type=project_ct, object_id__in=project_ids),
            is_active=True,
            user__is_active=True,
        ).select_related("role", "user", "created_by")
        if user:
            qs = qs.filter(user=user)
        return qs

    @extend_schema(
        summary="List team members of a resource",
        description=(
            "One row per user (deduplicated) with their direct Resource role "
            "and a nested `resource_projects[]` array of their per-ResourceProject "
            "grants under this resource. Mirrors the org-level "
            "`customers/{uuid}/users/` shape so the frontend can render an "
            "expandable per-user view."
        ),
        responses={200: serializers.ResourceTeamMemberSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def team_members(self, request, uuid=None):
        resource = self.get_object()
        if not self.can_view_scope_team(request.user, resource):
            raise PermissionDenied(
                "You do not have permission to list team members of this resource."
            )
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        rp_by_id = {
            rp.id: rp
            for rp in models.ResourceProject.available_objects.filter(resource=resource)
        }
        rp_ids = list(rp_by_id)
        users = (
            User.objects.filter(
                Q(
                    userrole__content_type=resource_ct,
                    userrole__object_id=resource.id,
                )
                | Q(
                    userrole__content_type=rp_ct,
                    userrole__object_id__in=rp_ids,
                ),
                userrole__is_active=True,
                is_active=True,
            )
            .distinct()
            .order_by("username")
        )
        search_string = request.query_params.get(
            "search_string"
        ) or request.query_params.get("user_keyword")
        if search_string:
            users = users.filter(
                Q(first_name__icontains=search_string)
                | Q(last_name__icontains=search_string)
                | Q(email__icontains=search_string)
                | Q(username__icontains=search_string)
            ).distinct()
        page = self.paginate_queryset(users)
        context = {**self.get_serializer_context(), "resource": resource}

        # Bulk-load every role grant for the page's users in two queries
        # (resource scope + resource_project scope) grouped by user, so the
        # per-member serializer fields resolve from these maps instead of
        # querying UserRole once per member (mirrors
        # utils.build_resource_team_response). Priming each RP grant's
        # generic-FK scope from rp_by_id keeps the nested serializer's
        # scope.* reads from fanning out too.
        page_user_ids = [user.id for user in page]
        resource_roles_by_user: dict = {}
        for grant in (
            UserRole.objects.filter(
                content_type=resource_ct,
                object_id=resource.id,
                user_id__in=page_user_ids,
                is_active=True,
            )
            .select_related("role")
            .order_by("role__name")
        ):
            # Prime the generic-FK scope (all these grants point at this
            # resource) so MemberSyncFieldsMixin._sync_row's scope check
            # doesn't fetch the Resource once per grant.
            grant.scope = resource
            resource_roles_by_user.setdefault(grant.user_id, []).append(grant)

        rp_roles_by_user: dict = {}
        for grant in (
            UserRole.objects.filter(
                content_type=rp_ct,
                object_id__in=rp_ids,
                user_id__in=page_user_ids,
                is_active=True,
            )
            .select_related("role")
            .order_by("role__name")
        ):
            grant.scope = rp_by_id.get(grant.object_id)
            rp_roles_by_user.setdefault(grant.user_id, []).append(grant)

        context["resource_roles_by_user"] = resource_roles_by_user
        context["rp_roles_by_user"] = rp_roles_by_user

        # Agent-reported per-grant sync state, opt-in per offering. The
        # index is keyed exactly like MemberSyncFieldsMixin._sync_row
        # looks it up; when the flag is off the context key stays absent
        # and the serializer omits the sync fields entirely, keeping the
        # opted-out response shape (and query cost) unchanged.
        if (resource.offering.plugin_options or {}).get(
            "enable_membership_sync_status"
        ):
            context["member_sync_index"] = {
                (
                    row.user_id,
                    row.scope_type,
                    row.resource_project_id,
                    row.role_name,
                ): row
                for row in models.ResourceMemberSyncStatus.objects.filter(
                    resource=resource
                )
            }
        serializer = serializers.ResourceTeamMemberSerializer(
            page,
            many=True,
            context=context,
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="Set end date of the resource",
        description="Allows a consumer (customer owner) to set or update the end date for a resource.",
        request=serializers.ResourceEndDateSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def set_end_date(self, request, uuid=None):
        template = (
            "End date of marketplace resource %(resource_name)s has been updated by consumer."
            " End date: %(end_date)s."
            " User: %(user)s."
        )
        return self._set_end_date_v2(request, template)

    set_end_date_permissions = [permissions.user_can_set_end_date_as_consumer]
    set_end_date_serializer_class = serializers.ResourceEndDateSerializer

    @extend_schema(
        summary="Suggest a resource name",
        description=(
            "Generates a suggested name for a new resource based on the project and offering. "
            "If the offering has a `resource_name_pattern` in `plugin_options`, "
            "it is used as a Python format string with variables: "
            "`{customer_name}`, `{customer_slug}`, `{project_name}`, `{project_slug}`, "
            "`{offering_name}`, `{offering_slug}`, `{plan_name}`, `{counter}`, "
            "and `{attributes[KEY]}` for any order form value."
        ),
        request=serializers.ResourceSuggestNameSerializer,
        responses={200: {"type": "object", "properties": {"name": {"type": "string"}}}},
        examples=[
            OpenApiExample(
                "Suggest a name for a new resource",
                value={
                    "project": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "offering": "b2c3d4e5-f678-9012-3456-7890abcdef12",
                },
                response_only=False,
                request_only=True,
            ),
            OpenApiExample(
                "Example response with suggested name",
                value={"name": "customer-slug-project-slug-offering-slug-1"},
                response_only=True,
                request_only=False,
            ),
        ],
    )
    @action(detail=False, methods=["post"])
    def suggest_name(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project: structure_models.Project = serializer.validated_data["project"]
        offering: models.Offering = serializer.validated_data["offering"]
        plan = serializer.validated_data.get("plan")
        attributes = serializer.validated_data.get("attributes") or {}
        return Response(
            {
                "name": utils.generate_resource_name(
                    project, offering, plan=plan, attributes=attributes
                )
            }
        )

    suggest_name_serializer_class = serializers.ResourceSuggestNameSerializer

    @extend_schema(
        summary="Switch resource plan",
        description="Creates a marketplace order to switch the billing plan for a resource. This action is asynchronous and may require approval.",
        request=serializers.ResourceSwitchPlanSerializer,
        responses=serializers.OrderUUIDSerializer,
    )
    @action(detail=True, methods=["post"])
    def switch_plan(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data["plan"]

        return self.create_resource_order(
            request=request,
            resource=resource,
            old_plan=resource.plan,
            plan=plan,
            type=OrderTypes.UPDATE,
            limits=resource.limits or {},
        )

    switch_plan_serializer_class = serializers.ResourceSwitchPlanSerializer

    @extend_schema(
        summary="Update resource limits",
        description="Creates a marketplace order to update the limits (e.g., CPU, RAM) for a resource. This action is asynchronous and may require approval.",
        request=serializers.ResourceUpdateLimitsSerializer,
        responses=serializers.OrderUUIDSerializer,
        examples=[
            OpenApiExample(
                "Update resource limits",
                value={"limits": {"cpu": 4, "ram_gb": 8}},
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def update_limits(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        limits = serializer.validated_data["limits"]
        request_comment = serializer.validated_data.get("request_comment", "")
        attachment = serializer.validated_data.get("attachment")

        if resource.limits == limits:
            raise ValidationError(
                "Impossible to create update orders with limits set to exactly the same."
            )

        utils.validate_limits(limits, resource.offering, resource)

        return self.create_resource_order(
            request=request,
            resource=resource,
            plan=resource.plan,
            type=OrderTypes.UPDATE,
            limits=limits,
            attributes={"old_limits": resource.limits},
            request_comment=request_comment,
            attachment=attachment,
        )

    @extend_schema(
        summary="Reallocate resource limits",
        description="Creates marketplace orders to reallocate limits from source resource to target resources.",
        request=serializers.ResourceReallocateLimitsSerializer,
        responses=serializers.ResourceReallocateLimitsResponseSerializer,
        examples=[
            OpenApiExample(
                "Reallocate limits",
                value={
                    "limits": {"cores": 2, "ram": 4},
                    "targets": [
                        {
                            "resource_uuid": "550e8400-e29b-41d4-a716-446655440000",
                            "allocated_limits": {"cores": 1, "ram": 2},
                        },
                        {
                            "resource_uuid": "660e8400-e29b-41d4-a716-446655440001",
                            "allocated_limits": {"cores": 1, "ram": 2},
                        },
                    ],
                },
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def reallocate_limits(self, request, uuid=None):
        source_resource = cast(models.Resource, self.get_object())

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        limits_to_reallocate = serializer.validated_data["limits"]
        targets = serializer.validated_data["targets"]

        utils.validate_reallocation(
            source_resource, limits_to_reallocate, targets, request.user
        )

        source_new_limits = utils.calculate_new_limits(
            source_resource.limits, limits_to_reallocate, subtract=True
        )

        with transaction.atomic():
            source_order = models.Order(
                project=source_resource.project,
                created_by=request.user,
                resource=source_resource,
                offering=source_resource.offering,
                plan=source_resource.plan,
                type=OrderTypes.UPDATE,
                limits=source_new_limits,
                attributes={"old_limits": source_resource.limits},
            )
            serializers.validate_order(source_order, request)
            source_order.init_cost()
            source_order.save()

            # Create target orders (increasing limits)
            target_orders = []
            for target_data in targets:
                target_resource = models.Resource.objects.get(
                    uuid=target_data["resource_uuid"]
                )
                self.ensure_resource_operations_allowed(target_resource)

                # Calculate new limits for target resource
                target_new_limits = utils.calculate_new_limits(
                    target_resource.limits,
                    target_data["allocated_limits"],
                    subtract=False,
                )

                target_order = models.Order(
                    project=target_resource.project,
                    created_by=request.user,
                    resource=target_resource,
                    offering=target_resource.offering,
                    plan=target_resource.plan,
                    type=OrderTypes.UPDATE,
                    limits=target_new_limits,
                    attributes={"old_limits": target_resource.limits},
                )
                serializers.validate_order(target_order, request)
                target_order.init_cost()
                target_order.save()
                target_orders.append(target_order)

        return Response(
            {
                "source_order_uuid": source_order.uuid.hex,
                "target_order_uuids": [order.uuid.hex for order in target_orders],
            }
        )

    reallocate_limits_serializer_class = serializers.ResourceReallocateLimitsSerializer

    update_limits_serializer_class = serializers.ResourceUpdateLimitsSerializer

    switch_plan_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.SWITCH_RESOURCE_PLAN,
            ["project", "project.customer"],
        ),
        permissions.check_order_creation_permission,
    ]
    reallocate_limits_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            ["project", "project.customer"],
        ),
        permissions.check_order_creation_permission,
    ]
    update_limits_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            ["project", "project.customer"],
        ),
        permissions.check_order_creation_permission,
    ]

    switch_plan_validators = [
        core_validators.StateValidator(models.Resource.States.OK),
    ]

    update_limits_validators = [
        core_validators.StateValidator(models.Resource.States.OK),
    ]

    @extend_schema(
        summary="Renew a prepaid resource",
        description="Creates a renewal order to extend the subscription period of a prepaid resource. Optionally, limits can be upgraded at the same time.",
        request=serializers.ResourceRenewSerializer,
        responses={200: serializers.OrderUUIDSerializer},
        examples=[
            OpenApiExample(
                "Renew for 12 months with limit upgrade",
                value={"extension_months": 12, "limits": {"storage": 200}},
            ),
            OpenApiExample(
                "Renew for 6 months without changing limits",
                value={"extension_months": 6},
            ),
        ],
    )
    @action(detail=True, methods=["post"])
    def renew(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())

        serializer = self.get_serializer(
            data=request.data,
            context={"resource": resource},  # Pass resource to serializer context
        )
        serializer.is_valid(raise_exception=True)

        extension_months = serializer.validated_data["extension_months"]
        new_limits = serializer.validated_data.get("limits")  # This can be None
        request_comment = serializer.validated_data.get("request_comment", "")
        attachment = serializer.validated_data.get("attachment")

        # If new limits are not provided, use the resource's current limits for the new period.
        final_limits = new_limits or resource.limits

        # Calculate the new end_date for the resource.
        # It's safest to calculate from the current end_date, or from today if expired.
        current_end_date = resource.end_date or timezone.now().date()
        if current_end_date < timezone.now().date():
            current_end_date = timezone.now().date()

        new_end_date = current_end_date + relativedelta(months=extension_months)

        # Calculate the renewal cost using the new model method.
        renewal_cost = resource.get_renewal_cost(extension_months, new_limits)

        # Create an 'UPDATE' order to handle the renewal.
        # The order processor will be responsible for updating the resource's
        # end_date and limits upon successful payment/approval.
        order_attributes = {
            "name": resource.name,
            "action": "renew",
            "old_limits": resource.limits,
            "old_end_date": resource.end_date.isoformat()
            if resource.end_date
            else None,
            "new_end_date": new_end_date.isoformat(),
            "extension_months": extension_months,
        }

        # The renewal cost is passed as 'switch_price' to the order,
        # which is the mechanism for one-time charges on UPDATE orders.
        response = self.create_resource_order(
            request=request,
            resource=resource,
            plan=resource.plan,
            type=OrderTypes.UPDATE,
            limits=final_limits,
            attributes=order_attributes,
            switch_price=renewal_cost,
            request_comment=request_comment,
            attachment=attachment,
        )

        # Silence all expiring_resource user actions for this resource
        # since a renewal order has been submitted
        from waldur_core.user_actions.models import UserAction

        UserAction.objects.filter(
            action_type="expiring_resource",
            resource_uuid=resource.uuid,
            is_silenced=False,
        ).update(is_silenced=True, silenced_at=timezone.now())

        return response

    renew_serializer_class = serializers.ResourceRenewSerializer

    renew_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,  # Re-use existing permission
            ["project", "project.customer"],
        ),
        permissions.check_order_creation_permission,
    ]

    renew_validators = [
        core_validators.StateValidator(ResourceStates.OK, ResourceStates.ERRED),
        check_prepaid_resource,
    ]

    @extend_schema(
        summary="Estimate renewal cost breakdown",
        request=serializers.RenewalEstimateRequestSerializer,
        responses={200: serializers.RenewalEstimateResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def estimate_renewal(self, request, uuid=None):
        resource = self.get_object()
        serializer = serializers.RenewalEstimateRequestSerializer(
            data=request.data, context={"resource": resource}
        )
        serializer.is_valid(raise_exception=True)
        estimate = resource.get_renewal_estimate(
            serializer.validated_data["extension_months"],
            serializer.validated_data.get("limits"),
        )
        response_serializer = serializers.RenewalEstimateResponseSerializer(estimate)
        return Response(response_serializer.data)

    estimate_renewal_serializer_class = serializers.RenewalEstimateRequestSerializer

    estimate_renewal_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            ["project", "project.customer"],
        ),
    ]

    estimate_renewal_validators = [
        core_validators.StateValidator(ResourceStates.OK, ResourceStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List provider resources",
        description="Returns a paginated list of resources for offerings managed by the current user as a service provider.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a provider resource",
        description="Returns details of a specific resource from a provider's perspective.",
    ),
    update=extend_schema(
        summary="Update a provider resource",
        description="Updates the name or description of a resource. Requires provider permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a provider resource",
        description="Partially updates the name or description of a resource. Requires provider permissions.",
    ),
)
class ProviderResourceViewSet(UserRoleMixin, BaseResourceViewSet):
    def get_queryset(self):
        # Avoid N+1 queries when serializing offering fields (image, thumbnail, etc.)
        queryset = self.queryset.filter_for_service_provider(
            self.request.user
        ).select_related(
            "offering",
            "offering__category",
            "offering__customer",
            "offering__parent",
            "project",
            "project__customer",
            "plan",
        )
        # Same annotation as the consumer viewset: this list shares
        # ResourceSerializer, so without it every row falls back to a per-instance
        # api_keys.exists() query.
        return queryset.annotate(
            has_api_keys_annotation=Exists(
                models.ResourceApiKey.objects.filter(resource=OuterRef("pk"))
            )
        )

    @extend_schema(
        summary="Get resource team",
        description=(
            "Returns project users for this resource from the service provider "
            "perspective. When ENFORCE_USER_CONSENT_FOR_OFFERINGS is enabled and the "
            "offering has active Terms of Service, only users with active consent are "
            "returned (staff and support still see the full team)."
        ),
        request=None,
        responses=serializers.ProjectUserSerializer(many=True),
        filters=False,
        parameters=[
            OpenApiParameter(
                name="has_consent",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description=(
                    "When ENFORCE_USER_CONSENT_FOR_OFFERINGS is disabled, passing true "
                    "returns only users who have active consent for this offering."
                ),
                required=False,
            ),
        ],
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def team(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        offering = resource.offering
        users = resource.project.get_users()
        if utils.should_filter_provider_resource_team_by_consent(
            request.user, offering
        ):
            users = utils.filter_users_with_active_offering_consent(users, offering)
        elif request.query_params.get("has_consent", "").lower() == "true":
            users = utils.filter_users_with_active_offering_consent(users, offering)
        return utils.build_resource_team_response(resource, request, users)

    @extend_schema(
        summary="Set end date by provider",
        description="Deprecated: Use set_end_date instead. Allows a service provider to set or update the end date for a resource.",
        request=serializers.ResourceEndDateByProviderSerializer,
        responses={200: None},
        deprecated=True,
    )
    @action(detail=True, methods=["post"])
    def set_end_date_by_provider(self, request, uuid=None):
        return self._set_end_date(request, False)

    set_end_date_by_provider_permissions = [
        permissions.user_can_set_end_date_by_provider
    ]

    @extend_schema(
        summary="Set end date of the resource",
        description="Allows a service provider to set or update the end date for a resource.",
        request=serializers.ResourceEndDateSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def set_end_date(self, request, uuid=None):
        template = (
            "End date of marketplace resource %(resource_name)s has been updated by provider."
            " End date: %(end_date)s."
            " User: %(user)s."
        )
        return self._set_end_date_v2(request, template)

    set_end_date_permissions = [permissions.user_can_set_end_date_as_provider]
    set_end_date_serializer_class = serializers.ResourceEndDateSerializer

    @extend_schema(
        summary="Set resource backend ID",
        description="Allows a service provider to set or update the backend ID for a resource, linking it to an external system's identifier.",
        request=serializers.ResourceBackendIDSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data["backend_id"]
        utils.validate_backend_id(
            new_backend_id, resource.offering, exclude_resource=resource
        )
        old_backend_id = resource.backend_id
        if new_backend_id != old_backend_id:
            resource.backend_id = serializer.validated_data["backend_id"]
            resource.save()
            logger.info(
                "%s has changed backend_id from %s to %s",
                request.user.full_name,
                old_backend_id,
                new_backend_id,
            )

            return Response(
                {"status": _("Resource backend_id has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource backend_id is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_backend_id_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_ID,
            ["offering", "offering.customer"],
        )
    ]
    set_backend_id_serializer_class = serializers.ResourceBackendIDSerializer

    @extend_schema(
        summary="Set resource effective ID",
        description="Allows a service provider to set or update the effective ID for a resource. The effective ID represents the backend identifier assigned by a downstream provider in federated Waldur deployments.",
        request=serializers.ResourceEffectiveIDSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_effective_id(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_effective_id = serializer.validated_data["effective_id"]
        old_effective_id = resource.effective_id
        if new_effective_id != old_effective_id:
            resource.effective_id = new_effective_id
            resource.save(update_fields=["effective_id"])
            logger.info(
                "%s has changed effective_id from %s to %s",
                request.user.full_name,
                old_effective_id,
                new_effective_id,
            )
            return Response(
                {"status": _("Resource effective_id has been changed.")},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": _("Resource effective_id is not changed.")},
                status=status.HTTP_200_OK,
            )

    set_effective_id_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_ID,
            ["offering", "offering.customer"],
        )
    ]
    set_effective_id_serializer_class = serializers.ResourceEffectiveIDSerializer

    @extend_schema(
        summary="Update resource options directly",
        description="Allows a service provider to directly update the options of a resource without creating an order. This is typically used for administrative changes or backend synchronization.",
        request=serializers.ResourceOptionsSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def update_options_direct(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data, instance=resource)
        serializer.is_valid(raise_exception=True)
        # Always update options directly without creating orders
        serializer.save()
        return Response(
            {"status": _("Resource options have been updated directly.")},
            status=status.HTTP_200_OK,
        )

    update_options_direct_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_OPTIONS,
            ["offering.customer"],
        )
    ]
    update_options_direct_serializer_class = serializers.ResourceOptionsSerializer

    @extend_schema(
        summary="Submit a report for a resource",
        description="Allows a service provider to submit a report (e.g., usage or status report) for a resource.",
        request=serializers.ResourceReportSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def submit_report(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource.report = serializer.validated_data["report"]
        resource.save(update_fields=["report"])

        return Response({"status": _("Report is submitted")}, status=status.HTTP_200_OK)

    submit_report_permissions = [
        permission_factory(
            PermissionEnum.SUBMIT_RESOURCE_REPORT,
            ["offering.customer"],
        )
    ]
    submit_report_serializer_class = serializers.ResourceReportSerializer

    @extend_schema(
        summary="Set resource state to OK",
        description="Allows a service provider to manually set the resource state to OK. This is useful for recovering from Erred state.",
        methods=["POST"],
        request=None,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_state_ok(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        resource.set_state_ok()
        resource.save(update_fields=["state"])
        return Response(
            {"status": _("Resource state has been set to OK.")},
            status=status.HTTP_200_OK,
        )

    set_state_ok_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            # A site agent runs as OFFERING.MANAGER (offering-scoped role),
            # so accept the offering scope alongside the owning customer.
            ["offering", "offering.customer"],
        )
    ]
    set_state_ok_validators = [
        core_validators.StateValidator(
            ResourceStates.ERRED,
            ResourceStates.CREATING,
            ResourceStates.UPDATING,
            ResourceStates.TERMINATING,
            state_enum=ResourceStates,
        )
    ]

    @extend_schema(
        summary="Set resource backend metadata",
        description="Allows a service provider to set or update the backend-specific metadata for a resource.",
        request=serializers.ResourceBackendMetadataSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_backend_metadata(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        resource.backend_metadata = serializer.validated_data["backend_metadata"]
        resource.save()

        return Response(
            {"status": _("The backend metadata is updated")}, status=status.HTTP_200_OK
        )

    set_backend_metadata_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_METADATA,
            ["offering.customer"],
        )
    ]

    set_backend_metadata_serializer_class = (
        serializers.ResourceBackendMetadataSerializer
    )

    @extend_schema(
        summary="Report per-member sync statuses for a resource",
        description=(
            "Full-replace report from the site agent: replaces every "
            "previously stored member sync status of this resource with "
            "the submitted set. Requires the offering to opt in via the "
            "enable_membership_sync_status plugin option. Entries whose "
            "user cannot be resolved are skipped and echoed back in the "
            "response instead of failing the whole report."
        ),
        request=serializers.MemberSyncStatusReportSerializer,
        responses={
            status.HTTP_200_OK: serializers.MemberSyncStatusReportResultSerializer,
            status.HTTP_409_CONFLICT: serializers.DetailResponseSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_membership_sync_statuses(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        if not (resource.offering.plugin_options or {}).get(
            "enable_membership_sync_status"
        ):
            return Response(
                {"detail": "Membership sync status is not enabled for this offering."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rp_by_uuid = {
            rp.uuid.hex: rp
            for rp in models.ResourceProject.available_objects.filter(resource=resource)
        }

        # Resolve every referenced user up front with two bulk queries
        # (by UUID, then by username) rather than one or two User lookups
        # per reported entry.
        statuses = serializer.validated_data["statuses"]
        users_by_uuid = {
            user.uuid.hex: user
            for user in User.objects.filter(
                uuid__in={e["user_uuid"].hex for e in statuses if e.get("user_uuid")}
            )
        }
        users_by_username = {
            user.username: user
            for user in User.objects.filter(
                username__in={e["username"] for e in statuses if e.get("username")}
            )
        }

        rows = []
        skipped = []
        for entry in statuses:
            user = None
            if entry.get("user_uuid"):
                user = users_by_uuid.get(entry["user_uuid"].hex)
            if user is None and entry.get("username"):
                user = users_by_username.get(entry["username"])
            if user is None:
                skipped.append(entry.get("username") or entry["user_uuid"].hex)
                continue
            resource_project = None
            if (
                entry["scope_type"]
                == models.ResourceMemberSyncStatus.ScopeTypes.RESOURCE_PROJECT
            ):
                resource_project = rp_by_uuid.get(entry["resource_project_uuid"].hex)
                if resource_project is None:
                    skipped.append(entry["resource_project_uuid"].hex)
                    continue
            rows.append(
                models.ResourceMemberSyncStatus(
                    resource=resource,
                    user=user,
                    scope_type=entry["scope_type"],
                    resource_project=resource_project,
                    role_name=entry["role_name"],
                    state=entry["state"],
                    message=entry.get("message", ""),
                )
            )

        with transaction.atomic():
            models.ResourceMemberSyncStatus.objects.filter(resource=resource).delete()
            models.ResourceMemberSyncStatus.objects.bulk_create(rows)

        result = serializers.MemberSyncStatusReportResultSerializer(
            {"stored": len(rows), "skipped": skipped}
        )
        return Response(result.data, status=status.HTTP_200_OK)

    # Reuses the backend-metadata permission at both offering and
    # customer scope: sync statuses are agent-reported backend state,
    # and the offering path lets an OFFERING.MANAGER-scoped agent
    # identity report without customer-wide rights.
    set_membership_sync_statuses_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_METADATA,
            ["offering", "offering.customer"],
        )
    ]

    set_membership_sync_statuses_serializer_class = (
        serializers.MemberSyncStatusReportSerializer
    )

    @extend_schema(
        summary="Set resource access endpoints",
        description="Allows a service provider to replace the set of access "
        "endpoints (name + URL) reported for a resource. Used to surface "
        "dynamic per-resource endpoints (e.g. an inference API) in the UI.",
        request=serializers.ResourceEndpointsSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_endpoints(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            resource.endpoints.all().delete()
            for endpoint in serializer.validated_data["endpoints"]:
                models.ResourceAccessEndpoint.objects.create(
                    resource=resource,
                    name=endpoint["name"],
                    url=endpoint["url"],
                )

        return Response(
            {"status": _("The access endpoints are updated")},
            status=status.HTTP_200_OK,
        )

    set_endpoints_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_BACKEND_METADATA,
            ["offering.customer"],
        )
    ]

    set_endpoints_serializer_class = serializers.ResourceEndpointsSerializer

    @extend_schema(
        summary="Set resource state to erred",
        description="Allows a service provider to manually set the state of a resource to 'erred'. An error message and traceback can be provided.",
        request=serializers.ResourceSetStateErredSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def set_as_erred(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("error_message"):
            resource.error_message = serializer.validated_data["error_message"]

        if serializer.validated_data.get("error_traceback"):
            resource.error_traceback = serializer.validated_data["error_traceback"]

        resource.set_state_erred()
        resource.save()

        if resource.scope and hasattr(resource.scope, "set_erred"):
            resource.scope.set_erred()
            resource.scope.save()

        return Response(status=status.HTTP_200_OK)

    set_as_erred_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            # A site agent runs as OFFERING.MANAGER (offering-scoped role),
            # so accept the offering scope alongside the owning customer.
            ["offering", "offering.customer"],
        )
    ]

    set_as_erred_serializer_class = serializers.ResourceSetStateErredSerializer

    @extend_schema(
        summary="Set resource state to OK",
        description="Allows a service provider to manually set the state of a resource to 'OK', clearing any previous error messages.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def set_as_ok(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        if resource.state == ResourceStates.OK:
            logger.warning("Resource %s is already in OK state", resource)
        else:
            resource.set_state_ok()
            resource.error_message = ""
            resource.error_traceback = ""
            resource.save()

            if resource.scope and hasattr(resource.scope, "set_ok"):
                resource.scope.set_ok()
                resource.scope.save()

        return Response(status=status.HTTP_200_OK)

    set_as_ok_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]

    @extend_schema(
        summary="Refresh last sync time",
        description="Updates the 'last_sync' timestamp for a resource to the current time. This is useful for backend agents to signal that a resource is being actively monitored.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def refresh_last_sync(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        resource.last_sync = timezone.now()
        resource.save(update_fields=["last_sync"])
        return Response(status=status.HTTP_200_OK)

    refresh_last_sync_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]

    @extend_schema(
        summary="Set resource limits",
        description="Allows a service provider to directly set the limits for a resource. This is typically used for administrative changes or backend synchronization, bypassing the normal order process.",
        request=serializers.ResourceSetLimitsSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_limits(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_limits = dict(serializer.validated_data["limits"])

        # Unlike the order path, this action performs no key validation, so an
        # agent can push backend-native keys that do not correspond to any
        # offering component (e.g. an inference backend reporting "max_tokens"
        # while the offering only declares "token_cost"). Such orphan keys later
        # crash limit formatting on resource update. Reject them: this is a
        # configuration skew between the agent and the offering that must not
        # pass unnoticed. The agent has no error handling for set_limits 4xx and
        # will mark the resource as ERRED, which is the intended signal that the
        # agent's limit reporting needs fixing. (This differs from the
        # periodic-policy echo handling below, which deliberately absorbs a
        # benign inflated value for a *known* component instead of erroring.)
        known_component_types = set(
            resource.offering.components.values_list("type", flat=True)
        )
        unknown_types = set(new_limits) - known_component_types
        if unknown_types:
            raise ValidationError(
                {
                    "limits": _("Unknown component types: %s")
                    % ", ".join(sorted(unknown_types))
                }
            )

        limit_based_components = resource.offering.components.filter(
            billing_type=BillingTypes.LIMIT
        )

        # When a SLURM periodic usage policy is active on the offering, the
        # policy computes the backend-side limit from resource.limits with a
        # grace_ratio multiplier baked in. If we accept set_limits writes
        # for the same component, the agent's reverse-sync echoes that
        # inflated value back into resource.limits, and the next policy
        # cycle inflates it again — geometric growth per round-trip.
        # Drop offending entries silently rather than rejecting: the agent
        # has no error-handling for set_limits 4xx and would mark the
        # resource as ERRED.
        has_active_periodic_policy = SlurmPeriodicUsagePolicy.objects.filter(
            scope=resource.offering
        ).exists()
        ignored_components = {}
        if has_active_periodic_policy:
            for component in limit_based_components:
                if component.type not in new_limits:
                    continue
                old_value = resource.limits.get(component.type)
                new_value = new_limits[component.type]
                if old_value == new_value:
                    continue
                ignored_components[component.type] = {
                    "from": old_value,
                    "to": new_value,
                }
                if old_value is None:
                    del new_limits[component.type]
                else:
                    new_limits[component.type] = old_value
            if ignored_components:
                logger.warning(
                    "Ignoring set_limits change(s) on resource %s for LIMIT-typed "
                    "components governed by an active SlurmPeriodicUsagePolicy: %s. "
                    "Use an update_limits order to change them.",
                    resource,
                    ignored_components,
                )

        for component in limit_based_components:
            if (
                component.type in resource.limits
                and component.type in new_limits
                and new_limits[component.type] != resource.limits[component.type]
            ) or (
                component.type not in resource.limits and component.type in new_limits
            ):
                logger.warning(
                    "Limit of the limit based component %s has been changed for resource %s from %s to %s",
                    component.type,
                    resource,
                    resource.limits.get(component.type),
                    new_limits.get(component.type),
                )

        resource.limits = new_limits
        resource.save(update_fields=["limits"])

        return Response(
            {"status": _("The resource limits are updated")}, status=status.HTTP_200_OK
        )

    set_limits_serializer_class = serializers.ResourceSetLimitsSerializer

    set_limits_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List offerings for a specific resource category",
        description="""
        Returns a paginated list of offerings that belong to a specified category and are associated with at least one active resource accessible to the current user.
        This endpoint is useful for finding other offerings in the same category as a user's existing resources.
        """,
        parameters=[
            OpenApiParameter(
                name="category_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                required=True,
                description="The UUID of the category to filter offerings by.",
            ),
            OpenApiParameter(
                name="name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by offering name (case-insensitive partial match).",
            ),
        ],
    )
)
class ResourceOfferingsViewSet(ListAPIView):
    serializer_class = serializers.ResourceOfferingSerializer
    queryset = models.Offering.objects.all()  # used by OpenAPI introspector
    filterset_class = structure_filters.NameFilterSet

    def get_category(self):
        if "category_uuid" not in self.kwargs:
            raise rf_exceptions.ValidationError("Category UUID is required.")
        category_uuid = self.kwargs["category_uuid"]
        if not is_uuid_like(category_uuid):
            raise rf_exceptions.ValidationError("Category UUID is invalid.")
        return get_object_or_404(models.Category, uuid=category_uuid)

    def get_queryset(self):
        user = self.request.user
        category = self.get_category()
        qs = cast(ResourceQuerySet, models.Resource.objects.all())
        offerings = (
            qs.filter_for_service_consumer(user)
            .filter(offering__category=category)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("offering_id", flat=True)
        )
        return models.Offering.objects.filter(pk__in=offerings)


class RuntimeStatesViewSet(generics.GenericAPIView):
    filter_backends = []
    pagination_class = None

    @extend_schema(
        summary="List available runtime states for resources",
        description="""
        Returns a unique, sorted list of runtime states for all resources accessible to the current user.
        The runtime state is a backend-specific state of a resource (e.g., 'ACTIVE', 'SHUTOFF' for a VM).
        This endpoint is useful for building dynamic filters in a user interface.
        The list can be optionally filtered by project or category.
        """,
        parameters=[
            OpenApiParameter(
                name="project_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter runtime states by resources within a specific project.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                name="category_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter runtime states by resources belonging to a specific category.",
                extensions={"x-waldur-operation-id": "marketplace_categories_retrieve"},
            ),
            OpenApiParameter(
                name="offering_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter runtime states by resources of a specific offering.",
                extensions={
                    "x-waldur-operation-id": "marketplace_provider_offerings_retrieve"
                },
            ),
        ],
        request=None,
        responses={
            200: serializers.RuntimeStatesSerializer(many=True),
        },
        examples=[
            OpenApiExample(
                "Example response for runtime states",
                summary="A list of unique runtime states found for the user's resources.",
                description="The response is a list of objects, each containing a `value` (the raw state from the backend) and a `label` (a lowercase version for display).",
                value=[
                    {"value": "ACTIVE", "label": "active"},
                    {"value": "BUILDING", "label": "building"},
                    {"value": "SHUTOFF", "label": "shutoff"},
                ],
            )
        ],
    )
    def get(self, request, **kwargs):
        projects = filter_queryset_for_user(
            structure_models.Project.objects.all(), request.user
        )
        project_uuid = request.query_params.get("project_uuid")
        if project_uuid and is_uuid_like(project_uuid):
            project = get_object_or_404(projects, uuid=project_uuid)
            resources = models.Resource.objects.filter(project=project)
        else:
            resources = models.Resource.objects.filter(project__in=projects)
        category_uuid = request.query_params.get("category_uuid")
        if category_uuid and is_uuid_like(category_uuid):
            resources = resources.filter(offering__category__uuid=category_uuid)
        offering_uuid = request.query_params.get("offering_uuid")
        if offering_uuid and is_uuid_like(offering_uuid):
            # A service provider rarely holds a role in the consumer projects its
            # resources live in, so the project-based scope above would hide every
            # runtime state of its own offering. Add back the resources of this
            # offering that are visible from the provider side.
            provider_resource_ids = (
                models.Resource.objects.filter(offering__uuid=offering_uuid)
                .filter_for_service_provider(request.user)
                .values("id")
            )
            resources = models.Resource.objects.filter(
                Q(id__in=resources.filter(offering__uuid=offering_uuid).values("id"))
                | Q(id__in=provider_resource_ids)
            )
        runtime_states = set(
            resources.values_list(
                "backend_metadata__runtime_state", flat=True
            ).distinct()
        )
        result = sorted(
            [
                {"value": state, "label": state.lower()}
                for state in runtime_states
                if state
            ],
            key=lambda option: option["value"],
        )
        return Response(result)


@extend_schema_view(
    list=extend_schema(
        summary="List related customers for a service provider",
        description="""
        Returns a paginated list of customers who have consumed resources from a specific service provider.
        This endpoint helps a service provider identify all the organizations that are their clients within the platform.
        The service provider is identified by its own customer UUID.
        """,
        parameters=[
            OpenApiParameter(
                name="customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
                required=True,
                description="The UUID of the service provider's customer profile.",
            ),
            OpenApiParameter(
                name="name",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter related customers by name (case-insensitive partial match).",
            ),
        ],
    )
)
class RelatedCustomersViewSet(ListAPIView):
    serializer_class = structure_serializers.BasicCustomerSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.NameFilterSet
    queryset = structure_models.Customer.objects.all()  # used by OpenAPI introspector

    def get_customer(self):
        if "customer_uuid" not in self.kwargs:
            raise rf_exceptions.ValidationError("Customer UUID is required.")
        customer_uuid = self.kwargs["customer_uuid"]
        if not is_uuid_like(customer_uuid):
            raise rf_exceptions.ValidationError("Customer UUID is invalid.")
        qs = filter_queryset_for_user(
            structure_models.Customer.objects.all(), self.request.user
        )
        return get_object_or_404(qs, uuid=customer_uuid)

    def get_queryset(self):
        customer = self.get_customer()
        qs = models.Resource.objects.all()
        customer_ids = (
            qs.filter_for_service_provider(self.request.user)
            .filter(offering__customer=customer)
            .values_list("project__customer_id", flat=True)
            .distinct()
        )
        return structure_models.Customer.objects.filter(id__in=customer_ids)


@extend_schema_view(
    list=extend_schema(
        summary="List aggregated category component usages",
        description="""
        Returns a paginated list of aggregated component usages for marketplace categories.
        This data is scoped to either a customer or a project and represents the total usage
        of a component type (e.g., total 'CPU hours' used across all resources of a certain category
        within a project).

        The list **must** be filtered by a `scope` parameter (either a customer or project URL).
        """,
    ),
    retrieve=extend_schema(
        summary="Retrieve an aggregated category component usage record",
        description="Returns the details of a single aggregated usage record for a category component, identified by its database ID.",
    ),
)
class CategoryComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    """
    Provides read-only access to aggregated component usage data for marketplace categories.

    This endpoint shows the total usage of a component type (like 'CPU hours') across all resources
    of a particular category within a specific scope (either a customer or a project).
    It is useful for high-level reporting and analytics.
    """

    queryset = models.CategoryComponentUsage.objects.all().order_by(
        "-date", "component__type"
    )
    filter_backends = (
        DjangoFilterBackend,
        filters.CategoryComponentUsageScopeFilterBackend,
    )
    filterset_class = filters.CategoryComponentUsageFilter
    serializer_class = serializers.CategoryComponentUsageSerializer


@extend_schema(
    summary="List monthly component usage summaries globally",
    description=(
        "Returns paginated monthly component usage across all offerings and service providers. "
        "Results are automatically filtered by the user's permissions. "
        "Defaults to the current month if no time filters ('billing_period', 'start', 'end') are provided."
    ),
)
class ComponentUsageMonthlyViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    queryset = models.ComponentUsageMonthly.objects.all()
    serializer_class = serializers.ComponentUsageMonthlySerializer
    filter_backends = (DjangoFilterBackend, OrderingFilter)
    filterset_class = filters.ComponentUsageMonthlyFilter
    ordering_fields = (
        "usage_percent",
        "billing_period",
        "total_consumed",
        "total_allocated",
    )

    def get_queryset(self):
        qs = super().get_queryset()

        if getattr(self, "swagger_fake_view", False):
            return qs

        # Filter offerings by user permissions
        offerings = models.Offering.objects.all().filter_for_user(self.request.user)
        qs = qs.filter(component__offering__in=offerings)

        # Optimize DB queries by pre-fetching related relations used by the Serializer
        qs = qs.select_related(
            "component",
            "component__offering",
            "component__offering__customer",
            "component__offering__category",
        )

        # Safeguard: If no date filters are provided, default to the current month
        # This prevents querying years of data for all offerings if a user hits the endpoint blindly
        params = self.request.query_params
        if not any(k in params for k in ("billing_period", "start", "end")):
            now = timezone.now()
            qs = qs.filter(billing_period=datetime.date(now.year, now.month, 1))

        return qs.order_by(
            "-billing_period", "component__offering__name", "component__name"
        )


@extend_schema_view(
    list=extend_schema(
        summary="List component usage records",
        description="Returns a paginated list of component usage records for resources. This data is used for billing and usage tracking.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a component usage record",
        description="Returns the details of a specific component usage record.",
    ),
)
class ComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = (
        models.ComponentUsage.objects.all()
        .select_related(
            "component",
            "resource__offering__customer",
            "resource__project__customer",
            "plan_period__plan__offering",
        )
        .order_by("-date", "component__type")
    )
    lookup_field = "uuid"
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUsageFilter
    serializer_class = serializers.ComponentUsageSerializer

    @staticmethod
    def _sync_component_usage_total(component_usage: models.ComponentUsage) -> None:
        """If total usage is zero but linked user usages are non-zero, set total to their sum.

        Guards against the case where set_user_usages creates a ComponentUsage container
        with usage=0 before set_usage has run (or when set_usage is skipped), leaving the
        "Total usages" display at 0 while individual user records show correct values.
        """
        if component_usage.usage != 0:
            return
        total = models.ComponentUserUsage.objects.filter(
            component_usage=component_usage
        ).aggregate(total=Sum("usage"))["total"]
        if total and total > 0:
            component_usage.usage = total
            component_usage.save(update_fields=["usage"])

    @extend_schema(
        summary="Set component usage for a resource",
        description="""
        Allows a service provider to report usage for one or more components of a specific resource.
        This endpoint is typically used by backend systems or agents to submit periodic usage data.

        - If a `plan_period` is provided, the usage is associated with that period.
        - If only a `resource` is provided, the system will determine the correct plan period based on the current date.
        - If a usage record for the same resource, component, and billing period already exists, it will be updated. Otherwise, a new record is created.
        """,
        request=serializers.ComponentUsageCreateSerializer,
        responses={status.HTTP_201_CREATED: None},
        examples=[
            OpenApiExample(
                "Report usage for multiple components",
                summary="Example of reporting usage for 'cpu' and 'ram' components.",
                value={
                    "plan_period": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "usages": [
                        {
                            "type": "cpu",
                            "amount": 120.50,
                            "description": "CPU usage for the last period",
                        },
                        {
                            "type": "ram",
                            "amount": 240.00,
                            "description": "RAM usage for the last period",
                            "recurring": True,
                        },
                    ],
                },
            )
        ],
    )
    @extend_schema(responses={status.HTTP_201_CREATED: None})
    @transaction.atomic
    @action(detail=False, methods=["post"])
    def set_usage(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = (
            "plan_period" in serializer.validated_data
            and serializer.validated_data["plan_period"].resource
            or serializer.validated_data["resource"]
        )
        if not has_permission(
            request, PermissionEnum.SET_RESOURCE_USAGE, resource.offering.customer
        ) and not has_permission(
            request, PermissionEnum.SET_RESOURCE_USAGE, resource.offering
        ):
            raise PermissionDenied()
        serializer.save()
        return Response(status=status.HTTP_201_CREATED)

    set_usage_serializer_class = serializers.ComponentUsageCreateSerializer

    @extend_schema(
        summary="Set user-specific component usage",
        description="""
        Allows a service provider to report usage for a specific user associated with a resource's component.
        This is used for detailed, per-user usage tracking within a single resource.

        - If a user-specific usage record already exists for the given component usage, it will be updated.
        - Otherwise, a new record is created.
        """,
        request=serializers.ComponentUserUsageCreateSerializer,
        responses={status.HTTP_201_CREATED: None},
        examples=[
            OpenApiExample(
                "Report usage for a specific user",
                summary="Example of reporting usage for a user identified by their OfferingUser link.",
                value={
                    "user": "http://testserver/api/marketplace-offering-users/a1b2c3d4e5f678901234567890abcdef/",
                    "username": "johndoe",
                    "usage": 50.75,
                },
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def set_user_usage(self, request, *args, **kwargs):
        component_usage: models.ComponentUsage = self.get_object()
        serializer = self.get_serializer(
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data

        # If date is provided, check if we need to use a different ComponentUsage record
        if validated_data.get("date"):
            date_to_use = validated_data["date"]
            local_date = timezone.localtime(date_to_use)
            billing_period = core_utils.month_start(local_date)
            resource = component_usage.resource
            component = component_usage.component

            # Find existing ComponentUsage for the billing period, or create one.
            # Use filter().first() because multiple records may exist when
            # set_usage was called with different plan_periods.
            component_usage = models.ComponentUsage.objects.filter(
                resource=resource,
                component=component,
                billing_period=billing_period,
            ).first()
            if component_usage is None:
                component_usage = models.ComponentUsage.objects.create(
                    resource=resource,
                    component=component,
                    billing_period=billing_period,
                    usage=0,
                    date=date_to_use,
                    description="Created for user usage backfill",
                    recurring=False,
                    modified_by=request.user,
                )

        existing_user_usage = models.ComponentUserUsage.objects.filter(
            component_usage=component_usage, username=validated_data["username"]
        ).first()

        if existing_user_usage is None:
            validated_data_copy = validated_data.copy()
            validated_data_copy.pop(
                "date", None
            )  # Remove date as it's not a field in ComponentUserUsage
            validated_data_copy["component_usage"] = component_usage
            models.ComponentUserUsage.objects.create(**validated_data_copy)
        else:
            existing_user_usage.usage = validated_data["usage"]
            existing_user_usage.save()

        self._sync_component_usage_total(component_usage)
        return Response(status=status.HTTP_201_CREATED)

    set_user_usage_serializer_class = serializers.ComponentUserUsageCreateSerializer

    set_user_usage_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_USAGE,
            ["resource.offering", "resource.offering.customer"],
        )
    ]

    @extend_schema(
        summary="Bulk set user-specific component usages",
        description="""
        Allows a service provider to report usage for multiple users associated with a resource's component
        in a single request. This avoids the need for one API call per user.

        - All usages are processed atomically: if any item fails validation, none are persisted.
        - If a user-specific usage record already exists for the given component usage, it will be updated.
        - Otherwise, a new record is created.
        """,
        request=serializers.ComponentUserUsageBulkCreateSerializer,
        responses={status.HTTP_201_CREATED: None},
        examples=[
            OpenApiExample(
                "Report usage for multiple users",
                summary="Example of reporting usage for multiple users in a single request.",
                value={
                    "usages": [
                        {
                            "username": "user1",
                            "usage": 50.0,
                        },
                        {
                            "username": "user2",
                            "usage": 75.5,
                        },
                    ]
                },
            )
        ],
    )
    @transaction.atomic
    @action(detail=True, methods=["post"])
    def set_user_usages(self, request, *args, **kwargs):
        component_usage: models.ComponentUsage = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        affected_component_usages: set[models.ComponentUsage] = set()

        for item in serializer.validated_data["usages"]:
            target_component_usage = component_usage

            # If date is provided, find or create ComponentUsage for that billing period
            if item.get("date"):
                date_to_use = item["date"]
                local_date = timezone.localtime(date_to_use)
                billing_period = core_utils.month_start(local_date)
                resource = component_usage.resource
                component = component_usage.component

                target_component_usage = models.ComponentUsage.objects.filter(
                    resource=resource,
                    component=component,
                    billing_period=billing_period,
                ).first()
                if target_component_usage is None:
                    target_component_usage = models.ComponentUsage.objects.create(
                        resource=resource,
                        component=component,
                        billing_period=billing_period,
                        usage=0,
                        date=date_to_use,
                        description="Created for user usage backfill",
                        recurring=False,
                        modified_by=request.user,
                    )

            existing_user_usage = models.ComponentUserUsage.objects.filter(
                component_usage=target_component_usage, username=item["username"]
            ).first()

            if existing_user_usage is None:
                item_copy = item.copy()
                item_copy.pop("date", None)
                item_copy["component_usage"] = target_component_usage
                models.ComponentUserUsage.objects.create(**item_copy)
            else:
                existing_user_usage.usage = item["usage"]
                existing_user_usage.save()

            affected_component_usages.add(target_component_usage)

        for cu in affected_component_usages:
            self._sync_component_usage_total(cu)

        return Response(status=status.HTTP_201_CREATED)

    set_user_usages_serializer_class = (
        serializers.ComponentUserUsageBulkCreateSerializer
    )

    set_user_usages_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_USAGE,
            ["resource.offering", "resource.offering.customer"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List user-specific component usages",
        description="""
        Returns a paginated list of component usage records attributed to specific users.
        This provides a granular view of resource consumption, breaking down the total usage of a component
        by individual users.
        """,
    ),
    retrieve=extend_schema(
        summary="Retrieve a user-specific component usage record",
        description="Returns the details of a single user-specific component usage record.",
    ),
)
class ComponentUserUsageViewSet(core_views.ReadOnlyActionsViewSet):
    """
    Provides read-only access to user-specific component usage data.

    This endpoint allows service providers and resource consumers to view detailed
    usage information for each user associated with a resource's components.
    It is useful for detailed billing reports, usage analysis, and per-user
    consumption monitoring.
    """

    lookup_field = "uuid"
    queryset = models.ComponentUserUsage.objects.all().order_by(
        "-component_usage__date", "component_usage__component__type"
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUserUsageFilter
    serializer_class = serializers.ComponentUserUsageSerializer


@extend_schema_view(
    check_signature=extend_schema(
        summary="Check service provider signature",
        description="""
        Validates a signed payload from a service provider. The payload is a JWT token
        signed with the provider's API secret code. This endpoint is used to verify the
        authenticity of a request before processing it.

        The `data` field should contain the JWT token.
        """,
        request=serializers.ServiceProviderSignatureSerializer,
        responses={200: None},
        examples=[
            OpenApiExample(
                "Example signature check request",
                value={
                    "customer": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "data": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
                },
            )
        ],
    ),
    set_usage=extend_schema(
        summary="Set component usage with signature",
        description="""
        Allows a service provider to report usage for resource components using a signed JWT payload.
        This provides a secure way for external systems to submit billing data.

        The `data` field must contain a JWT token that, when decoded, matches the structure of the
        `ComponentUsageCreateSerializer`.
        """,
        request=serializers.ServiceProviderSignatureSerializer,
        responses={201: None},
    ),
)
class MarketplaceAPIViewSet(rf_viewsets.ViewSet):
    """
    Public API endpoints for marketplace interactions, typically used by service providers
    with signature-based authentication.

    Note: These endpoints are intended for backend integrations and are exempt from standard CSRF protection.
    """

    permission_classes = ()
    serializer_class = serializers.ServiceProviderSignatureSerializer

    def get_validated_data(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data["data"]
        dry_run = serializer.validated_data["dry_run"]

        if self.action == "set_usage":
            data_serializer = serializers.ComponentUsageCreateSerializer(
                data=data, context={"request": request}
            )
            data_serializer.is_valid(raise_exception=True)
            if not dry_run:
                data_serializer.save()

        return serializer.validated_data, dry_run

    @extend_schema(responses={status.HTTP_200_OK: None})
    @action(detail=False, methods=["post"])
    @csrf_exempt
    def check_signature(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(responses={status.HTTP_201_CREATED: None})
    @action(detail=False, methods=["post"])
    @csrf_exempt
    def set_usage(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_201_CREATED)


class OfferingFileViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingFile.objects.all().order_by("name")
    filterset_class = filters.OfferingFileFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.OfferingFileSerializer
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        offering = serializer.validated_data["offering"]

        if user.is_staff or (
            offering.customer and offering.customer.has_user(user, CustomerRole.OWNER)
        ):
            return

        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [structure_permissions.is_owner]


def validate_offering_user_state_transition(valid_states, target_state_name):
    """Create a validator for offering user state transitions that returns HTTP 400."""

    def validator(offering_user):
        if offering_user.state not in valid_states:
            states_names = dict(OfferingUserStates.CHOICES)
            valid_states_names = [str(states_names[state]) for state in valid_states]
            raise ValidationError(
                {
                    "detail": f"Cannot transition to {target_state_name} from current state: {offering_user.state}. "
                    f"Valid states for operation: {', '.join(valid_states_names)}."
                }
            )

    return validator


@extend_schema_view(
    list=extend_schema(
        summary="List offering users",
        description="Returns a paginated list of users associated with offerings. The visibility of users depends on the role of the authenticated user. Staff and support can see all users. Service providers can see users of their offerings if the user has consented. Regular users can only see their own offering-user records.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an offering user",
        description="Returns the details of a specific offering-user link. Visibility follows the same rules as the list view.",
    ),
    create=extend_schema(
        summary="Create an offering user",
        description="Associates a user with a specific offering, creating an offering-specific user account. This is typically done by a service provider.",
        request=serializers.OfferingUserSerializer,
        responses={201: serializers.OfferingUserSerializer},
        examples=[
            OpenApiExample(
                "Create an offering user link",
                value={
                    "offering": "http://testserver/api/marketplace-provider-offerings/a1b2c3d4e5f678901234567890abcdef/",
                    "user": "http://testserver/api/users/b2c3d4e5f678901234567890abcdef12/",
                    "username": "johndoe_hpc",
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Delete an offering user",
        description="Removes the association between a user and an offering. This action may trigger backend cleanup processes depending on the offering type.",
    ),
)
class OfferingUsersViewSet(
    UserChecklistMixin,
    ReviewerChecklistMixin,
    core_views.ActionsViewSet,
):
    queryset = models.OfferingUser.objects.all()
    serializer_class = serializers.OfferingUserSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserFilter

    def perform_update(self, serializer):
        instance: models.OfferingUser = serializer.instance

        old_username = instance.username
        new_username = serializer.validated_data.get("username", old_username)

        serializer.save()

        if "username" in serializer.validated_data and old_username != new_username:
            # The home directory is derived from the username (homedir_prefix +
            # username). Under the service_provider username policy it is first
            # computed while the username is still empty, so re-derive it now
            # that the provider has assigned one. An explicit per-user override
            # (a homeDir that no longer matches the derived pattern) is left
            # untouched.
            backend_metadata = instance.backend_metadata or {}
            if "homeDir" in backend_metadata:
                prefix = instance.offering.plugin_options.get(
                    "homedir_prefix", "/home/"
                )
                if backend_metadata.get("homeDir") == f"{prefix}{old_username}":
                    backend_metadata["homeDir"] = f"{prefix}{new_username}"
                    instance.backend_metadata = backend_metadata
                    instance.save(update_fields=["backend_metadata"])
            logger.info(
                "OfferingUser username update via API: offering_user_uuid=%s offering_uuid=%s old_username=%r new_username=%r source_user_uuid=%s",
                instance.uuid.hex,
                instance.offering.uuid.hex,
                old_username,
                new_username,
                getattr(self.request.user, "uuid", None) and self.request.user.uuid.hex,
            )

    @extend_schema(
        summary="Set POSIX attributes for an offering user",
        description=(
            "Override the login shell, home directory, UID and/or primary GID "
            "for a single offering user, taking precedence over the "
            "offering-level defaults / the range allocator. This is the "
            "programmatic equivalent of the 'Edit POSIX attributes' dialog. "
            "The accepted fields are 'login_shell', 'home_directory', "
            "'uidnumber' and 'primarygroup'; all are optional, but at least "
            "one must be provided.\n\n"
            "A UID or primary GID re-points the allocation ledger and must "
            "fall within the POSIX ID pool resolved for the offering: a value "
            "outside that pool's range is rejected with 400, as is a value "
            "already held by another active identity (another account, a "
            "robot account or a group) and any override on an offering for "
            "which no pool resolves. The action is all-or-nothing - a "
            "conflict on the second identifier rolls back the change made for "
            "the first.\n\n"
            "The response 'warnings' list carries only non-fatal advisories "
            "about values that were accepted: the reserved POSIX ids 65534 "
            "and 65535, and values of 2^31 or above, which may break software "
            "using signed 32-bit ids."
        ),
        request=serializers.OfferingUserPosixAttributesSerializer,
        responses={200: serializers.OfferingUserPosixUpdateResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_posix_attributes(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserPosixAttributesSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        warnings = []
        posix_id_overrides = (
            ("uidnumber", posix_ids.UID, "UID"),
            ("primarygroup", posix_ids.GID, "primary GID"),
        )
        # One transaction so a conflict on the second identifier rolls back the
        # identity change made for the first — the action is all-or-nothing.
        with transaction.atomic():
            backend_metadata = offering_user.backend_metadata or {}
            if "login_shell" in data:
                backend_metadata["loginShell"] = data["login_shell"]
            if "home_directory" in data:
                backend_metadata["homeDir"] = data["home_directory"]

            for field, namespace, label in posix_id_overrides:
                if data.get(field) is None:
                    continue
                value = data[field]
                try:
                    posix_ids.set_value(
                        offering_user, namespace, value, offering_user.offering
                    )
                except posix_ids.PosixIdValueConflict:
                    raise rf_exceptions.ValidationError(
                        {
                            field: _(
                                "%(value)s is already allocated to another account."
                            )
                            % {"value": value}
                        }
                    )
                except DjangoValidationError as exc:
                    raise rf_exceptions.ValidationError({field: exc.messages[0]})
                backend_metadata[field] = value
                warnings.extend(posix_ids.posix_value_advisories(label, value))

            offering_user.backend_metadata = backend_metadata
            offering_user.save(update_fields=["backend_metadata"])

        # Echo the just-persisted values from backend_metadata (the source the
        # allocator/GLAuth read). Building the response from the action's input
        # serializer would re-read non-existent model attributes and report null.
        return Response(
            serializers.OfferingUserPosixUpdateResponseSerializer(
                {
                    "uidnumber": backend_metadata.get("uidnumber"),
                    "primarygroup": backend_metadata.get("primarygroup"),
                    "warnings": warnings,
                }
            ).data
        )

    set_posix_attributes_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]
    set_posix_attributes_serializer_class = (
        serializers.OfferingUserPosixAttributesSerializer
    )

    @extend_schema(
        summary="List project group GIDs an offering user belongs to",
        description=(
            "Returns the project group GIDs (shared GIDs that appear in the "
            "user's GLAuth otherGroups) for this offering user."
        ),
        responses={200: serializers.OfferingUserPosixGroupSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def posix_groups(self, request, uuid=None):
        offering_user = self.get_object()
        rows = utils.get_offering_user_posix_groups(offering_user, viewer=request.user)
        serializer = serializers.OfferingUserPosixGroupSerializer(rows, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="List POSIX UID/GID allocations of an offering user",
        description=(
            "Returns the user's POSIX identifiers (UID, primary GID) and, for "
            "each, the POSIX ID pool that tracks it. The pool fields are null "
            "when the value is not tracked by a pool."
        ),
        responses={200: serializers.OfferingUserPosixAllocationSerializer(many=True)},
    )
    @action(detail=True, methods=["get"])
    def posix_allocations(self, request, uuid=None):
        offering_user = self.get_object()
        rows = utils.get_offering_user_posix_allocations(offering_user)
        serializer = serializers.OfferingUserPosixAllocationSerializer(rows, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="List a user's POSIX identities across all their offerings",
        description=(
            "Consolidated view of one user's POSIX identifiers (UID, primary "
            "GID and project group GIDs) across every offering they have an "
            "account on, each with the range it was allocated from. Scoped to "
            "the offering users the requester is allowed to see."
        ),
        parameters=[
            OpenApiParameter(
                "user_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                required=True,
            ),
        ],
        responses={200: serializers.UserPosixIdentitySerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def posix_identities(self, request):
        user_uuid = request.query_params.get("user_uuid")
        if not user_uuid:
            raise rf_exceptions.ValidationError(
                {"user_uuid": _("This query parameter is required.")}
            )
        offering_users = (
            self.filter_queryset(self.get_queryset())
            .filter(user__uuid=user_uuid)
            .select_related("offering", "user")
        )
        rows = utils.get_user_posix_identities(offering_users, viewer=request.user)
        serializer = serializers.UserPosixIdentitySerializer(rows, many=True)
        return Response(serializer.data)

    def _offering_user_or_service_provider_permission(request, view, obj=None):
        """
        Allow access to:
        1. The offering user themselves (user == obj.user)
        2. Service provider staff with UPDATE_OFFERING_USER permission
        """
        # For the initial has_permission check (obj=None), allow all authenticated users
        # The real permission check will happen in has_object_permission with the actual object
        if not obj:
            if not request.user.is_authenticated:
                raise rf_exceptions.PermissionDenied("Authentication required")
            return

        # Check if the current user is the offering user themselves
        if request.user == obj.user:
            return

        # Check if user has service provider permission (customer or offering scope)
        if has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, obj.offering.customer
        ) or has_permission(request, PermissionEnum.UPDATE_OFFERING_USER, obj.offering):
            return

        raise rf_exceptions.PermissionDenied()

    # User checklist permissions (for offering users filling in checklists)
    checklist_permissions = [_offering_user_or_service_provider_permission]
    completion_status_permissions = [_offering_user_or_service_provider_permission]
    submit_answers_permissions = [_offering_user_or_service_provider_permission]

    # Reviewer checklist permissions (for service providers reviewing compliance)
    checklist_review_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]
    completion_review_status_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    def get_checklist_completion(self, obj):
        """Get checklist completion for the given OfferingUser.

        Returns:
            ChecklistCompletion instance or None
        """
        # Get the compliance checklist for this offering
        checklist = obj.offering.compliance_checklist
        if not checklist:
            return None

        try:
            # Get the completion for the checklist
            content_type = ContentType.objects.get_for_model(obj)
            completion = checklist_models.ChecklistCompletion.objects.get(
                scope_content_type=content_type,
                scope_object_id=obj.id,
                checklist=checklist,
            )
            return completion
        except checklist_models.ChecklistCompletion.DoesNotExist:
            return None

    def perform_destroy(self, instance):
        request = self.request
        offering = instance.offering

        if not has_permission(
            request, PermissionEnum.DELETE_OFFERING_USER, offering.customer
        ):
            raise PermissionDenied(_("You do not have permission to delete this user."))
        instance.delete()

    def get_queryset(self):
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            # Staff and support users see all OfferingUsers without any filtering
            queryset = super().get_queryset()
        else:
            queryset = get_allowed_offering_users_for_user(
                self.request.user,
                include_consent_filtering=True,
                action=self.action,
            )
        return self._optimize_for_serialization(queryset)

    def _optimize_for_serialization(self, queryset):
        """Preload what OfferingUserSerializer reads for every row."""
        completion_exists = checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=ContentType.objects.get_for_model(models.OfferingUser),
            scope_object_id=OuterRef("pk"),
            checklist=OuterRef("offering__compliance_checklist"),
        )
        return (
            queryset.select_related(
                "offering__compliance_checklist",
                "offering__user_attribute_config",
                "user",
                "offering__customer",
            )
            .prefetch_related(
                "offering__user_consents", "offering__terms_of_service_configs"
            )
            # Populates the fast path in
            # OfferingUserSerializer.get_has_compliance_checklist, which
            # otherwise costs one existence query per row. An offering with no
            # compliance checklist matches nothing, which is the False the
            # serializer returns for that case anyway.
            .annotate(_compliance_completion_exists=Exists(completion_exists))
        )

    @extend_schema(
        summary="Update restriction status",
        description="Allows a service provider to mark an offering user as restricted or unrestricted. A restricted user may have limited access to the resource.",
        request=serializers.OfferingUserUpdateRestrictionSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_restricted(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserUpdateRestrictionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        offering_user.is_restricted = serializer.validated_data["is_restricted"]
        offering_user.save(update_fields=["is_restricted"])
        event_logger.emit(
            f"Restriction status for user {offering_user.user} in offering {offering_user.offering.name} has been set to {offering_user.is_restricted}.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_RESTRICTION_UPDATED,
            event_context={"offering_user": offering_user},
        )
        return Response(status=status.HTTP_200_OK)

    set_pending_additional_validation_permissions = (
        set_validation_complete_permissions
    ) = set_pending_account_linking_permissions = begin_creating_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Begin creation process",
        description="Transitions the offering user state from 'Requested' or 'Error Creating' to 'Creating'. This is typically used by an agent to signal that the creation process has started.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def begin_creating(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        offering_user.begin_creating()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} creation has begun.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        return Response(status=status.HTTP_200_OK)

    begin_creating_validators = [
        validate_offering_user_state_transition(
            [OfferingUserStates.CREATION_REQUESTED, OfferingUserStates.ERROR_CREATING],
            "CREATING",
        )
    ]

    @extend_schema(
        summary="Set state to Pending Additional Validation",
        description="Transitions the state to 'Pending Additional Validation' and allows a service provider to add a comment and a URL for the user to follow.",
        request=serializers.OfferingUserStateTransitionSerializer,
        responses={200: None},
        examples=[
            OpenApiExample(
                "Request additional validation",
                value={
                    "comment": "Please upload a valid ID to complete the verification.",
                    "comment_url": "https://example.com/upload-id",
                },
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def set_pending_additional_validation(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserStateTransitionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        comment_url = serializer.validated_data.get("comment_url")
        offering_user.set_pending_additional_validation(
            comment=comment, comment_url=comment_url
        )
        offering_user.save(
            update_fields=[
                "state",
                "service_provider_comment",
                "service_provider_comment_url",
            ]
        )
        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} set to pending additional validation.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        return Response(status=status.HTTP_200_OK)

    set_pending_additional_validation_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.CREATING,
                OfferingUserStates.ERROR_CREATING,
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
            ],
            "PENDING_ADDITIONAL_VALIDATION",
        )
    ]

    @extend_schema(
        summary="Set state to Pending Account Linking",
        description="Transitions the state to 'Pending Account Linking' and allows a service provider to add a comment and a URL to guide the user.",
        request=serializers.OfferingUserStateTransitionSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_pending_account_linking(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserStateTransitionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        comment_url = serializer.validated_data.get("comment_url")
        offering_user.set_pending_account_linking(
            comment=comment, comment_url=comment_url
        )
        offering_user.save(
            update_fields=[
                "state",
                "service_provider_comment",
                "service_provider_comment_url",
            ]
        )
        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} set to pending account linking.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_RESTRICTION_UPDATED,
            event_context={"offering_user": offering_user},
        )
        return Response(status=status.HTTP_200_OK)

    set_pending_account_linking_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.CREATING,
                OfferingUserStates.ERROR_CREATING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            ],
            "PENDING_ACCOUNT_LINKING",
        )
    ]

    @extend_schema(
        summary="Set state to Validation Complete",
        description="Transitions the state from a pending validation state to 'OK', indicating that the user has completed the required steps. This clears any service provider comments.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_validation_complete(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_validation_complete()
        offering_user.save(
            update_fields=[
                "state",
                "service_provider_comment",
                "service_provider_comment_url",
            ]
        )
        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} validation completed.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} validation completed by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_validation_complete_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            ],
            "VALIDATION_COMPLETE",
        )
    ]

    @extend_schema(
        summary="Set state to OK",
        description="Manually sets the offering user state to 'OK'. This can be used to recover from an error state or to complete a manual creation process.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_ok(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_ok()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} set to OK.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} set to OK by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_ok_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.CREATION_REQUESTED,
                OfferingUserStates.CREATING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
                OfferingUserStates.ERROR_CREATING,
                OfferingUserStates.ERROR_DELETING,
            ],
            "OK",
        )
    ]

    @extend_schema(
        summary="Set state to Error Creating",
        description="Manually moves the offering user into the 'Error Creating' state. This is typically used by an agent to report a failure during the creation process.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_error_creating(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_error_creating()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} set to error creating state.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.warning(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} set to ERROR_CREATING by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_error_creating_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.CREATION_REQUESTED,
                OfferingUserStates.CREATING,
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            ],
            "ERROR_CREATING",
        )
    ]

    @extend_schema(
        summary="Set state to Error Deleting",
        description="Manually moves the offering user into the 'Error Deleting' state. This is typically used by an agent to report a failure during the deletion process.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_error_deleting(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_error_deleting()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} set to error deleting state.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.warning(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} set to ERROR_DELETING by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_error_deleting_validators = [
        validate_offering_user_state_transition(
            [OfferingUserStates.DELETION_REQUESTED, OfferingUserStates.DELETING],
            "ERROR_DELETING",
        )
    ]

    @extend_schema(
        summary="Set state to Deleted",
        description="Transitions the offering user to the 'Deleted' state, marking the successful completion of the deletion process.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_deleted(self, request, uuid=None):
        """Action to mark an offering user as successfully deleted."""
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_deleted()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} marked as deleted.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} set to DELETED by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_deleted_validators = [
        validate_offering_user_state_transition(
            [OfferingUserStates.DELETING], "DELETED"
        )
    ]

    set_deleted_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Request deletion of an offering user",
        description="Initiates the deletion process for an offering user account by transitioning it to the 'Deletion Requested' state.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def request_deletion(self, request, uuid=None):
        """Action to request deletion of an offering user account."""
        offering_user: models.OfferingUser = self.get_object()
        offering_user.request_deletion()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"Deletion requested for user {offering_user.user} in offering {offering_user.offering.name}.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"Deletion requested for user {offering_user.user.username} in offering {offering_user.offering.name} by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    request_deletion_validators = [
        validate_offering_user_state_transition(
            [OfferingUserStates.OK], "DELETION_REQUESTED"
        )
    ]

    request_deletion_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Begin deletion process",
        description="Transitions the offering user to the 'Deleting' state. This is typically used by an agent to signal that the deletion process has started.",
        request=None,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def set_deleting(self, request, uuid=None):
        """Action to begin the deletion process for an offering user."""
        offering_user: models.OfferingUser = self.get_object()
        offering_user.set_deleting()
        offering_user.save(update_fields=["state"])

        event_logger.emit(
            f"User {offering_user.user} in offering {offering_user.offering.name} deletion process started.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"User {offering_user.user.username} in offering {offering_user.offering.name} set to DELETING by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    set_deleting_validators = [
        validate_offering_user_state_transition(
            [
                OfferingUserStates.DELETION_REQUESTED,
                OfferingUserStates.ERROR_DELETING,
            ],
            "DELETING",
        )
    ]

    set_deleting_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Update service provider comments",
        description="Allows a service provider to update the `service_provider_comment` and `service_provider_comment_url` fields for an offering user. This is often used to provide feedback or instructions during a pending state.",
        request=serializers.OfferingUserServiceProviderCommentSerializer,
        responses=serializers.OfferingUserServiceProviderCommentSerializer,
    )
    @action(detail=True, methods=["patch"])
    def update_comments(self, request, uuid=None):
        """Action for service providers to update comment and comment URL fields."""
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserServiceProviderCommentSerializer(
            offering_user, data=request.data, context={"request": request}, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        event_logger.emit(
            f"Service provider comments updated for user {offering_user.user} in offering {offering_user.offering.name}.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"Service provider comments updated for user {offering_user.user.username} in offering {offering_user.offering.name} by {request.user.username}."
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _check_update_comments_state(request, view, obj=None):
        """Check if offering user is in valid state for updating comments."""
        offering_user = obj or view.get_object()
        allowed_states = [
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.CREATING,
            OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
            OfferingUserStates.PENDING_ACCOUNT_LINKING,
            OfferingUserStates.OK,
            OfferingUserStates.DELETION_REQUESTED,
            OfferingUserStates.DELETING,
            OfferingUserStates.ERROR_CREATING,
            OfferingUserStates.ERROR_DELETING,
        ]
        if offering_user.state not in allowed_states:
            raise PermissionDenied(
                f"Cannot update comments for offering user in state: {offering_user.get_state_display()}"
            )

    update_comments_permissions = [_check_update_comments_state]

    @extend_schema(
        summary="Update runtime state",
        description=(
            "Allows a service provider to set the operational/access state of an offering user. "
            "Unlike the lifecycle state, this can be updated at any time (except when the account is Deleted). "
            "Use this to signal access blockers such as pending Terms of Use acceptance or "
            "pending account linking (e.g. MyAccessID). "
            "Optionally include service_provider_comment and service_provider_comment_url "
            "to explain the change to the user in the same request."
        ),
        request=serializers.OfferingUserUpdateRuntimeStateSerializer,
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_runtime_state(self, request, uuid=None):
        """Action for service providers to update the runtime/operational state."""
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserUpdateRuntimeStateSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        offering_user.runtime_state = serializer.validated_data["runtime_state"]
        update_fields = ["runtime_state"]
        if "service_provider_comment" in serializer.validated_data:
            offering_user.service_provider_comment = serializer.validated_data[
                "service_provider_comment"
            ]
            update_fields.append("service_provider_comment")
        if "service_provider_comment_url" in serializer.validated_data:
            offering_user.service_provider_comment_url = serializer.validated_data[
                "service_provider_comment_url"
            ]
            update_fields.append("service_provider_comment_url")
        offering_user.save(update_fields=update_fields)

        event_logger.emit(
            f"Runtime state for user {offering_user.user} in offering {offering_user.offering.name} "
            f"set to {offering_user.runtime_state}.",
            event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"Runtime state for user {offering_user.user.username} in offering "
            f"{offering_user.offering.name} set to {offering_user.runtime_state} "
            f"by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)

    update_runtime_state_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_USER,
            ["offering.customer", "offering"],
        )
    ]

    @extend_schema(
        summary="Get profile field warnings",
        description=(
            "Returns a mapping of user profile field names to offerings that expose "
            "those fields. When ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS is enabled, "
            "clearing a field listed here would make the user invisible to the "
            "service provider for the associated offerings."
        ),
        request=None,
        responses={200: serializers.ProfileFieldWarningsSerializer},
    )
    @action(detail=False, methods=["get"])
    def profile_field_warnings(self, request):
        if not config.ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS:
            return Response({}, status=status.HTTP_200_OK)

        offering_users = models.OfferingUser.objects.filter(
            user=request.user,
        ).exclude(state=OfferingUserStates.DELETED)

        attr_to_user_fields = utils._build_attribute_to_user_fields()
        result: dict[str, list[dict[str, str]]] = {}

        for offering_user in offering_users:
            offering = offering_user.offering

            has_active_resources = (
                models.Resource.objects.filter(
                    offering=offering,
                )
                .exclude(state=ResourceStates.TERMINATED)
                .exists()
            )

            if not has_active_resources:
                continue

            exposed_attrs = (
                models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
                    offering
                )
            )

            for attr_name in exposed_attrs:
                user_fields = attr_to_user_fields.get(attr_name, [])
                for user_field in user_fields:
                    result.setdefault(user_field, [])
                    offering_info = {
                        "offering_uuid": str(offering.uuid),
                        "offering_name": offering.name,
                    }
                    # Avoid duplicates if multiple attrs map to same field
                    if offering_info not in result[user_field]:
                        result[user_field].append(offering_info)

        return Response(result, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        summary="List checklist completions for offering users",
        description="""
        Returns a paginated list of all checklist completions for offering users that the current user is allowed to see.
        This endpoint is used by service providers to monitor compliance status and by users to see their own required checklists.
        Visibility follows the same rules as the `OfferingUsers` endpoint.
        """,
    ),
    retrieve=extend_schema(
        summary="Retrieve a checklist completion",
        description="Returns the details of a specific checklist completion for an offering user.",
    ),
)
class OfferingUserChecklistCompletionsViewSet(core_views.ReadOnlyActionsViewSet):
    """List all checklist completions for offering users that the current user is allowed to see."""

    queryset = checklist_models.ChecklistCompletion.objects.all()
    serializer_class = serializers.UserChecklistCompletionSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserChecklistCompletionsFilter
    permission_classes = [rf_permissions.IsAuthenticated]

    def get_queryset(self):
        """
        Get all checklist completions for offering users that the current user is allowed to see.
        Uses the same permission logic as OfferingUsersViewSet.
        """
        # Get the base queryset of all checklist completions for OfferingUsers
        content_type = ContentType.objects.get_for_model(models.OfferingUser)

        # Get the allowed OfferingUsers using the shared helper function
        allowed_offering_users = get_allowed_offering_users_for_user(
            self.request.user, include_consent_filtering=True, action="list"
        )

        # Use SubqueryCount per metric to avoid the Cartesian product that
        # plain Count(distinct=True) over multiple multi-valued joins
        # (answers + checklist__questions) produces.
        answers_qs = checklist_models.Answer.objects.filter(completion=OuterRef("pk"))
        questions_qs = checklist_models.Question.objects.filter(
            checklist=OuterRef("checklist_id")
        )
        required_questions_qs = checklist_models.Question.objects.filter(
            checklist=OuterRef("checklist_id"), required=True
        )

        queryset = (
            checklist_models.ChecklistCompletion.objects.filter(
                scope_content_type=content_type,
                scope_object_id__in=Subquery(allowed_offering_users.values("id")),
            )
            .select_related("checklist")
            .annotate(
                # Denominator for completion_percentage — must count questions
                # in the checklist, not Answer rows (which only exist for
                # questions the user has touched).
                total_questions=SubqueryCount(questions_qs),
                answered_answers=SubqueryCount(
                    answers_qs.filter(answer_data__isnull=False)
                ),
                total_required_questions=SubqueryCount(required_questions_qs),
                total_required_answers=SubqueryCount(
                    answers_qs.filter(
                        question__required=True, answer_data__isnull=False
                    )
                ),
            )
            .annotate(
                unanswered_required_questions=ExpressionWrapper(
                    F("total_required_questions") - F("total_required_answers"),
                    output_field=IntegerField(),
                )
            )
        )

        return queryset.order_by("-modified")


class OfferingUserGroupViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingUserGroup.objects.all()
    serializer_class = serializers.OfferingUserGroupDetailsSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserGroupFilter
    create_serializer_class = update_serializer_class = (
        partial_update_serializer_class
    ) = serializers.OfferingUserGroupSerializer

    unsafe_methods_permissions = [permissions.user_can_manage_offering_user_group]

    def get_queryset(self):
        queryset = super().get_queryset()
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            return queryset

        projects = get_connected_projects(current_user)
        customers = get_connected_customers(current_user)

        subquery = (
            Q(projects__customer__in=customers)
            | Q(offering__customer__in=customers)
            | Q(projects__in=projects)
        )
        return queryset.filter(subquery)

    def perform_create(self, serializer):
        offering_group: models.OfferingUserGroup = serializer.save()
        offering = offering_group.offering

        gid = posix_ids.allocate(offering, posix_ids.GID, offering_group)
        if gid is not None:
            offering_group.backend_metadata["gid"] = gid
            offering_group.save(update_fields=["backend_metadata"])
        else:
            logger.warning(
                "No POSIX ID pool configured for offering %s; offering user "
                "group %s created without a gid.",
                offering,
                offering_group.pk,
            )


class ProjectPosixGroupsViewSet(rf_viewsets.ViewSet):
    """Read-only rollup of POSIX group GIDs assigned to a project across all
    offerings — both project-mapped groups and resource/role groups."""

    @extend_schema(
        summary="List POSIX group GIDs assigned to a project",
        description=(
            "Returns every POSIX group GID a project has been assigned, across "
            "all offerings: project-mapped groups (project_group_gid) and "
            "resource / resource-project role groups (role_group_gid). The "
            "project_uuid query parameter is required."
        ),
        parameters=[
            OpenApiParameter(
                "project_uuid",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
            )
        ],
        responses={200: serializers.ProjectPosixGroupSerializer(many=True)},
    )
    def list(self, request):
        project_uuid = request.query_params.get("project_uuid")
        if not project_uuid:
            raise rf_exceptions.ValidationError(
                {"project_uuid": "This query parameter is required."}
            )
        project = get_object_or_404(structure_models.Project, uuid=project_uuid)

        user = request.user
        if not (
            user.is_staff
            or user.is_support
            or project.id in get_connected_projects(user)
            or project.customer_id in get_connected_customers(user)
        ):
            raise rf_exceptions.PermissionDenied()

        rows = utils.get_project_posix_groups(project)
        serializer = serializers.ProjectPosixGroupSerializer(rows, many=True)
        return Response(serializer.data)


class StatsViewSet(EagerLoadMixin, rf_viewsets.GenericViewSet):
    filter_backends = []
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = EmptySerializer

    def get_queryset(self):
        return models.Resource.objects.none()

    @extend_schema(
        responses=serializers.MarketplaceCustomerStatsSerializer(many=True),
        description="Return project count per organization.",
    )
    @action(detail=False, methods=["get"])
    def organization_project_count(self, request, *args, **kwargs):
        data = structure_models.Project.available_objects.values(
            "customer__abbreviation", "customer__name", "customer__uuid"
        ).annotate(count=Count("customer__uuid"))
        serializer = serializers.MarketplaceCustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return user count per nationality.",
        responses=serializers.UserNationalityStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_nationality(self, request):
        stats = (
            core_models.User.objects.values("nationality")
            .annotate(count=Count("nationality"))
            .order_by("-count")
        )
        serializer = serializers.UserNationalityStatsSerializer(stats, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return user count per residence country.",
        responses=serializers.UserResidenceCountryStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_residence_country(self, request):
        stats = (
            core_models.User.objects.values("country_of_residence")
            .annotate(count=Count("country_of_residence"))
            .order_by("-count")
        )
        serializer = serializers.UserResidenceCountryStatsSerializer(stats, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return project creation counts grouped by month.",
        responses=serializers.ProjectCreationTrendSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def project_creation_trend(self, request):
        monthly_counts = (
            structure_models.Project.available_objects.annotate(
                month=TruncMonth("created")
            )
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
        serializer = serializers.ProjectCreationTrendSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return resource creation counts grouped by month.",
        responses=serializers.ProjectCreationTrendSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resource_creation_trend(self, request):
        monthly_counts = (
            models.Resource.objects.annotate(month=TruncMonth("created"))
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
        serializer = serializers.ProjectCreationTrendSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return top service providers by number of active resources.",
        parameters=[
            OpenApiParameter(
                name="limit",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Number of top providers to return. Default is 5.",
            ),
        ],
        responses=serializers.TopServiceProviderByResourcesSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def top_service_providers_by_resources(self, request):
        try:
            limit = int(request.query_params.get("limit", 5))
        except ValueError:
            limit = 5
        result = (
            self.get_active_resources()
            .values(
                customer_uuid=F("offering__customer__uuid"),
                customer_name=F("offering__customer__name"),
            )
            .annotate(
                resources_count=Count("id"),
                projects_count=Count("project_id", distinct=True),
            )
            .order_by("-resources_count")[:limit]
        )
        serializer = serializers.TopServiceProviderByResourcesSerializer(
            result, many=True
        )
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Retrieve statistics about the number of offerings, grouped by category and service provider.",
        responses=serializers.OfferingStatsCounterSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def offerings_counter_stats(self, request):
        excluded_states = (
            OfferingStates.ARCHIVED,
            OfferingStates.DRAFT,
        )
        try:
            offerings_stats = (
                models.Offering.objects.select_related("category", "customer")
                .exclude(state__in=excluded_states)
                .values(
                    category_uuid=F("category__uuid"),
                    category_title=F("category__title"),
                    service_provider_name=F("customer__name"),
                    service_provider_uuid=F("customer__uuid"),
                )
                .annotate(count=Count("uuid", distinct=True))
            )
            serialized_data = serializers.OfferingStatsCounterSerializer(
                offerings_stats, many=True
            ).data

            return Response(
                serialized_data,
                status=status.HTTP_200_OK,
            )
        except models.Offering.DoesNotExist as e:
            logger.error(f"Offerings not found: {str(e)}")
            return Response(
                {"error": "Offerings not found"}, status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Unexpected error fetching offering stats: {str(e)}")
            return Response(
                {"error": "An unexpected error occurred"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @extend_schema(
        description="Return resource count per organization.",
        responses=serializers.MarketplaceCustomerStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def organization_resource_count(self, request, *args, **kwargs):
        data = (
            models.Resource.objects.filter(state=ResourceStates.OK)
            .values(
                "project__customer__abbreviation",
                "project__customer__name",
                "project__customer__uuid",
            )
            .annotate(count=Count("project__customer__uuid"))
        )
        serializer = serializers.MarketplaceCustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @extend_schema(
        description="Return count of customer members.",
        request=None,
        responses=serializers.CustomerMemberCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def customer_member_count(self, request, *args, **kwargs):
        # Pre-compute customer IDs that have active resources (single query)
        customers_with_resources = set(
            models.Resource.objects.filter(
                state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            )
            .values_list("project__customer_id", flat=True)
            .distinct()
        )

        # Pre-aggregate user counts per customer (single query with GROUP BY)
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        user_counts = dict(
            QuotaUsage.objects.filter(
                content_type=customer_ct,
                name="nc_user_count",
            )
            .values("object_id")
            .annotate(total=Sum("delta"))
            .values_list("object_id", "total")
        )

        # Simple query for customers without correlated subqueries
        customers = structure_models.Customer.objects.values(
            "id", "uuid", "name", "abbreviation"
        )

        # Combine results in Python (very fast for reasonable customer counts)
        result = [
            {
                "uuid": c["uuid"],
                "name": c["name"],
                "abbreviation": c["abbreviation"],
                "count": user_counts.get(c["id"]),
                "has_resources": c["id"] in customers_with_resources,
            }
            for c in customers
        ]

        return Response(result)

    @extend_schema(
        description="Return resources limits per offering.",
        responses=serializers.ResourcesLimitsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resources_limits(self, request, *args, **kwargs):
        data = []
        for resource in (
            models.Resource.objects.filter(state=ResourceStates.OK)
            .exclude(limits={})
            .values("offering__uuid", "limits")
        ):
            limits = resource["limits"]

            for name, value in limits.items():
                if value > 0:
                    try:
                        prev = next(
                            filter(
                                lambda x: (
                                    x["offering_uuid"] == resource["offering__uuid"]
                                    and x["name"] == name
                                ),
                                data,
                            )
                        )
                    except StopIteration:
                        prev = None

                    if not prev:
                        data.append(
                            {
                                "offering_uuid": resource["offering__uuid"],
                                "name": name,
                                "value": value,
                            }
                        )
                    else:
                        prev["value"] += value

        return Response(
            self._expand_result_with_information_of_organization_groups(data),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return component usages for current month.",
        responses=serializers.ComponentUsagesStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def component_usages(self, request, *args, **kwargs):
        now = timezone.now()
        data = (
            models.ComponentUsage.objects.filter(
                billing_period__year=now.year, billing_period__month=now.month
            )
            .values("resource__offering__uuid", "component__type")
            .annotate(usage=Sum("usage"))
        )
        serializer = serializers.ComponentUsagesStatsSerializer(data, many=True)
        return Response(
            self._expand_result_with_information_of_organization_groups(
                serializer.data
            ),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return component usages per project.",
        responses=serializers.ComponentUsagesPerProjectSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def component_usages_per_project(self, request, *args, **kwargs):
        now = timezone.now()
        data = (
            models.ComponentUsage.objects.filter(
                billing_period__year=now.year, billing_period__month=now.month
            )
            .annotate(
                project_uuid=F("resource__project__uuid"),
                component_type=F("component__type"),
            )
            .values("project_uuid", "component_type")
            .annotate(usage=Sum("usage"))
        )
        return Response(
            data,
            status=status.HTTP_200_OK,
        )

    # cache for 1 hour
    @method_decorator(cache_page(60 * 60))
    @extend_schema(
        description="Return component usages per month.",
        responses=serializers.ComponentUsagesPerMonthStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def component_usages_per_month(self, request, *args, **kwargs):
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        usages = models.ComponentUsage.objects.filter(
            billing_period__gte=start, billing_period__lte=end
        )

        data = usages.values(
            "resource__offering__uuid",
            "component__type",
            "billing_period__year",
            "billing_period__month",
        ).annotate(usage=Sum("usage"))
        serializer = serializers.ComponentUsagesPerMonthStatsSerializer(data, many=True)
        return Response(
            self._expand_result_with_information_of_organization_groups(
                serializer.data
            ),
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _expand_result_with_information_of_organization_groups(result):
        data_with_organization_groups = []

        # Collect all offering UUIDs and prefetch offerings with related data in bulk
        offering_uuids = {record["offering_uuid"] for record in result}
        offerings = (
            models.Offering.objects.filter(uuid__in=offering_uuids)
            .select_related("customer")
            .prefetch_related("organization_groups")
        )
        # Use string keys to handle both UUID objects and string UUIDs from serializers
        offerings_by_uuid = {str(o.uuid): o for o in offerings}

        for record in result:
            offering = offerings_by_uuid.get(str(record["offering_uuid"]))
            if not offering:
                continue
            record["offering_country"] = offering.country or offering.customer.country
            organization_groups = offering.organization_groups.all()

            if not organization_groups:
                new_data = copy.copy(record)
                new_data["organization_group_name"] = ""
                new_data["organization_group_uuid"] = ""
                data_with_organization_groups.append(new_data)
            else:
                for organization_group in organization_groups:
                    new_data = copy.copy(record)
                    new_data["organization_group_name"] = organization_group.name
                    new_data["organization_group_uuid"] = organization_group.uuid.hex
                    data_with_organization_groups.append(new_data)

        return data_with_organization_groups

    @extend_schema(
        description="Count users of service providers.",
        responses=serializers.CountUsersOfServiceProvidersSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def count_users_of_service_providers(self, request, *args, **kwargs):
        result = []
        for sp in models.ServiceProvider.objects.all().prefetch_related(
            "customer__organization_groups"
        ):
            for group in sp.customer.organization_groups.all():
                # Get base user IDs from projects, we filter users by ToS consent for sp offerings
                user_ids = utils.get_service_provider_user_ids(self.request.user, sp)

                sp_offerings = models.Offering.objects.filter(customer=sp.customer)
                if config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
                    consented_user_ids = (
                        models.UserOfferingConsent.objects.filter(
                            offering__in=sp_offerings,
                            revocation_date__isnull=True,
                        )
                        .values_list("user_id", flat=True)
                        .distinct()
                    )
                    final_user_ids = set(user_ids) & set(consented_user_ids)
                else:
                    final_user_ids = user_ids

                data = {
                    "count": len(final_user_ids),
                    "customer_organization_group_uuid": group.uuid.hex,
                    "customer_organization_group_name": group.name,
                }
                data.update(self._get_service_provider_info(sp))
                result.append(data)

        return Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        description="Count projects of service providers.",
        responses=serializers.CountProjectsOfServiceProvidersSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def count_projects_of_service_providers(self, request, *args, **kwargs):
        result = []

        for sp in models.ServiceProvider.objects.all().prefetch_related(
            "customer__organization_groups"
        ):
            for group in sp.customer.organization_groups.all():
                data = {
                    "count": utils.get_service_provider_project_ids(sp).count(),
                    "customer_organization_group_uuid": group.uuid.hex,
                    "customer_organization_group_name": group.name,
                }
                data.update(self._get_service_provider_info(sp))
                result.append(data)
        return Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        description="Count projects of service providers grouped by OECD.",
        responses=serializers.CountProjectsOfServiceProvidersGroupedByOecdSerializer(
            many=True
        ),
    )
    @action(detail=False, methods=["get"])
    def count_projects_of_service_providers_grouped_by_oecd(
        self, request, *args, **kwargs
    ):
        result = []

        for sp in models.ServiceProvider.objects.all().prefetch_related(
            "customer__organization_groups"
        ):
            for group in sp.customer.organization_groups.all():
                project_ids = utils.get_service_provider_project_ids(sp)
                projects = (
                    structure_models.Project.available_objects.filter(
                        id__in=project_ids
                    )
                    .values("oecd_fos_2007_code")
                    .annotate(count=Count("id"))
                )

                for p in projects:
                    data = {
                        "count": p["count"],
                        "oecd_fos_2007_code": p["oecd_fos_2007_code"],
                        "customer_organization_group_uuid": group.uuid.hex,
                        "customer_organization_group_name": group.name,
                    }
                    data.update(self._get_service_provider_info(sp))
                    result.append(data)

        return Response(
            self._expand_result_with_oecd_name(result), status=status.HTTP_200_OK
        )

    def _projects_usages_grouped_by_field(self, field_name):
        results = {}

        for project in structure_models.Project.objects.all():
            field_value = str(getattr(project, field_name))
            if field_value in results:
                results[field_value]["projects_ids"].append(project.id)
            else:
                results[field_value] = {
                    "projects_ids": [project.id],
                }

        now = timezone.now()

        for key, result in results.items():
            ids = result.pop("projects_ids")
            usages = (
                models.ComponentUsage.objects.filter(
                    billing_period__year=now.year,
                    billing_period__month=now.month,
                    resource__project__id__in=ids,
                )
                .values("component__type")
                .annotate(usage=Sum("usage"))
            )

            for usage in usages:
                result[usage["component__type"]] = usage["usage"]

        return results

    @extend_schema(
        description="Group project usages by OECD code.",
        responses=serializers.ProjectsUsagesGroupedByOecdSerializer,
    )
    @action(detail=False, methods=["get"])
    def projects_usages_grouped_by_oecd(self, request, *args, **kwargs):
        data = self._replace_keys_from_oecd_code_to_oecd_name(
            self._projects_usages_grouped_by_field("oecd_fos_2007_code")
        )
        return Response(
            {"usages": data},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Group project usages by industry flag.",
        responses=serializers.ProjectsUsagesGroupedByIndustryFlagSerializer,
    )
    @action(detail=False, methods=["get"])
    def projects_usages_grouped_by_industry_flag(self, request, *args, **kwargs):
        data = self._projects_usages_grouped_by_field("is_industry")
        return Response(
            {"usages": data},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return user count grouped by authentication method.",
        responses=serializers.UserAuthMethodCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_auth_method_count(self, request, *args, **kwargs):
        users = (
            core_models.User.objects.all()
            .values("registration_method")
            .annotate(count=Count("id"))
        )
        data = []
        for user in users:
            method = user["registration_method"]
            label = get_identity_provider_name(method)
            data.append({"method": label, "count": user["count"]})

        return Response(data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return user count grouped by identity source.",
        responses=serializers.UserIdentitySourceCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_identity_source_count(self, request, *args, **kwargs):
        users = (
            core_models.User.objects.all()
            .values("identity_source")
            .annotate(count=Count("id"))
        )
        data = [
            {"identity_source": user["identity_source"], "count": user["count"]}
            for user in users
        ]
        return Response(data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return user count grouped by organization.",
        responses=serializers.UserOrganizationCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_organization_count(self, request, *args, **kwargs):
        users = (
            core_models.User.objects.exclude(organization="")
            .values("organization")
            .annotate(count=Count("id"))
        )
        data = [
            {"organization": user["organization"], "count": user["count"]}
            for user in users
        ]
        return Response(data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return user count grouped by affiliation.",
        responses=serializers.UserAffiliationCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_affiliation_count(self, request, *args, **kwargs):
        query_set = self._get_affiliation_count_queryset()
        return Response(query_set, status=status.HTTP_200_OK)

    @extend_schema(
        description=(
            "Paginated affiliation rows with parsed organization, country, "
            "category and identifier fields. Drives the affiliation details "
            "table; the unparsed aggregate counts remain available via "
            "user_affiliation_count."
        ),
        parameters=[
            OpenApiParameter(
                name="country",
                type=str,
                location=OpenApiParameter.QUERY,
                description="ISO country code (case-insensitive).",
            ),
            OpenApiParameter(
                name="category",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "One of: home-organization, personal-identifier, "
                    "organization-type, user-status, eduperson, other."
                ),
            ),
            OpenApiParameter(
                name="organization",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Exact organization domain match.",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Substring match against raw URN or organization.",
            ),
            OpenApiParameter(
                name="o",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Ordering field; prefix with - for descending. "
                    "Allowed: count, organization, country, category, affiliation. "
                    "Defaults to -count."
                ),
            ),
        ],
        responses=serializers.UserAffiliationDetailSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_affiliation_details(self, request, *args, **kwargs):
        rows = [
            self._build_affiliation_row(item)
            for item in self._get_affiliation_count_queryset()
        ]
        rows = self._filter_affiliation_rows(rows, request.query_params)
        rows = self._order_affiliation_rows(rows, request.query_params.get("o"))
        page = self.paginate_queryset(rows)
        serializer = serializers.UserAffiliationDetailSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    user_affiliation_details_permissions = [core_permissions.IsSupport]

    @staticmethod
    def _get_affiliation_count_queryset():
        class JsonbArrayElementsText(Func):
            """Custom function to call PostgreSQL jsonb_array_elements_text."""

            function = "jsonb_array_elements_text"
            output_field = CharField()

        return (
            core_models.User.objects.annotate(
                affiliation=JsonbArrayElementsText(
                    F("affiliations"), output_field=CharField()
                )
            )
            .values("affiliation")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

    @staticmethod
    def _build_affiliation_row(item: dict) -> dict:
        parsed = parse_affiliation(item["affiliation"])
        return {
            "affiliation": parsed.raw,
            "organization": parsed.organization,
            "country": parsed.country,
            "category": parsed.category,
            "identifier": parsed.identifier,
            "count": item["count"],
        }

    @staticmethod
    def _filter_affiliation_rows(rows: list[dict], params) -> list[dict]:
        country = (params.get("country") or "").lower() or None
        category = (params.get("category") or "").lower() or None
        organization = params.get("organization") or None
        search = (params.get("search") or "").lower() or None

        def keep(row: dict) -> bool:
            if country and (row["country"] or "").lower() != country:
                return False
            if category and row["category"] != category:
                return False
            if organization and row["organization"] != organization:
                return False
            if search:
                haystack = (row["affiliation"] or "").lower()
                org = (row["organization"] or "").lower()
                if search not in haystack and search not in org:
                    return False
            return True

        return [row for row in rows if keep(row)]

    @staticmethod
    def _order_affiliation_rows(rows: list[dict], ordering: str | None) -> list[dict]:
        allowed = {"count", "organization", "country", "category", "affiliation"}
        field = (ordering or "-count").strip()
        reverse = field.startswith("-")
        key = field.lstrip("-")
        if key not in allowed:
            key, reverse = "count", True

        def sort_key(row: dict):
            value = row.get(key)
            # Secondary key on the raw URN keeps page boundaries deterministic
            # when many rows share the same primary value.
            return (value is None, value or "", row["affiliation"])

        return sorted(rows, key=sort_key, reverse=reverse)

    @extend_schema(
        description="Return user count grouped by organization type (SCHAC URN).",
        responses=serializers.UserOrganizationTypeCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_organization_type_count(self, request, *args, **kwargs):
        users = (
            core_models.User.objects.exclude(organization_type="")
            .values("organization_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        data = [
            {
                "organization_type": user["organization_type"],
                "count": user["count"],
            }
            for user in users
        ]
        return Response(data, status=status.HTTP_200_OK)

    user_organization_type_count_permissions = [core_permissions.IsSupport]

    @extend_schema(
        description="Return user count grouped by job title.",
        responses=serializers.UserJobTitleCountSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def user_job_title_count(self, request, *args, **kwargs):
        users = (
            core_models.User.objects.exclude(job_title="")
            .annotate(normalized_job_title=Trim(Lower("job_title")))
            .values("normalized_job_title")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        data = [
            {
                "job_title": user["normalized_job_title"],
                "count": user["count"],
            }
            for user in users
        ]
        return Response(data, status=status.HTTP_200_OK)

    user_job_title_count_permissions = [core_permissions.IsSupport]

    @extend_schema(
        description="Get resource provisioning statistics.",
        parameters=[
            OpenApiParameter(
                name="last_minutes",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Filter by last N minutes. Default is 60.",
            ),
        ],
        responses=serializers.ResourceProvisioningStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resource_provisioning_stats(self, request, *args, **kwargs):
        try:
            last_minutes = int(request.query_params.get("last_minutes", 60))
        except ValueError:
            raise rf_exceptions.ValidationError("last_minutes must be an integer.")
        cutoff = timezone.now() - datetime.timedelta(minutes=last_minutes)

        # Get completed orders in the time window
        completed_orders = models.Order.objects.filter(
            type=OrderTypes.CREATE,
            state__in=(OrderStates.DONE, OrderStates.ERRED),
            completed_at__gte=cutoff,
        ).select_related("offering", "offering__customer")

        # Get in-progress orders (no time filter, or maybe apply cutoff to created?)
        # For "in progress count", we usually want current snapshot.
        in_progress_orders = models.Order.objects.filter(
            type=OrderTypes.CREATE,
            state__in=models.OrderStates.PENDING_STATES,
        ).select_related("offering", "offering__customer")

        stats = {}

        # Process completed orders
        completed_order_ids = [order.id for order in completed_orders]
        order_ct = ContentType.objects.get_for_model(models.Order)

        feeds = logging_models.Feed.objects.filter(
            content_type=order_ct,
            object_id__in=completed_order_ids,
            event__event_type=EventType.MARKETPLACE_ORDER_APPROVED,
        ).select_related("event")

        order_executing_starts = {feed.object_id: feed.event.created for feed in feeds}

        def get_or_create_entry(offering):
            if offering.uuid not in stats:
                stats[offering.uuid] = {
                    "offering_uuid": offering.uuid,
                    "offering_name": offering.name,
                    "service_provider_uuid": offering.customer.uuid,
                    "service_provider_name": offering.customer.name,
                    "provisioning_count": 0,
                    "provisioning_success_count": 0,
                    "provisioning_error_count": 0,
                    "provisioning_in_progress_count": 0,
                    "total_provisioning_duration": 0.0,
                    "total_pending_duration": 0.0,
                    "duration_count": 0,
                }
            return stats[offering.uuid]

        for order in completed_orders:
            entry = get_or_create_entry(order.offering)
            entry["provisioning_count"] += 1
            if order.state == OrderStates.DONE:
                entry["provisioning_success_count"] += 1
            else:
                entry["provisioning_error_count"] += 1

            start_executing = order_executing_starts.get(order.id)
            if not start_executing:
                if not order.consumer_reviewed_at and not order.provider_reviewed_at:
                    start_executing = order.created
                else:
                    start_executing = (
                        order.provider_reviewed_at
                        or order.consumer_reviewed_at
                        or order.created
                    )

            if start_executing:
                pending_duration = (start_executing - order.created).total_seconds()
                completed_at = order.completed_at or timezone.now()
                provisioning_duration = (completed_at - start_executing).total_seconds()

                entry["total_pending_duration"] += max(0, pending_duration)
                entry["total_provisioning_duration"] += max(0, provisioning_duration)
                entry["duration_count"] += 1

        # Process in-progress orders
        for order in in_progress_orders:
            entry = get_or_create_entry(order.offering)
            entry["provisioning_in_progress_count"] += 1

        results = []
        for entry in stats.values():
            count = entry["duration_count"]
            entry["avg_provisioning_duration"] = (
                entry["total_provisioning_duration"] / count if count else 0
            )
            entry["avg_pending_duration"] = (
                entry["total_pending_duration"] / count if count else 0
            )
            entry["provisioning_success_rate"] = (
                entry["provisioning_success_count"] / entry["provisioning_count"]
                if entry["provisioning_count"]
                else 0
            )

            entry.pop("total_provisioning_duration")
            entry.pop("total_pending_duration")
            entry.pop("duration_count")
            results.append(entry)

        serializer = serializers.ResourceProvisioningStatsSerializer(results, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _projects_limits_grouped_by_field(self, field_name):
        results = {}

        # Single query: join Resource → Project to avoid consecutive DB queries
        resources = (
            models.Resource.objects.filter(state=ResourceStates.OK)
            .exclude(limits={})
            .values(f"project__{field_name}", "limits")
        )

        for resource in resources:
            field_value = str(resource[f"project__{field_name}"])
            if field_value not in results:
                results[field_value] = {}

            for name, value in resource["limits"].items():
                if value > 0:
                    if name in results[field_value]:
                        results[field_value][name] += value
                    else:
                        results[field_value][name] = value

        return results

    @extend_schema(
        description="Group project limits by OECD code.",
        responses=serializers.ProjectsLimitsGroupedByOecdSerializer,
    )
    @action(detail=False, methods=["get"])
    def projects_limits_grouped_by_oecd(self, request, *args, **kwargs):
        data = self._replace_keys_from_oecd_code_to_oecd_name(
            self._projects_limits_grouped_by_field("oecd_fos_2007_code")
        )
        return Response(
            {"limits": data},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Group project limits by industry flag.",
        responses=serializers.ProjectsLimitsGroupedByIndustryFlagSerializer,
    )
    @action(detail=False, methods=["get"])
    def projects_limits_grouped_by_industry_flag(self, request, *args, **kwargs):
        data = self._projects_limits_grouped_by_field("is_industry")
        return Response(
            {"limits": data},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Total cost of active resources per offering.",
        responses=serializers.OfferingCostSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def total_cost_of_active_resources_per_offering(self, request, *args, **kwargs):
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        queryset = (
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start,
                invoice__created__lte=end,
            )
            .exclude(resource__offering__isnull=True)
            .values("resource__offering__uuid", "resource__offering__name")
            .annotate(
                cost=Sum(
                    (Ceil(F("quantity") * F("unit_price") * 100) / 100),
                    output_field=FloatField(),
                )
            )
            .order_by("-cost")
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = serializers.OfferingCostSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = serializers.OfferingCostSerializer(queryset, many=True)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _get_service_provider_info(service_provider):
        customer = service_provider.customer
        # organization_groups = customer.organization_groups.all()
        return {
            "service_provider_uuid": service_provider.uuid.hex,
            "customer_uuid": customer.uuid.hex,
            "customer_name": customer.name,
            # "customer_organization_groups": [
            #     {"uuid": group.uuid.hex, "name": group.name}
            #     for group in organization_groups
            # ],
        }

    @staticmethod
    def _expand_result_with_oecd_name(data):
        if not hasattr(data, "__iter__"):
            return data

        for d in data:
            if not isinstance(d, dict):
                return data

            if "oecd_fos_2007_code" in d.keys():
                name = [
                    c[1]
                    for c in structure_models.Project.OECD_FOS_2007_CODES
                    if c[0] == d["oecd_fos_2007_code"]
                ]
                if name:
                    d["oecd_fos_2007_name"] = name[0]
                else:
                    d["oecd_fos_2007_name"] = ""

        return data

    @staticmethod
    def _replace_keys_from_oecd_code_to_oecd_name(data):
        if not isinstance(data, dict):
            return data

        results = {}
        for code, value in data.items():
            name = [
                c[1]
                for c in structure_models.Project.OECD_FOS_2007_CODES
                if c[0] == code
            ]
            if name:
                results[f"{code} {str(name[0])}"] = value
            else:
                results[code] = value

        return results

    @extend_schema(
        description="Count unique users connected with active resources of service provider.",
        responses=serializers.CountUniqueUsersConnectedWithActiveResourcesOfServiceProviderSerializer(
            many=True
        ),
    )
    @action(detail=False, methods=["get"])
    def count_unique_users_connected_with_active_resources_of_service_provider(
        self, request, *args, **kwargs
    ):
        raw_query = """
            SELECT "customer_uuid", "customer_name", COUNT("user_id") AS "count_users"
            FROM
                (SELECT DISTINCT
                    CUSTOMERS."uuid" AS "customer_uuid",
                    CUSTOMERS."name" AS "customer_name",
                    ROLES."user_id" AS "user_id"
                FROM (
                        SELECT *
                        FROM "marketplace_resource"
                        WHERE "marketplace_resource"."state" IN (%s, %s, %s)
                     ) RESOURCES
                    INNER JOIN "marketplace_offering" OFFERINGS
                        ON (RESOURCES."offering_id" = OFFERINGS."id")
                    INNER JOIN "structure_customer" CUSTOMERS
                        ON (OFFERINGS."customer_id" = CUSTOMERS."id")
                    LEFT JOIN (
                            SELECT *
                            FROM "permissions_userrole"
                            WHERE
                                "permissions_userrole"."content_type_id" = %s
                                AND "permissions_userrole"."is_active"
                            ) ROLES
                        ON (ROLES."object_id" = RESOURCES."project_id")
                ) U0
            GROUP BY "customer_uuid", "customer_name"
        """
        ctype = ContentType.objects.get_for_model(structure_models.Project)

        with connection.cursor() as cursor:
            cursor.execute(
                raw_query,
                [
                    ResourceStates.OK,
                    ResourceStates.UPDATING,
                    ResourceStates.TERMINATING,
                    ctype.id,
                ],
            )
            result = cursor.fetchall()

        return Response(
            list(
                map(
                    lambda x: dict(
                        customer_uuid=x[0].hex, customer_name=x[1], count_users=x[2]
                    ),
                    result,
                )
            ),
            status=status.HTTP_200_OK,
        )

    def get_active_resources(self):
        return models.Resource.objects.filter(
            state__in=(
                ResourceStates.OK,
                ResourceStates.UPDATING,
                ResourceStates.TERMINATING,
            )
        )

    @extend_schema(
        description="Count active resources grouped by offering.",
        responses=serializers.OfferingStatsSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def count_active_resources_grouped_by_offering(self, request, *args, **kwargs):
        queryset = (
            self.get_active_resources()
            .values("offering__uuid", "offering__name", "offering__country")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = serializers.OfferingStatsSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = serializers.OfferingStatsSerializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        responses=serializers.OfferingCountryStatsSerializer(many=True),
        description="Count active resources grouped by offering country.",
    )
    @action(detail=False, methods=["get"])
    def count_active_resources_grouped_by_offering_country(
        self, request, *args, **kwargs
    ):
        result = (
            self.get_active_resources()
            .values("offering__country")
            .annotate(count=Count("id"))
            .order_by()
        )

        return Response(
            serializers.OfferingCountryStatsSerializer(result, many=True).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        responses=serializers.CountStatsSerializer(many=True),
        description="Count active resources grouped by organization group.",
    )
    @action(detail=False, methods=["get"])
    def count_active_resources_grouped_by_organization_group(
        self, request, *args, **kwargs
    ):
        # Single grouped aggregate instead of one COUNT query per organization
        # group (was an N+1). Groups without active resources are naturally
        # excluded because they produce no rows.
        grouped = (
            self.get_active_resources()
            .filter(offering__customer__organization_groups__isnull=False)
            .values(
                "offering__customer__organization_groups__uuid",
                "offering__customer__organization_groups__name",
            )
            .annotate(count=Count("id"))
            .order_by()
        )

        results = [
            {
                "organization_group_uuid": row[
                    "offering__customer__organization_groups__uuid"
                ].hex,
                "organization_group_name": row[
                    "offering__customer__organization_groups__name"
                ],
                "count": row["count"],
            }
            for row in grouped
        ]

        serialized_results = serializers.CountStatsSerializer(results, many=True).data

        return Response(
            serialized_results,
            status=status.HTTP_200_OK,
        )

    def _get_count_projects_with_active_resources_grouped_by_provider_and_field(
        self, grouped_field
    ):
        return (
            structure_models.Project.objects.filter(is_removed=False)
            .filter(
                resource__state__in=(
                    ResourceStates.OK,
                    ResourceStates.UPDATING,
                    ResourceStates.TERMINATING,
                )
            )
            .values(
                "resource__offering__customer__name",
                "resource__offering__customer__abbreviation",
                "resource__offering__customer__uuid",
                grouped_field,
            )
            .annotate(count=Count("id"))
            .order_by("resource__offering__customer__name")
        )

    @extend_schema(
        responses=serializers.CustomerOecdCodeStatsSerializer(many=True),
        description="Count projects grouped by provider and OECD code",
    )
    @action(detail=False, methods=["get"])
    def count_projects_grouped_by_provider_and_oecd(self, request, *args, **kwargs):
        result = self._get_count_projects_with_active_resources_grouped_by_provider_and_field(
            "oecd_fos_2007_code"
        )
        result = self._expand_result_with_oecd_name(result)
        return Response(
            serializers.CustomerOecdCodeStatsSerializer(result, many=True).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        responses=serializers.CustomerIndustryFlagStatsSerializer(many=True),
        description="Count projects grouped by provider and industry flag",
    )
    @action(detail=False, methods=["get"])
    def count_projects_grouped_by_provider_and_industry_flag(
        self, request, *args, **kwargs
    ):
        result = self._get_count_projects_with_active_resources_grouped_by_provider_and_field(
            "is_industry"
        )
        return Response(
            serializers.CustomerIndustryFlagStatsSerializer(result, many=True).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return summary statistics for offering costs.",
        responses=serializers.OfferingCostsSummarySerializer,
    )
    @action(detail=False, methods=["get"])
    def offering_costs_summary(self, request, *args, **kwargs):
        """Return aggregated offering costs summary."""
        from decimal import Decimal

        # Aggregate cost data directly in the database
        result = (
            models.Resource.objects.filter(
                state__in=(
                    ResourceStates.OK,
                    ResourceStates.UPDATING,
                )
            )
            .values("offering__uuid")
            .annotate(cost=Sum("cost"))
            .aggregate(
                total_cost=Coalesce(Sum("cost"), Decimal("0")),
                offering_count=Count("offering__uuid", distinct=True),
            )
        )

        total_cost = result["total_cost"] or Decimal("0")
        offering_count = result["offering_count"] or 0
        average_cost = (
            total_cost / offering_count if offering_count > 0 else Decimal("0")
        )

        data = {
            "total_cost": total_cost,
            "offering_count": offering_count,
            "average_cost": average_cost,
        }

        return Response(
            serializers.OfferingCostsSummarySerializer(data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return summary statistics for resource geographic distribution.",
        responses=serializers.ResourcesGeographySummarySerializer,
    )
    @action(detail=False, methods=["get"])
    def resources_geography_summary(self, request, *args, **kwargs):
        """Return aggregated resource geography summary."""
        active_resources = self.get_active_resources()

        total_resources = active_resources.count()

        # Count distinct countries
        countries_count = (
            active_resources.exclude(offering__country="")
            .values("offering__country")
            .distinct()
            .count()
        )

        # Count distinct organization groups
        # Get customer IDs that have active resources via their offerings
        customer_ids_with_resources = active_resources.values_list(
            "offering__customer_id", flat=True
        ).distinct()
        org_groups_count = (
            structure_models.OrganizationGroup.objects.filter(
                customers__id__in=customer_ids_with_resources
            )
            .distinct()
            .count()
        )

        # Count distinct offerings with resources
        offerings_count = active_resources.values("offering__uuid").distinct().count()

        data = {
            "total_resources": total_resources,
            "countries_count": countries_count,
            "org_groups_count": org_groups_count,
            "offerings_count": offerings_count,
        }

        return Response(
            serializers.ResourcesGeographySummarySerializer(data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return summary statistics for customer members.",
        responses=serializers.CustomerMemberSummarySerializer,
    )
    @action(detail=False, methods=["get"])
    def customer_member_summary(self, request, *args, **kwargs):
        """Return aggregated customer member summary."""
        # Pre-compute customer IDs that have active resources
        customers_with_resources = set(
            models.Resource.objects.filter(
                state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            )
            .values_list("project__customer_id", flat=True)
            .distinct()
        )

        # Pre-aggregate user counts per customer
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        user_counts = dict(
            QuotaUsage.objects.filter(
                content_type=customer_ct,
                name="nc_user_count",
            )
            .values("object_id")
            .annotate(total=Sum("delta"))
            .values_list("object_id", "total")
        )

        # Get all customer IDs
        customer_ids = structure_models.Customer.objects.values_list("id", flat=True)

        total_organizations = len(customer_ids)
        total_members = sum(user_counts.get(cid, 0) or 0 for cid in customer_ids)
        organizations_with_resources = len(customers_with_resources)
        average_members_per_org = (
            round(total_members / total_organizations) if total_organizations > 0 else 0
        )

        data = {
            "total_organizations": total_organizations,
            "total_members": total_members,
            "organizations_with_resources": organizations_with_resources,
            "average_members_per_org": average_members_per_org,
        }

        return Response(
            serializers.CustomerMemberSummarySerializer(data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return summary statistics for project classification.",
        responses=serializers.ProjectClassificationSummarySerializer,
    )
    @action(detail=False, methods=["get"])
    def project_classification_summary(self, request, *args, **kwargs):
        """Return aggregated project classification summary."""
        # Count projects grouped by industry flag
        result = (
            structure_models.Project.objects.filter(is_removed=False)
            .filter(
                resource__state__in=(
                    ResourceStates.OK,
                    ResourceStates.UPDATING,
                    ResourceStates.TERMINATING,
                )
            )
            .values("is_industry")
            .annotate(count=Count("id", distinct=True))
        )

        industry_projects = 0
        academic_projects = 0

        for item in result:
            if item["is_industry"]:
                industry_projects = item["count"]
            else:
                academic_projects = item["count"]

        total_projects = industry_projects + academic_projects

        data = {
            "total_projects": total_projects,
            "academic_projects": academic_projects,
            "industry_projects": industry_projects,
        }

        return Response(
            serializers.ProjectClassificationSummarySerializer(data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Return usage-based resources with no usage reported in the specified billing period.",
        parameters=[
            OpenApiParameter(
                name="billing_period",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Billing period in YYYY-MM format. Defaults to current month.",
            ),
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by service provider UUID.",
            ),
        ],
        responses=serializers.ResourceMissingUsageSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resources_missing_usage(self, request, *args, **kwargs):
        """Return usage-based resources with no usage reported for the billing period."""
        from django.db.models import Max

        billing_period = request.query_params.get("billing_period")
        if billing_period:
            try:
                year, month = map(int, billing_period.split("-"))
                target_year = year
                target_month = month
            except (ValueError, AttributeError):
                raise rf_exceptions.ValidationError(
                    "billing_period must be in YYYY-MM format"
                )
        else:
            now = timezone.now()
            target_year = now.year
            target_month = now.month

        provider_uuid = request.query_params.get("provider_uuid")

        # Get resources with usage-based billing components in OK or Updating state
        queryset = models.Resource.objects.filter(
            offering__components__billing_type=BillingTypes.USAGE,
            state__in=[ResourceStates.OK, ResourceStates.UPDATING],
        ).distinct()

        if provider_uuid:
            queryset = queryset.filter(offering__customer__uuid=provider_uuid)

        # Exclude resources that have usage for this billing period
        queryset = queryset.exclude(
            usages__billing_period__year=target_year,
            usages__billing_period__month=target_month,
        )

        # Add last_usage_date annotation and eager load
        queryset = queryset.annotate(last_usage_date=Max("usages__date")).order_by(
            "-created"
        )

        queryset = serializers.ResourceMissingUsageSerializer.eager_load(queryset)

        page = self.paginate_queryset(queryset)

        serializer = serializers.ResourceMissingUsageSerializer(
            page if page is not None else queryset,
            many=True,
            context={"request": self.request},
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return comprehensive order statistics including daily breakdown, state/type aggregations, and summary stats.",
        parameters=[
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start date in YYYY-MM-DD format. Defaults to 30 days ago.",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End date in YYYY-MM-DD format. Defaults to today.",
            ),
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by service provider UUID.",
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by customer UUID.",
            ),
        ],
        responses=serializers.OrderStatsResponseSerializer,
    )
    @action(detail=False, methods=["get"])
    def order_stats(self, request, *args, **kwargs):
        """Return comprehensive order statistics for reporting."""

        # Parse date parameters
        start_str = request.query_params.get("start")
        end_str = request.query_params.get("end")

        if start_str:
            try:
                start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d").date()
            except ValueError:
                raise rf_exceptions.ValidationError(
                    "start must be in YYYY-MM-DD format"
                )
        else:
            start_date = (timezone.now() - datetime.timedelta(days=30)).date()

        if end_str:
            try:
                end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d").date()
            except ValueError:
                raise rf_exceptions.ValidationError("end must be in YYYY-MM-DD format")
        else:
            end_date = timezone.now().date()

        # Build base queryset
        queryset = models.Order.objects.filter(
            created__date__gte=start_date,
            created__date__lte=end_date,
        )

        # Apply optional filters
        provider_uuid = request.query_params.get("provider_uuid")
        customer_uuid = request.query_params.get("customer_uuid")

        if provider_uuid:
            queryset = queryset.filter(offering__customer__uuid=provider_uuid)
        if customer_uuid:
            queryset = queryset.filter(project__customer__uuid=customer_uuid)

        # Calculate summary stats
        total = queryset.count()
        total_cost = queryset.aggregate(total=Sum("cost"))["total"] or 0

        # Calculate revenue (cost from create/update orders only)
        revenue_queryset = queryset.filter(
            type__in=[OrderTypes.CREATE, OrderTypes.UPDATE]
        )
        total_revenue = revenue_queryset.aggregate(total=Sum("cost"))["total"] or 0

        # State counts using ORM
        state_counts_raw = dict(
            queryset.values("state")
            .annotate(count=Count("id"))
            .values_list("state", "count")
        )
        # Convert integer state keys to string labels
        state_int_to_label = dict(OrderStates.CHOICES)
        state_counts = {
            state_int_to_label.get(state, state): count
            for state, count in state_counts_raw.items()
        }
        # Convert integer type keys to string labels
        type_int_to_label = dict(OrderTypes.CHOICES)
        type_counts_raw = dict(
            queryset.values("type")
            .annotate(count=Count("id"))
            .values_list("type", "count")
        )
        type_counts = {
            type_int_to_label.get(t, t): count for t, count in type_counts_raw.items()
        }

        # Summary breakdown - use string labels since state_counts keys are converted to labels
        summary = {
            "total": total,
            "total_cost": total_cost,
            "total_revenue": total_revenue,
            "pending": (
                state_counts.get("pending-consumer", 0)
                + state_counts.get("pending-provider", 0)
                + state_counts.get("pending-project", 0)
                + state_counts.get("pending-start-date", 0)
            ),
            "executing": state_counts.get("executing", 0),
            "done": state_counts.get("done", 0),
            "erred": state_counts.get("erred", 0),
            "canceled": state_counts.get("canceled", 0),
            "rejected": state_counts.get("rejected", 0),
        }

        # Daily breakdown using ORM aggregation
        daily_stats = list(
            queryset.annotate(date=TruncDate("created"))
            .values("date")
            .annotate(total=Count("id"), total_cost=Sum("cost"))
            .order_by("date")
        )

        # Daily revenue (cost from create/update orders)
        daily_revenue = dict(
            revenue_queryset.annotate(date=TruncDate("created"))
            .values("date")
            .annotate(revenue=Sum("cost"))
            .values_list("date", "revenue")
        )

        # Get state and type breakdown per day efficiently
        daily_state_counts = {}
        daily_type_counts = {}

        state_by_day = (
            queryset.annotate(date=TruncDate("created"))
            .values("date", "state")
            .annotate(count=Count("id"))
        )
        for row in state_by_day:
            date_str = row["date"].isoformat()
            if date_str not in daily_state_counts:
                daily_state_counts[date_str] = {}
            # Convert integer state to string label
            state_label = state_int_to_label.get(row["state"], row["state"])
            daily_state_counts[date_str][state_label] = row["count"]

        type_by_day = (
            queryset.annotate(date=TruncDate("created"))
            .values("date", "type")
            .annotate(count=Count("id"))
        )
        for row in type_by_day:
            date_str = row["date"].isoformat()
            if date_str not in daily_type_counts:
                daily_type_counts[date_str] = {}
            # Convert integer type to string label
            type_label = type_int_to_label.get(row["type"], row["type"])
            daily_type_counts[date_str][type_label] = row["count"]

        # Combine daily data
        daily_data = []
        for row in daily_stats:
            date_str = row["date"].isoformat()
            daily_data.append(
                {
                    "date": row["date"],
                    "total": row["total"],
                    "total_cost": row["total_cost"],
                    "revenue": daily_revenue.get(row["date"]) or 0,
                    "by_state": daily_state_counts.get(date_str, {}),
                    "by_type": daily_type_counts.get(date_str, {}),
                }
            )

        result = {
            "summary": summary,
            "by_state": state_counts,
            "by_type": type_counts,
            "daily": daily_data,
        }

        serializer = serializers.OrderStatsResponseSerializer(result)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return resource statistics for a service provider.",
        parameters=[
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Service provider UUID.",
            ),
        ],
        responses=serializers.ProviderResourceStatsSerializer,
    )
    @action(detail=False, methods=["get"])
    def provider_resources(self, request, *args, **kwargs):
        """Return resource statistics for a service provider."""
        from dateutil.relativedelta import relativedelta

        provider_uuid = request.query_params.get("provider_uuid")
        if not provider_uuid:
            raise rf_exceptions.ValidationError("provider_uuid is required")

        # Get provider
        try:
            provider = models.ServiceProvider.objects.get(uuid=provider_uuid)
        except models.ServiceProvider.DoesNotExist:
            raise rf_exceptions.NotFound("Service provider not found")

        # Base queryset for provider's resources
        queryset = models.Resource.objects.filter(offering__customer=provider.customer)

        # Total and by state
        total = queryset.exclude(state=ResourceStates.TERMINATED).count()
        state_names = dict(models.Resource.States.CHOICES)
        state_counts = {
            state_names[state]: count
            for state, count in queryset.values("state")
            .annotate(count=Count("id"))
            .values_list("state", "count")
        }

        # By offering (top 10)
        by_offering = list(
            queryset.exclude(state=ResourceStates.TERMINATED)
            .values(
                offering_uuid=F("offering__uuid"),
                offering_name=F("offering__name"),
            )
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        # Monthly trend (last 12 months)
        start_date = timezone.now() - relativedelta(months=12)
        monthly = list(
            queryset.filter(created__gte=start_date)
            .annotate(month=TruncMonth("created"))
            .values("month")
            .annotate(count=Count("id"))
            .order_by("month")
        )
        # Convert datetime to string for JSON serialization
        for item in monthly:
            item["month"] = item["month"].strftime("%Y-%m")

        result = {
            "total": total,
            "by_state": state_counts,
            "by_offering": by_offering,
            "monthly": monthly,
        }

        serializer = serializers.ProviderResourceStatsSerializer(result)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return customer statistics for a service provider.",
        parameters=[
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Service provider UUID.",
            ),
        ],
        responses=serializers.ProviderCustomerStatsSerializer,
    )
    @action(detail=False, methods=["get"])
    def provider_customers(self, request, *args, **kwargs):
        """Return customer statistics for a service provider."""
        from dateutil.relativedelta import relativedelta

        provider_uuid = request.query_params.get("provider_uuid")
        if not provider_uuid:
            raise rf_exceptions.ValidationError("provider_uuid is required")

        # Get provider
        try:
            provider = models.ServiceProvider.objects.get(uuid=provider_uuid)
        except models.ServiceProvider.DoesNotExist:
            raise rf_exceptions.NotFound("Service provider not found")

        # Get distinct customers with active resources
        active_resources = models.Resource.objects.filter(
            offering__customer=provider.customer
        ).exclude(state=ResourceStates.TERMINATED)

        customer_ids = active_resources.values_list(
            "project__customer_id", flat=True
        ).distinct()
        total = len(set(customer_ids))

        # New customers this month
        month_start = timezone.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        new_this_month = (
            active_resources.filter(created__gte=month_start)
            .values_list("project__customer_id", flat=True)
            .distinct()
            .count()
        )

        # Top customers by resource count
        top_by_resources = list(
            active_resources.values(
                customer_uuid=F("project__customer__uuid"),
                customer_name=F("project__customer__name"),
            )
            .annotate(resource_count=Count("id"))
            .order_by("-resource_count")[:10]
        )

        # Top customers by revenue (last 12 months)
        start_date = timezone.now() - relativedelta(months=12)
        top_by_revenue = list(
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start_date,
                resource__offering__customer=provider.customer,
            )
            .values(
                customer_uuid=F("invoice__customer__uuid"),
                customer_name=F("invoice__customer__name"),
            )
            .annotate(revenue=Sum(F("unit_price") * F("quantity")))
            .order_by("-revenue")[:10]
        )

        # Monthly customer trend (last 12 months) - customers with resources created that month
        monthly = list(
            active_resources.filter(created__gte=start_date)
            .annotate(month=TruncMonth("created"))
            .values("month")
            .annotate(customer_count=Count("project__customer_id", distinct=True))
            .order_by("month")
        )
        for item in monthly:
            item["month"] = item["month"].strftime("%Y-%m")

        result = {
            "total": total,
            "new_this_month": new_this_month,
            "top_by_revenue": top_by_revenue,
            "top_by_resources": top_by_resources,
            "monthly": monthly,
        }

        serializer = serializers.ProviderCustomerStatsSerializer(result)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return offering performance statistics for a service provider.",
        parameters=[
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Service provider UUID.",
            ),
        ],
        responses=serializers.ProviderOfferingStatsSerializer,
    )
    @action(detail=False, methods=["get"])
    def provider_offerings(self, request, *args, **kwargs):
        """Return offering performance statistics for a service provider."""
        from dateutil.relativedelta import relativedelta

        provider_uuid = request.query_params.get("provider_uuid")
        if not provider_uuid:
            raise rf_exceptions.ValidationError("provider_uuid is required")

        # Get provider
        try:
            provider = models.ServiceProvider.objects.get(uuid=provider_uuid)
        except models.ServiceProvider.DoesNotExist:
            raise rf_exceptions.NotFound("Service provider not found")

        # Get all active/paused offerings for the provider
        offerings = models.Offering.objects.filter(
            customer=provider.customer,
            state__in=(
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
                OfferingStates.UNAVAILABLE,
            ),
        )

        # Calculate stats for each offering
        start_date = timezone.now() - relativedelta(months=12)
        result_offerings = []

        for offering in offerings:
            # Resource counts by state
            resources = models.Resource.objects.filter(offering=offering)
            active_count = resources.filter(
                state__in=(ResourceStates.OK, ResourceStates.UPDATING)
            ).count()
            total_count = resources.exclude(state=ResourceStates.TERMINATED).count()

            # Revenue (last 12 months)
            revenue = (
                invoice_models.InvoiceItem.objects.filter(
                    invoice__created__gte=start_date,
                    resource__offering=offering,
                )
                .aggregate(total=Sum(F("unit_price") * F("quantity")))
                .get("total")
                or 0
            )

            # Plan utilization (if plans have limits)
            plans_data = []
            for plan in offering.plans.filter(archived=False):
                plan_usage = (
                    models.Resource.objects.filter(
                        offering=offering,
                        plan=plan,
                    )
                    .exclude(state=ResourceStates.TERMINATED)
                    .count()
                )

                plans_data.append(
                    {
                        "plan_uuid": str(plan.uuid),
                        "plan_name": plan.name,
                        "usage": plan_usage,
                        "limit": plan.max_amount,
                        "utilization": (
                            round(plan_usage / plan.max_amount * 100, 1)
                            if plan.max_amount
                            else None
                        ),
                    }
                )

            result_offerings.append(
                {
                    "offering_uuid": str(offering.uuid),
                    "offering_name": offering.name,
                    "category_name": offering.category.title
                    if offering.category
                    else None,
                    "state": offering.state,
                    "active_resources": active_count,
                    "total_resources": total_count,
                    "revenue": revenue,
                    "plans": plans_data,
                }
            )

        # Sort by active resources descending
        result_offerings.sort(key=lambda x: x["active_resources"], reverse=True)

        result = {"offerings": result_offerings}
        serializer = serializers.ProviderOfferingStatsSerializer(result)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return component usages grouped by project members' organization type.",
        responses=serializers.ResourceUsageByOrgTypeSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resource_usage_by_organization_type(self, request, *args, **kwargs):
        """
        Aggregate component usages by organization_type of users
        who are members of the resource's project.
        """
        now = timezone.now()
        billing_period = now.date().replace(day=1)
        project_content_type_id = ContentType.objects.get_for_model(
            structure_models.Project
        ).id

        raw_query = """
            SELECT
                COALESCE(u.organization_type, '') as organization_type,
                oc.type as component_type,
                SUM(cu.usage) as usage,
                COUNT(DISTINCT r.id) as resource_count
            FROM marketplace_componentusage cu
            JOIN marketplace_resource r ON cu.resource_id = r.id
            JOIN marketplace_offeringcomponent oc ON cu.component_id = oc.id
            JOIN structure_project p ON r.project_id = p.id
            JOIN permissions_userrole ur ON ur.object_id = p.id
                AND ur.content_type_id = %s
                AND ur.is_active = TRUE
            JOIN core_user u ON ur.user_id = u.id
            WHERE cu.billing_period = %s
            GROUP BY u.organization_type, oc.type
            ORDER BY resource_count DESC
        """

        with connection.cursor() as cursor:
            cursor.execute(raw_query, [project_content_type_id, billing_period])
            columns = [col[0] for col in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        serializer = serializers.ResourceUsageByOrgTypeSerializer(results, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return resource usage statistics grouped by customer.",
        responses=serializers.ResourceUsageByCustomerSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resource_usage_by_customer(self, request, *args, **kwargs):
        """
        Aggregate resource data per customer including:
        - Component usages (current month)
        - Resource limits (sum)
        - Total cost
        - Resource count by state
        """
        now = timezone.now()

        # Get customers with their resource stats
        customers = (
            structure_models.Customer.objects.annotate(
                resources_ok=Count(
                    "projects__resource",
                    filter=Q(projects__resource__state=ResourceStates.OK),
                ),
                resources_erred=Count(
                    "projects__resource",
                    filter=Q(projects__resource__state=ResourceStates.ERRED),
                ),
                resources_total=Count(
                    "projects__resource",
                    filter=~Q(projects__resource__state=ResourceStates.TERMINATED),
                ),
                total_cost=Sum(
                    "projects__resource__cost",
                    filter=Q(
                        projects__resource__state__in=[
                            ResourceStates.OK,
                            ResourceStates.UPDATING,
                        ]
                    ),
                ),
            )
            .filter(resources_total__gt=0)
            .order_by("-resources_total")
            .values(
                "uuid",
                "name",
                "abbreviation",
                "resources_ok",
                "resources_erred",
                "resources_total",
                "total_cost",
            )
        )

        # Pre-fetch usages for all customers
        customer_usages = {}
        usages = (
            models.ComponentUsage.objects.filter(
                billing_period__year=now.year,
                billing_period__month=now.month,
            )
            .values(
                "resource__project__customer__uuid",
                "component__type",
            )
            .annotate(usage=Sum("usage"))
        )
        for u in usages:
            customer_uuid = str(u["resource__project__customer__uuid"])
            if customer_uuid not in customer_usages:
                customer_usages[customer_uuid] = {}
            customer_usages[customer_uuid][u["component__type"]] = u["usage"]

        # Pre-fetch limits for all customers
        customer_limits = {}
        for resource in (
            models.Resource.objects.filter(state=ResourceStates.OK)
            .exclude(limits={})
            .values("project__customer__uuid", "limits")
        ):
            customer_uuid = str(resource["project__customer__uuid"])
            if customer_uuid not in customer_limits:
                customer_limits[customer_uuid] = {}
            for name, value in resource["limits"].items():
                if value > 0:
                    if name in customer_limits[customer_uuid]:
                        customer_limits[customer_uuid][name] += value
                    else:
                        customer_limits[customer_uuid][name] = value

        # Build result
        result = []
        for customer in customers:
            customer_uuid = str(customer["uuid"])
            result.append(
                {
                    "customer_uuid": customer["uuid"],
                    "customer_name": customer["name"],
                    "customer_abbreviation": customer["abbreviation"],
                    "resources_ok": customer["resources_ok"],
                    "resources_erred": customer["resources_erred"],
                    "resources_total": customer["resources_total"],
                    "total_cost": customer["total_cost"] or 0,
                    "usages": customer_usages.get(customer_uuid, {}),
                    "limits": customer_limits.get(customer_uuid, {}),
                }
            )

        serializer = serializers.ResourceUsageByCustomerSerializer(result, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return resource usage grouped by creator's affiliation.",
        responses=serializers.ResourceUsageByAffiliationSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def resource_usage_by_creator_affiliation(self, request, *args, **kwargs):
        """
        Aggregate usage by user affiliations.
        Users with multiple affiliations are counted in each.
        Uses PostgreSQL jsonb_array_elements_text to expand affiliations.
        """
        now = timezone.now()
        billing_period = now.date().replace(day=1)

        # Use raw SQL for jsonb_array_elements_text expansion
        raw_query = """
            SELECT
                affiliation,
                component_type,
                SUM(usage) as total_usage,
                SUM(cost) as total_cost,
                COUNT(DISTINCT resource_id) as resource_count
            FROM (
                SELECT
                    jsonb_array_elements_text(u.affiliations) as affiliation,
                    oc.type as component_type,
                    cu.usage,
                    r.cost,
                    r.id as resource_id
                FROM marketplace_componentusage cu
                JOIN marketplace_resource r ON cu.resource_id = r.id
                JOIN marketplace_order o ON o.resource_id = r.id AND o.type = %s
                JOIN core_user u ON o.created_by_id = u.id
                JOIN marketplace_offeringcomponent oc ON cu.component_id = oc.id
                WHERE cu.billing_period = %s
                  AND u.affiliations IS NOT NULL
                  AND jsonb_array_length(u.affiliations) > 0
            ) subq
            GROUP BY affiliation, component_type
            ORDER BY resource_count DESC, affiliation, component_type
        """

        with connection.cursor() as cursor:
            cursor.execute(raw_query, [OrderTypes.CREATE, billing_period])
            columns = [col[0] for col in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        serializer = serializers.ResourceUsageByAffiliationSerializer(
            results, many=True
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return aggregated usage trends per month.",
        responses=serializers.AggregatedUsageTrendSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def aggregated_usage_trends(self, request, *args, **kwargs):
        """
        Aggregate component usages by year/month for trends reporting.
        Returns total usage, resource count, and component count per period.
        """
        usages = (
            models.ComponentUsage.objects.values(
                "billing_period__year",
                "billing_period__month",
            )
            .annotate(
                total_usage=Sum("usage"),
                resource_count=Count("resource_id", distinct=True),
                component_count=Count("id"),
            )
            .order_by("billing_period__year", "billing_period__month")
        )

        result = [
            {
                "period": f"{u['billing_period__year']}-{u['billing_period__month']:02d}",
                "year": u["billing_period__year"],
                "month": u["billing_period__month"],
                "total_usage": u["total_usage"],
                "resource_count": u["resource_count"],
                "component_count": u["component_count"],
            }
            for u in usages
        ]

        serializer = serializers.AggregatedUsageTrendSerializer(result, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="List all OpenStack instances with infrastructure details.",
        description="Returns a paginated flat list of all OpenStack instances across all clusters. "
        "Staff and support users can filter by infrastructure properties.",
        parameters=[
            OpenApiParameter(
                "name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by instance name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "flavor_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by flavor name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "image_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by image name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "hypervisor_hostname",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by hypervisor hostname (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "runtime_state",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by runtime state (e.g. ACTIVE, SHUTOFF).",
            ),
            OpenApiParameter(
                "availability_zone_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by availability zone name.",
            ),
            OpenApiParameter(
                "cores_min",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Minimum number of vCPUs.",
            ),
            OpenApiParameter(
                "cores_max",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Maximum number of vCPUs.",
            ),
            OpenApiParameter(
                "ram_min",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Minimum RAM in MiB.",
            ),
            OpenApiParameter(
                "ram_max",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Maximum RAM in MiB.",
            ),
            OpenApiParameter(
                "disk_min",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Minimum disk in MiB.",
            ),
            OpenApiParameter(
                "disk_max",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Maximum disk in MiB.",
            ),
            OpenApiParameter(
                "service_settings_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by cluster (service settings) UUID.",
                extensions={"x-waldur-operation-id": "service_settings_retrieve"},
            ),
            OpenApiParameter(
                "customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by customer UUID.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                "project_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by project UUID.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                "tenant_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by tenant UUID.",
                extensions={"x-waldur-operation-id": "openstack_tenants_retrieve"},
            ),
            OpenApiParameter(
                "state",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by provisioning state (e.g. OK, ERRED). Supports multiple values.",
            ),
            OpenApiParameter(
                "o",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Ordering field. Prefix with - for descending. "
                "Options: name, cores, ram, disk, created, runtime_state, "
                "flavor_name, hypervisor_hostname, customer_name, project_name, "
                "cluster_name, start_time.",
            ),
            OpenApiParameter(
                "page",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Page number.",
            ),
            OpenApiParameter(
                "page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                description="Number of results per page (max 300).",
            ),
        ],
        responses={200: serializers.OpenStackInstanceReportSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def openstack_instances(self, request):
        queryset = (
            openstack_models.Instance.objects.all()
            .select_related(
                "service_settings",
                "project__customer",
                "tenant",
                "availability_zone",
            )
            .prefetch_related("ports__floating_ips", "volumes")
        )

        filterset = filters.OpenStackInstanceReportFilter(
            request.query_params, queryset=queryset
        )
        queryset = filterset.qs

        paginator = LinkHeaderPagination()
        page = paginator.paginate_queryset(queryset, request)
        instances = page if page is not None else queryset

        data = []
        for instance in instances:
            volumes = instance.volumes.all()
            ports = instance.ports.all()
            floating_ips = []
            internal_ips = set()
            for port in ports:
                for fip in port.floating_ips.all():
                    floating_ips.append(fip)
                if port.fixed_ips:
                    for fixed_ip in port.fixed_ips:
                        ip_address = (
                            fixed_ip.get("ip_address")
                            if isinstance(fixed_ip, dict)
                            else None
                        )
                        if ip_address:
                            internal_ips.add(ip_address)

            data.append(
                {
                    "uuid": instance.uuid,
                    "name": instance.name,
                    "created": instance.created,
                    "cores": instance.cores,
                    "ram": instance.ram,
                    "disk": instance.disk,
                    "flavor_name": instance.flavor_name,
                    "flavor_disk": instance.flavor_disk,
                    "image_name": instance.image_name,
                    "hypervisor_hostname": instance.hypervisor_hostname,
                    "runtime_state": instance.runtime_state,
                    "state": instance.get_state_display(),
                    "availability_zone_name": (
                        instance.availability_zone.name
                        if instance.availability_zone
                        else None
                    ),
                    "start_time": instance.start_time,
                    "service_settings_uuid": instance.service_settings.uuid,
                    "service_settings_name": instance.service_settings.name,
                    "tenant_uuid": instance.tenant.uuid if instance.tenant else None,
                    "tenant_name": instance.tenant.name if instance.tenant else "",
                    "project_uuid": instance.project.uuid,
                    "project_name": instance.project.name,
                    "customer_uuid": instance.project.customer.uuid,
                    "customer_name": instance.project.customer.name,
                    "customer_abbreviation": instance.project.customer.abbreviation,
                    "volume_count": len(volumes),
                    "total_volume_size_mb": sum(v.size for v in volumes),
                    "floating_ip_count": len(floating_ips),
                    "port_count": len(ports),
                    "internal_ips": sorted(internal_ips),
                    "external_ips": sorted(
                        {fip.address for fip in floating_ips if fip.address}
                    ),
                }
            )

        serializer = serializers.OpenStackInstanceReportSerializer(data, many=True)
        if page is not None:
            return paginator.get_paginated_response(serializer.data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    OPENSTACK_INSTANCE_GROUP_BY_MAPPING = {
        "hypervisor_hostname": {
            "key_field": "hypervisor_hostname",
        },
        "flavor_name": {
            "key_field": "flavor_name",
        },
        "image_name": {
            "key_field": "image_name",
        },
        "availability_zone": {
            "key_field": "availability_zone__name",
        },
        "service_settings": {
            "key_field": "service_settings__uuid",
            "label_field": "service_settings__name",
        },
        "customer": {
            "key_field": "project__customer__uuid",
            "label_field": "project__customer__name",
        },
        "runtime_state": {
            "key_field": "runtime_state",
        },
    }

    @extend_schema(
        summary="Aggregate OpenStack instances by a dimension.",
        description="Returns aggregated metrics (count, cores, RAM, disk) grouped by the specified dimension.",
        parameters=[
            OpenApiParameter(
                "group_by",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                enum=list(OPENSTACK_INSTANCE_GROUP_BY_MAPPING.keys()),
                description="Dimension to group by.",
            ),
            OpenApiParameter(
                "name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by instance name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "flavor_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by flavor name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "image_name",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by image name (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "hypervisor_hostname",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by hypervisor hostname (case-insensitive partial match).",
            ),
            OpenApiParameter(
                "runtime_state",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by runtime state (e.g. ACTIVE, SHUTOFF).",
            ),
            OpenApiParameter(
                "service_settings_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by cluster (service settings) UUID.",
                extensions={"x-waldur-operation-id": "service_settings_retrieve"},
            ),
            OpenApiParameter(
                "customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by customer UUID.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
            OpenApiParameter(
                "project_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by project UUID.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                "tenant_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter by tenant UUID.",
                extensions={"x-waldur-operation-id": "openstack_tenants_retrieve"},
            ),
            OpenApiParameter(
                "state",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by provisioning state (e.g. OK, ERRED).",
            ),
        ],
        responses={200: serializers.OpenStackInstanceAggregateSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def openstack_instances_aggregate(self, request):
        group_by = request.query_params.get("group_by")
        if not group_by or group_by not in self.OPENSTACK_INSTANCE_GROUP_BY_MAPPING:
            raise ValidationError(
                {
                    "group_by": f"This parameter is required. Valid values: "
                    f"{', '.join(sorted(self.OPENSTACK_INSTANCE_GROUP_BY_MAPPING.keys()))}"
                }
            )

        mapping = self.OPENSTACK_INSTANCE_GROUP_BY_MAPPING[group_by]
        key_field = mapping["key_field"]
        label_field = mapping.get("label_field")

        queryset = openstack_models.Instance.objects.all()
        filterset = filters.OpenStackInstanceReportFilter(
            request.query_params, queryset=queryset
        )
        queryset = filterset.qs

        group_fields = [key_field]
        if label_field:
            group_fields.append(label_field)

        # Use per-instance subqueries for volume size and floating IP count,
        # then aggregate the subquery results per group. This avoids both
        # cross-join inflation and the N+1 of per-group queries.
        volume_size_subquery = (
            openstack_models.Volume.objects.filter(instance=OuterRef("pk"))
            .order_by()
            .values("instance")
            .annotate(total=Sum("size"))
            .values("total")
        )
        floating_ip_subquery = (
            openstack_models.FloatingIP.objects.filter(port__instance=OuterRef("pk"))
            .order_by()
            .values("port__instance")
            .annotate(total=Count("id"))
            .values("total")
        )

        annotated_qs = queryset.annotate(
            _vol_size=Coalesce(Subquery(volume_size_subquery), 0),
            _fip_count=Coalesce(Subquery(floating_ip_subquery), 0),
        )

        aggregated = (
            annotated_qs.values(*group_fields)
            .annotate(
                instance_count=Count("id"),
                total_cores=Sum("cores"),
                total_ram_mb=Sum("ram"),
                total_disk_mb=Sum("disk"),
                total_volume_size_mb=Sum("_vol_size"),
                total_floating_ips=Sum("_fip_count"),
            )
            .order_by("-instance_count")
        )

        data = []
        for row in aggregated:
            key = row[key_field]
            label = row.get(label_field, key) if label_field else key
            data.append(
                {
                    "group_key": str(key) if key is not None else "",
                    "group_label": str(label) if label is not None else "",
                    "instance_count": row["instance_count"],
                    "total_cores": row["total_cores"],
                    "total_ram_mb": row["total_ram_mb"],
                    "total_disk_mb": row["total_disk_mb"],
                    "total_volume_size_mb": row["total_volume_size_mb"],
                    "total_floating_ips": row["total_floating_ips"],
                }
            )

        serializer = serializers.OpenStackInstanceAggregateSerializer(data, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ProviderInvoiceItemsViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = invoice_models.InvoiceItem.objects.all().order_by("-invoice__created")
    filter_backends = (
        DjangoFilterBackend,
        filters.MarketplaceInvoiceItemsFilterBackend,
    )
    filterset_class = filters.MarketplaceInvoiceItemsFilter
    serializer_class = invoice_serializers.InvoiceItemSerializer


def can_mutate_robot_account(request, view, obj=None):
    if obj and obj.backend_id:
        raise PermissionDenied("Remote robot account is synchronized.")


@extend_schema_view(
    list=extend_schema(summary="List service accounts"),
    retrieve=extend_schema(summary="Retrieve a service account"),
    create=extend_schema(summary="Create a service account"),
    update=extend_schema(summary="Update a service account"),
    partial_update=extend_schema(summary="Partially update a service account"),
    destroy=extend_schema(summary="Close (delete) a service account"),
)
class BaseServiceAccountViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"

    def perform_create(self, serializer):
        owner_username = self.request.user.username
        try:
            data = serializer.validated_data
            scope_type = (
                "customer"
                if serializer.Meta.model == models.CustomerServiceAccount
                else "project"
            )
            if scope_type == "project":
                project = data.get("project")
                if project.max_service_accounts is not None:
                    project_service_accounts_count = (
                        models.ProjectServiceAccount.objects.filter(project=project)
                        .exclude(state=ServiceAccountState.CLOSED)
                        .count()
                    )
                    if project_service_accounts_count >= project.max_service_accounts:
                        raise ValidationError(
                            {
                                "detail": "Maximum number of service accounts reached for this project"
                            }
                        )
            elif scope_type == "customer":
                customer = data.get("customer")
                if customer.max_service_accounts is not None:
                    customer_service_accounts_count = (
                        models.CustomerServiceAccount.objects.filter(customer=customer)
                        .exclude(state=ServiceAccountState.CLOSED)
                        .count()
                    )
                    if customer_service_accounts_count >= customer.max_service_accounts:
                        raise ValidationError(
                            {
                                "detail": "Maximum number of service accounts reached for this customer"
                            }
                        )

            response_data = utils.create_service_account(
                data, owner_username, scope_type
            )
            if response_data and "apiKey" in response_data:
                instance = serializer.save()
                instance._token = response_data["apiKey"]["apiKey"]
                instance._expires_at = response_data["apiKey"]["expiresAt"]
                if "serviceAccount" in response_data:
                    instance.username = response_data["serviceAccount"]["username"]
                    instance.save(update_fields=["username"])
            else:
                raise ValidationError(
                    {
                        "detail": "Service account creation is disabled or returned no token."
                    }
                )
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            if "instance" in locals():
                instance.set_state_erred()
                instance.error_message = str(error_details)
                instance.error_traceback = traceback.format_exc()
                instance.save(
                    update_fields=["state", "error_message", "error_traceback"]
                )
            raise ValidationError({"detail": error_details})

    def perform_update(self, serializer):
        instance: models.ScopedServiceAccount = serializer.instance
        # Set the fields in the cache object
        instance.email = serializer.validated_data.get("email", instance.email)
        instance.description = serializer.validated_data.get(
            "description", instance.description
        )
        try:
            utils.update_service_account(instance)
            if instance.state != ServiceAccountState.OK:
                instance.set_state_ok()
                instance.error_message = ""
                instance.error_traceback = ""
            # Update the DB object only if the API call is successful
            super().perform_update(serializer)
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            raise ValidationError({"detail": error_details})

    update_validators = destroy_validators = [
        core_validators.StateValidator(
            ServiceAccountState.OK, ServiceAccountState.ERRED
        )
    ]

    def perform_destroy(self, instance):
        try:
            utils.close_service_account(instance)
        except httpx.HTTPError as exc:
            raise ValidationError(
                {"detail": utils.extract_error_details_from_httpx_error(exc)}
            )


@extend_schema_view(
    create=extend_schema(
        summary="Create a project service account",
        description="Creates a new service account scoped to a specific project. This generates an API key that can be used for automated access to resources within that project.",
        examples=[
            OpenApiExample(
                "Create a project service account",
                value={
                    "project": "http://testserver/api/projects/a1b2c3d4e5f678901234567890abcdef/",
                    "email": "automation-bot@example.com",
                    "description": "Service account for CI/CD pipelines",
                    "preferred_identifier": "cicd-bot-project-alpha",
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Close a project service account",
        description="Deactivates a project service account and revokes its API key.",
    ),
)
class ProjectServiceAccountViewSet(BaseServiceAccountViewSet):
    """
    Manage service accounts that are scoped to a specific project.

    Project service accounts provide a mechanism for automated systems (like CI/CD pipelines)
    to interact with resources within a single project, using a dedicated API key instead of a user's credentials.
    """

    queryset = models.ProjectServiceAccount.objects.exclude(
        state=ServiceAccountState.CLOSED
    )
    serializer_class = serializers.ProjectServiceAccountSerializer
    filterset_class = filters.ProjectServiceAccountFilter
    filter_backends = (DjangoFilterBackend,)
    destroy_permissions = partial_update_permissions = update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["project", "project.customer"],
        )
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs

        projects = get_connected_projects_by_permission(
            user, PermissionEnum.MANAGE_SERVICE_ACCOUNT
        )
        if projects:
            return qs.filter(project__in=projects)
        return qs.none()

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data.get("project")
        if not project:
            raise PermissionDenied()
        if not (
            has_permission(request, PermissionEnum.MANAGE_SERVICE_ACCOUNT, project)
            or has_permission(
                request, PermissionEnum.MANAGE_SERVICE_ACCOUNT, project.customer
            )
        ):
            raise PermissionDenied()

    create_permissions = [check_create_permissions]

    @extend_schema(
        summary="Rotate API key for a project service account",
        description="Generates a new API key for the service account, immediately invalidating the old one. The new key is returned in the response.",
        request=None,
        responses=serializers.ProjectServiceAccountSerializer,
    )
    @action(detail=True, methods=["post"])
    def rotate_api_key(self, request, uuid=None):
        service_account = self.get_object()
        try:
            response_data = utils.rotate_service_account_api_key(service_account)
            if response_data and "apiKey" in response_data:
                service_account._token = response_data["apiKey"]["apiKey"]
                service_account._expires_at = response_data["apiKey"]["expiresAt"]
                serializer = self.get_serializer(service_account)
                return Response(serializer.data)
            else:
                raise ValidationError(
                    {"detail": "API key rotation failed - no token returned"}
                )
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            raise ValidationError({"detail": error_details})

    rotate_api_key_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["project", "project.customer"],
        )
    ]

    rotate_api_key_validators = [core_validators.StateValidator(ServiceAccountState.OK)]


@extend_schema_view(
    create=extend_schema(
        summary="Create a customer service account",
        description="Creates a new service account scoped to a specific customer (organization). This generates an API key that can be used for automated access to resources across all projects within that customer.",
        examples=[
            OpenApiExample(
                "Create a customer service account",
                value={
                    "customer": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "email": "billing-bot@example.com",
                    "description": "Service account for billing and reporting",
                    "preferred_identifier": "billing-bot-customer-alpha",
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Close a customer service account",
        description="Deactivates a customer service account and revokes its API key.",
    ),
)
class CustomerServiceAccountViewSet(BaseServiceAccountViewSet):
    """
    Manage service accounts that are scoped to a specific customer (organization).

    Customer service accounts provide a mechanism for automated systems to interact
    with resources across all projects of a customer, using a dedicated API key.
    """

    queryset = models.CustomerServiceAccount.objects.exclude(
        state=ServiceAccountState.CLOSED
    )
    serializer_class = serializers.CustomerServiceAccountSerializer
    filterset_class = filters.CustomerServiceAccountFilter
    filter_backends = (DjangoFilterBackend,)

    destroy_permissions = partial_update_permissions = update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["customer"],
        )
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        customers = get_connected_customers_by_permission(
            user, PermissionEnum.MANAGE_SERVICE_ACCOUNT
        )
        if customers:
            return qs.filter(customer__in=customers)
        return qs.none()

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = serializer.validated_data.get("customer")
        if not customer:
            raise PermissionDenied()
        if not has_permission(request, PermissionEnum.MANAGE_SERVICE_ACCOUNT, customer):
            raise PermissionDenied()

    create_permissions = [check_create_permissions]

    @extend_schema(
        summary="Rotate API key for a customer service account",
        description="Generates a new API key for the service account, immediately invalidating the old one. The new key is returned in the response.",
        request=None,
        responses=serializers.CustomerServiceAccountSerializer,
    )
    @action(detail=True, methods=["post"])
    def rotate_api_key(self, request, uuid=None):
        service_account = self.get_object()
        try:
            response_data = utils.rotate_service_account_api_key(service_account)
            if response_data and "apiKey" in response_data:
                service_account._token = response_data["apiKey"]["apiKey"]
                service_account._expires_at = response_data["apiKey"]["expiresAt"]
                serializer = self.get_serializer(service_account)
                return Response(serializer.data)
            else:
                raise ValidationError(
                    {"detail": "API key rotation failed - no token returned"}
                )
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            raise ValidationError({"detail": error_details})

    rotate_api_key_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["customer"],
        )
    ]

    rotate_api_key_validators = [core_validators.StateValidator(ServiceAccountState.OK)]


def check_provider_api_key_permissions(request, view, obj=None):
    serializer = view.get_serializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    resource = serializer.validated_data["resource"]
    if not has_permission(
        request, PermissionEnum.MANAGE_RESOURCE_API_KEY, resource.offering.customer
    ):
        raise PermissionDenied()


class ResourceApiKeyViewSet(core_views.ActionsViewSet):
    """Manage the API keys a site-agent resource exposes.

    A resource owns many keys. The site agent generates each key, applies it to
    the backend, then reports it here (encrypted). Members reveal keys; managers
    rotate them. Portal actions are commands — the agent does the backend change
    and reports back through the provider actions.
    """

    queryset = models.ResourceApiKey.objects.select_related(
        "resource__project__customer",
        "resource__offering__customer",
    ).order_by("created")
    lookup_field = "uuid"
    serializer_class = serializers.ResourceApiKeyStatusSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ResourceApiKeyFilter
    # No destroy: the key count is fixed at provisioning, so nothing needs one, and
    # an ungated delete could drop a row whose key still serves at the backend.
    # Termination cleanup deletes the rows directly (callbacks.py).
    disabled_actions = ["create", "update", "partial_update", "destroy"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        customers = get_connected_customers(user)
        projects = get_connected_projects(user)
        return qs.filter(
            Q(resource__project__in=projects)
            | Q(resource__project__customer__in=customers)
            | Q(resource__offering__customer__in=customers)
        )

    # --- consumer actions ------------------------------------------------------

    @extend_schema(
        summary="Reveal an API key",
        description="Returns the decrypted key value. Available to users with "
        "resource access (except minimal-visibility viewers). Audit-logged.",
        responses={status.HTTP_200_OK: serializers.ResourceApiKeySerializer},
    )
    @action(detail=True, methods=["get"])
    def reveal(self, request, uuid=None):
        api_key = self.get_object()
        user = request.user
        resource = api_key.resource
        # Reveal exposes a live secret. The shared queryset also admits the
        # provider org (so the agent can reach the write actions), but a
        # provider-org member must NOT read a consumer's key — restrict reveal to
        # consumer-side access (project or its customer), plus staff/support.
        if not (
            user.is_staff
            or user.is_support
            or resource.project_id in get_connected_projects(user)
            or resource.project.customer_id in get_connected_customers(user)
        ):
            raise PermissionDenied(
                "Only members of the resource's project or organization can "
                "reveal its API key."
            )
        if utils.is_resource_project_only_viewer(user, resource):
            raise PermissionDenied(
                "Minimal-visibility viewers cannot reveal the resource API key."
            )
        # Only an OK key is guaranteed live at the backend; a transitional key's
        # stored value may not match the gateway.
        if api_key.state != models.ResourceApiKey.States.OK:
            raise IncorrectStateException(
                f"The API key is {api_key.state}; it can only be revealed when OK."
            )
        try:
            data = serializers.ResourceApiKeySerializer(api_key).data
        except InvalidToken:
            raise IncorrectStateException(
                "The stored API key cannot be decrypted with the configured "
                "encryption keys; check FIELD_ENCRYPTION_KEY and "
                "FIELD_ENCRYPTION_KEY_FALLBACKS."
            )
        log.log_resource_api_key_revealed(api_key, user)
        response = Response(data)
        # The body carries a live secret; no cache may retain it.
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _require_resource_live(resource):
        """Reject key commands on a resource that is not live.

        Rotating a key makes no sense once the resource is on its way out; a
        command emitted for a terminating resource races the termination cleanup
        (which deletes the key rows) at the agent.
        """
        dead = (
            models.Resource.States.TERMINATING,
            models.Resource.States.TERMINATED,
        )
        if resource.state in dead:
            raise IncorrectStateException(
                f"The resource is {resource.get_state_display()}; its API keys "
                f"can no longer be managed."
            )

    @staticmethod
    def _locked_transition(obj, transition: str, **updates):
        """Lock the row, apply an FSM transition plus field updates, save —
        atomic against a concurrent duplicate command that would otherwise both
        read the same source state."""
        with transaction.atomic():
            api_key = models.ResourceApiKey.objects.select_for_update().get(pk=obj.pk)
            getattr(api_key, transition)()
            for field, value in updates.items():
                setattr(api_key, field, value)
            api_key.save()
        return api_key

    @extend_schema(
        summary="Rotate an API key",
        description="Asks the site agent to replace this key's value at the "
        "backend. The other keys are untouched (zero downtime).",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def rotate(self, request, uuid=None):
        obj = self.get_object()
        self._require_resource_live(obj.resource)
        try:
            api_key = self._locked_transition(obj, "set_updating")
        except TransitionNotAllowed:
            raise IncorrectStateException(
                "An API key can only be rotated from the OK state."
            )
        utils.publish_api_key_event(api_key)
        log.log_resource_api_key_rotated(api_key, request.user)
        return Response(
            {"status": _("API key rotation has been requested.")},
            status=status.HTTP_202_ACCEPTED,
        )

    rotate_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_RESOURCE_USERS,
            ["resource.project", "resource.project.customer"],
        )
    ]

    # There is deliberately no revoke: the key count is fixed at provisioning and
    # rotation replaces a value in place, so a resource can never be left without
    # a way to authenticate. See docs/resource-api-keys.md.

    # --- provider (site-agent) actions -----------------------------------------

    @extend_schema(
        summary="Report a freshly-applied API key",
        description="Used by the site agent after it generated and applied a key "
        "to the backend. Stores the value encrypted and marks the key OK.",
        request=serializers.ResourceApiKeyReportCreatedSerializer,
        responses={status.HTTP_201_CREATED: serializers.ResourceApiKeyStatusSerializer},
    )
    @action(detail=False, methods=["post"])
    def report_created(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        resource = data["resource"]
        # A late report against a terminating/terminated resource must not
        # recreate rows the termination cleanup deletes.
        self._require_resource_live(resource)
        plaintext = data["api_key"]
        # Idempotent upsert on (resource, client_id): a retried or duplicated
        # report must not 500 on the unique constraint, and a re-applied key just
        # overwrites the stored value. New rows land OK (the agent already applied
        # the key to the backend before reporting). No row lock is needed: there is
        # no transitional state a stale duplicate could resurrect, and
        # update_or_create already handles a concurrent insert through the unique
        # constraint.
        with transaction.atomic():
            api_key, _ = models.ResourceApiKey.objects.update_or_create(
                resource=resource,
                client_id=data["client_id"],
                defaults={
                    "key_ciphertext": encryption.encrypt_value(plaintext),
                    "state": models.ResourceApiKey.States.OK,
                    "error_message": "",
                },
            )
        return Response(
            serializers.ResourceApiKeyStatusSerializer(api_key).data,
            status=status.HTTP_201_CREATED,
        )

    report_created_permissions = [check_provider_api_key_permissions]
    report_created_serializer_class = serializers.ResourceApiKeyReportCreatedSerializer

    @extend_schema(
        summary="Report a rotated API key value",
        description="Used by the site agent after it applied a rotated key. "
        "Replaces the stored value and marks the key OK.",
        request=serializers.ResourceApiKeySetKeySerializer,
        responses={status.HTTP_200_OK: serializers.ResourceApiKeyStatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_key(self, request, uuid=None):
        obj = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plaintext = serializer.validated_data["api_key"]
        updates = {
            "key_ciphertext": encryption.encrypt_value(plaintext),
            "error_message": "",
        }
        # A backend whose public identifier rotates together with the secret (an S3
        # access key) reports the new one; one with a stable client_id omits it. The
        # collision check runs before the transition so a rejected rename leaves the
        # key exactly as it was.
        client_id = serializer.validated_data.get("client_id")
        if client_id and client_id != obj.client_id:
            if (
                models.ResourceApiKey.objects.filter(
                    resource=obj.resource, client_id=client_id
                )
                .exclude(pk=obj.pk)
                .exists()
            ):
                raise ValidationError(
                    f"Another API key of this resource already uses client_id {client_id}."
                )
            updates["client_id"] = client_id
        # Transition under lock, persist only if it is legal: a late or duplicated
        # report must never overwrite an already applied OK key.
        try:
            api_key = self._locked_transition(obj, "set_ok", **updates)
        except TransitionNotAllowed:
            raise IncorrectStateException(
                f"A key value can only be applied to a Creating or Updating key, "
                f"not {obj.state}."
            )
        except IntegrityError:
            # The collision check above runs outside the row lock, so a concurrent
            # writer can claim the client_id between check and save; surface the
            # constraint violation as the same 400 instead of a 500.
            raise ValidationError(
                f"Another API key of this resource already uses client_id {client_id}."
            )
        return Response(serializers.ResourceApiKeyStatusSerializer(api_key).data)

    set_key_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_RESOURCE_API_KEY, ["resource.offering.customer"]
        )
    ]
    set_key_serializer_class = serializers.ResourceApiKeySetKeySerializer

    @extend_schema(
        summary="Mark an API key as erred",
        description="Used by the site agent to report that applying the key "
        "failed. Stores the error message for the UI.",
        request=serializers.ResourceApiKeySetErredSerializer,
        responses={status.HTTP_200_OK: serializers.ResourceApiKeyStatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_erred(self, request, uuid=None):
        obj = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            api_key = self._locked_transition(
                obj,
                "set_erred",
                error_message=serializer.validated_data["error_message"],
            )
        except TransitionNotAllowed:
            # A newer set_key already landed the key OK; ignore the stale erred.
            raise IncorrectStateException(f"Cannot mark a {obj.state} key erred.")
        return Response(serializers.ResourceApiKeyStatusSerializer(api_key).data)

    set_erred_permissions = set_key_permissions
    set_erred_serializer_class = serializers.ResourceApiKeySetErredSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List robot accounts",
        description="Returns a paginated list of robot accounts accessible to the current user.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a robot account",
        description="Returns the details of a specific robot account.",
    ),
    create=extend_schema(
        summary="Create a robot account",
        description="Creates a new robot account for a specific resource. This is typically used for automated access to a resource, e.g., for CI/CD pipelines.",
    ),
    update=extend_schema(
        summary="Update a robot account",
        description="Updates the properties of a robot account, such as its username or associated users. Not allowed for synchronized remote accounts.",
    ),
    partial_update=extend_schema(
        summary="Partially update a robot account",
        description="Partially updates the properties of a robot account. Not allowed for synchronized remote accounts.",
    ),
    destroy=extend_schema(
        summary="Delete a robot account",
        description="Deletes a robot account. This is a hard delete and should be used with caution.",
    ),
)
class RobotAccountViewSet(core_views.ActionsViewSet):
    queryset = models.RobotAccount.objects.select_related(
        "responsible_user",
        "resource__project__customer",
        "resource__offering__customer",
    ).prefetch_related("users")
    lookup_field = "uuid"
    create_serializer_class = serializers.RobotAccountSerializer
    update_serializer_class = partial_update_serializer_class = (
        serializers.RobotAccountSerializer
    )
    serializer_class = serializers.RobotAccountDetailsSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.RobotAccountFilter

    unsafe_methods_permissions = [can_mutate_robot_account]

    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_RESOURCE_ROBOT_ACCOUNT,
            ["resource.offering.customer"],
        )
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        customers = get_connected_customers(user)
        projects = get_connected_projects(user)
        subquery = (
            Q(resource__project__in=projects)
            | Q(resource__project__customer__in=customers)
            | Q(resource__offering__customer__in=customers)
        )
        return qs.filter(subquery)

    def perform_create(self, serializer):
        instance: models.RobotAccount = serializer.save()
        offering = instance.resource.offering
        utils.setup_linux_related_data(instance, offering)
        # Set state to CREATING and OK since setup is complete
        instance.begin_creating()
        instance.set_ok()
        instance.save()

    def perform_update(self, serializer):
        instance: models.RobotAccount = serializer.save()
        offering = instance.resource.offering
        utils.setup_linux_related_data(instance, offering)
        instance.save()

    @extend_schema(
        summary="Set robot account state to creating",
        description="Transitions the robot account state from 'Requested' to 'Creating'. This is typically used by an agent to signal that the creation process has started.",
        request=None,
        responses={
            200: serializers.RobotAccountDetailsSerializer,
            400: serializers.StateTransitionErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_state_creating(self, request, uuid=None):
        robot_account: models.RobotAccount = self.get_object()
        try:
            robot_account.begin_creating()
            robot_account.save()
            serializer = self.get_serializer(robot_account)
            return Response(serializer.data)
        except TransitionNotAllowed:
            error_serializer = serializers.StateTransitionErrorSerializer(
                {"detail": "This transition is not allowed in the current state."}
            )
            return Response(error_serializer.data, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="Set robot account state to OK",
        description="Manually sets the robot account state to 'OK', indicating that it is fully operational. This can be used to recover from an error state.",
        request=None,
        responses={
            200: serializers.RobotAccountDetailsSerializer,
            400: serializers.StateTransitionErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_state_ok(self, request, uuid=None):
        robot_account: models.RobotAccount = self.get_object()
        try:
            robot_account.set_ok()
            robot_account.save()
            serializer = self.get_serializer(robot_account)
            return Response(serializer.data)
        except TransitionNotAllowed:
            error_serializer = serializers.StateTransitionErrorSerializer(
                {
                    "detail": f"This transition is not allowed in the current state: {robot_account.state}"
                }
            )
            return Response(error_serializer.data, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="Request deletion of a robot account",
        description="Transitions the robot account state from 'OK' to 'Requested deletion', initiating the deletion process.",
        request=None,
        responses={
            200: serializers.RobotAccountDetailsSerializer,
            400: serializers.StateTransitionErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_state_request_deletion(self, request, uuid=None):
        robot_account: models.RobotAccount = self.get_object()
        try:
            robot_account.request_deletion()
            robot_account.save()
            serializer = self.get_serializer(robot_account)
            return Response(serializer.data)
        except TransitionNotAllowed:
            error_serializer = serializers.StateTransitionErrorSerializer(
                {
                    "detail": f"This transition is not allowed in the current state: {robot_account.state}"
                }
            )
            return Response(error_serializer.data, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="Set robot account state to deleted",
        description="Transitions the robot account state from 'Requested deletion' to 'Deleted', marking the successful completion of the deletion process.",
        request=None,
        responses={
            200: serializers.RobotAccountDetailsSerializer,
            400: serializers.StateTransitionErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_state_deleted(self, request, uuid=None):
        robot_account: models.RobotAccount = self.get_object()
        try:
            robot_account.set_deleted()
            robot_account.save()
            serializer = self.get_serializer(robot_account)
            return Response(serializer.data)
        except TransitionNotAllowed:
            error_serializer = serializers.StateTransitionErrorSerializer(
                {
                    "detail": f"This transition is not allowed in the current state: {robot_account.state}"
                }
            )
            return Response(error_serializer.data, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="Set robot account state to erred",
        description="Manually moves the robot account into the 'Error' state. An optional error message can be provided.",
        request=serializers.RobotAccountErrorSerializer,
        responses={
            200: serializers.RobotAccountDetailsSerializer,
            400: serializers.StateTransitionErrorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_state_erred(self, request, uuid=None):
        robot_account: models.RobotAccount = self.get_object()
        error_serializer = serializers.RobotAccountErrorSerializer(data=request.data)
        error_serializer.is_valid(raise_exception=True)

        try:
            robot_account.set_error()
            if error_serializer.validated_data.get("error_message"):
                robot_account.error_message = error_serializer.validated_data.get(
                    "error_message"
                )
            robot_account.save()
            response_serializer = self.get_serializer(robot_account)
            return Response(response_serializer.data)
        except TransitionNotAllowed:
            error_serializer = serializers.StateTransitionErrorSerializer(
                {
                    "detail": f"This transition is not allowed in the current state: {robot_account.state}"
                }
            )
            return Response(error_serializer.data, status=status.HTTP_400_BAD_REQUEST)

    set_state_creating_permissions = set_state_ok_permissions = (
        set_state_request_deletion_permissions
    ) = set_state_deleted_permissions = set_state_erred_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT,
            ["resource.offering.customer"],
        )
    ]

    # Validators for state transitions
    set_state_creating_validators = [
        core_validators.StateValidator(
            RobotAccountStates.REQUESTED, state_enum=RobotAccountStates
        )
    ]
    set_state_ok_validators = [
        core_validators.StateValidator(
            RobotAccountStates.CREATING, state_enum=RobotAccountStates
        )
    ]
    set_state_request_deletion_validators = [
        core_validators.StateValidator(
            RobotAccountStates.OK, state_enum=RobotAccountStates
        )
    ]
    set_state_deleted_validators = [
        core_validators.StateValidator(
            RobotAccountStates.REQUESTED_DELETION, state_enum=RobotAccountStates
        )
    ]
    set_state_erred_validators = [
        core_validators.StateValidator(
            RobotAccountStates.OK, state_enum=RobotAccountStates
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List sections",
        description="Returns a paginated list of all sections. Sections are used to group attributes within a category.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a section",
        description="Returns the details of a specific section, identified by its key.",
    ),
    create=extend_schema(
        summary="Create a section",
        description="Creates a new section within a category. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a section",
        description="Updates an existing section. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a section",
        description="Partially updates an existing section. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a section",
        description="Deletes a section. Requires staff permissions.",
    ),
)
class SectionViewSet(rf_viewsets.ModelViewSet):
    """
    Manage sections for marketplace categories.

    Sections are used to organize attributes into logical groups within a category's
    offering configuration form. This endpoint is primarily for administrative purposes
    and requires staff permissions for modification.
    """

    queryset = models.Section.objects.all().order_by("title")
    lookup_field = "key"
    serializer_class = serializers.SectionSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


@extend_schema_view(
    list=extend_schema(
        summary="List attributes",
        description="Returns a paginated list of all attributes. Attributes define form fields within section. Filter by section (URL).",
    ),
    retrieve=extend_schema(
        summary="Retrieve an attribute",
        description="Returns the details of a specific attribute, identified by its UUID.",
    ),
    create=extend_schema(
        summary="Create an attribute",
        description="Creates a new attribute within a section. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update an attribute",
        description="Updates an existing attribute. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update an attribute",
        description="Partially updates an existing attribute. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete an attribute",
        description="Deletes an attribute. Requires staff permissions.",
    ),
)
class AttributeViewSet(rf_viewsets.ModelViewSet):
    """
    Manage attributes for marketplace sections.

    Attributes define form fields (string, integer, choice, etc.) within a section.
    This endpoint is primarily for administrative purposes and requires staff
    permissions for modification.
    """

    queryset = models.Attribute.objects.all().order_by("title")
    lookup_field = "uuid"
    serializer_class = serializers.AttributeSerializer
    filterset_class = filters.AttributeFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


@extend_schema_view(
    list=extend_schema(
        summary="List attribute options",
        description="Returns a paginated list of options for choice-type attributes. Filter by attribute (URL). Default option is determined by attribute.default.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an attribute option",
        description="Returns the details of a specific attribute option.",
    ),
    create=extend_schema(
        summary="Create an attribute option",
        description="Creates a new option for a choice-type attribute. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update an attribute option",
        description="Updates an existing attribute option. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update an attribute option",
        description="Partially updates an existing attribute option. To set the default option, PATCH the attribute with default=<option_key>. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete an attribute option",
        description="Deletes an attribute option. Requires staff permissions.",
    ),
)
class AttributeOptionViewSet(rf_viewsets.ModelViewSet):
    """
    Manage options for choice-type attributes.

    Options can only be added to attributes of type 'choice'. The default
    option is stored in attribute.default. Use PATCH on the attribute to set
    the default option key.
    Requires staff permissions for modification.
    """

    queryset = models.AttributeOption.objects.all().order_by("title")
    lookup_field = "uuid"
    serializer_class = serializers.AttributeOptionSerializer
    filterset_class = filters.AttributeOptionFilter
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


@extend_schema_view(
    list=extend_schema(
        summary="List category help articles",
        description="Returns a paginated list of all help articles associated with marketplace categories.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a category help article",
        description="Returns the details of a specific help article, identified by its ID.",
    ),
    create=extend_schema(
        summary="Create a category help article",
        description="Creates a new help article and associates it with one or more categories. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a category help article",
        description="Updates an existing help article. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a category help article",
        description="Partially updates an existing help article. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a category help article",
        description="Deletes a help article. Requires staff permissions.",
    ),
)
class CategoryHelpArticleViewSet(rf_viewsets.ModelViewSet):
    """
    Manage help articles for marketplace categories.

    Help articles provide links to documentation or support resources related to a category.
    These are displayed on the offering details page to assist users. This endpoint is
    primarily for administrative purposes and requires staff permissions for modification.
    """

    queryset = models.CategoryHelpArticle.objects.all().order_by("title")
    serializer_class = serializers.CategoryHelpArticlesSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


@extend_schema_view(
    list=extend_schema(
        summary="List category components",
        description="Returns a paginated list of all components defined at the category level. These act as templates for components in offerings.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a category component",
        description="Returns the details of a specific category component, identified by its ID.",
    ),
    create=extend_schema(
        summary="Create a category component",
        description="Creates a new component for a category. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a category component",
        description="Updates an existing category component. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a category component",
        description="Partially updates an existing category component. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a category component",
        description="Deletes a category component. Requires staff permissions.",
    ),
)
class CategoryComponentViewSet(rf_viewsets.ModelViewSet):
    """
    Manage components for marketplace categories.

    Category components define the measurable units (e.g., CPU, RAM, storage) that can be
    included in offerings within a specific category. They serve as templates and are used
    for aggregated usage reporting. This endpoint is primarily for administrative purposes
    and requires staff permissions for modification.
    """

    queryset = models.CategoryComponent.objects.all().order_by("name")
    serializer_class = serializers.CategoryComponentsSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


class GlobalCategoriesViewSet(views.APIView):
    @extend_schema(
        summary="Get resource counts by category",
        description="""
        Returns a dictionary mapping marketplace category UUIDs to the count of active (non-terminated)
        resources the current user has access to within that category. This is primarily used for UI
        dashboards or sidebars to display the number of resources in each category filter.

        The counts can be further filtered by providing a `project_uuid` or `customer_uuid`.
        """,
        parameters=[
            OpenApiParameter(
                name="project_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter counts by resources within a specific project.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                description="Filter counts by resources within a specific customer.",
                extensions={"x-waldur-operation-id": "customers_retrieve"},
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample(
                name="Example of Category Resource Counts",
                summary="A dictionary of category UUIDs and their corresponding resource counts.",
                description="The keys are the UUIDs of the categories (in hex format), and the values are the number of non-terminated resources the user can see in that category.",
                value={
                    "a1b2c3d4e5f678901234567890abcdef": 5,
                    "b2c3d4e5f678901234567890abcdef12": 2,
                    "c3d4e5f678901234567890abcdef1234": 10,
                },
            )
        ],
    )
    def get(self, request):
        # We need to reset ordering to avoid extra GROUP BY created field.
        qs: ResourceQuerySet = models.Resource.objects.all()
        resources = (
            qs.order_by()
            .filter_for_service_consumer(request.user)
            .exclude(state=ResourceStates.TERMINATED)
        )

        project_uuid = request.query_params.get("project_uuid")
        customer_uuid = request.query_params.get("customer_uuid")

        if project_uuid:
            resources = resources.filter(project__uuid=project_uuid)

        if customer_uuid:
            resources = resources.filter(project__customer__uuid=customer_uuid)

        resources = filter_queryset_by_user_ip(resources, request)

        qs = resources.values("offering__category__uuid").annotate(count=Count("*"))
        return Response(
            {row["offering__category__uuid"].hex: row["count"] for row in qs}
        )


@extend_schema_view(
    list=extend_schema(
        summary="List integration statuses",
        description="Returns a paginated list of integration statuses for offerings. This is used to monitor the connectivity and health of backend agents (e.g., site agents) associated with offerings.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an integration status",
        description="Returns the details of a specific integration status, including the agent type, status, and last request timestamp.",
    ),
)
class IntegrationStatusViewSet(core_views.ReadOnlyActionsViewSet):
    """
    Provides read-only access to the integration status of backend agents for offerings.

    This viewset is used by service providers to monitor the health of their integrations.
    Each record represents the status of a specific agent (e.g., for order processing,
    usage reporting) for a particular offering. The status is automatically updated when
    the agent communicates with the marketplace API.
    """

    lookup_field = "uuid"
    queryset = models.IntegrationStatus.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.IntegrationStatusFilter
    serializer_class = serializers.IntegrationStatusDetailsSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs

        offerings = [
            offering
            for offering in models.Offering.objects.all().filter_for_user(user)
            if offering.customer.has_user(user, CustomerRole.OWNER)
            or offering.customer.has_user(
                user,
                ServiceProviderRole.MANAGER,
            )
        ]
        return qs.filter(offering__in=offerings)


@extend_schema_view(
    list=extend_schema(
        summary="List component usage limits for users",
        description="Returns a paginated list of usage limits set for specific users on resource components.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a component usage limit",
        description="Returns the details of a specific user's usage limit for a component.",
    ),
    create=extend_schema(
        summary="Create a component usage limit for a user",
        description="Sets a usage limit for a specific user on a resource's component. This is only applicable for offerings that support per-user consumption limitation.",
        examples=[
            OpenApiExample(
                "Set a CPU usage limit for a user",
                value={
                    "resource": "http://testserver/api/marketplace-resources/a1b2c3d4-e5f6-7890-1234-567890abcdef/",
                    "component": "b2c3d4e5-f678-9012-3456-7890abcdef12",
                    "user": "http://testserver/api/marketplace-offering-users/c3d4e5f6-7890-1234-5678-90abcdef1234/",
                    "limit": 100,
                },
            )
        ],
    ),
    update=extend_schema(
        summary="Update a component usage limit",
        description="Updates an existing usage limit for a user on a component.",
    ),
    partial_update=extend_schema(
        summary="Partially update a component usage limit",
        description="Partially updates an existing usage limit for a user on a component.",
    ),
    destroy=extend_schema(
        summary="Delete a component usage limit",
        description="Removes a usage limit for a user on a component.",
    ),
)
class ComponentUserUsageLimitViewSet(core_views.ActionsViewSet):
    """
    Manage per-user usage limits for resource components.

    This viewset allows project and customer administrators to set, update, and delete
    consumption limits for individual users on specific components of a resource. This is
    useful for controlling and budgeting resource usage within a team.
    """

    lookup_field = "uuid"
    queryset = models.ComponentUserUsageLimit.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUserUsageLimitFilter
    serializer_class = serializers.ComponentUserUsageLimitSerializer

    destroy_permissions = update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION,
            ["resource.project.customer", "resource.project"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List backend resources",
        description="Returns a paginated list of backend resources that are available for import. This endpoint is typically used by site agents to see which resources they have reported.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a backend resource",
        description="Returns the details of a specific backend resource.",
    ),
    create=extend_schema(
        summary="Create a backend resource",
        description="Creates a new backend resource record. This is typically done by a site agent to report a resource that is available for import into the marketplace.",
        examples=[
            OpenApiExample(
                "Create a backend resource",
                summary="Example of creating a backend resource for a specific offering and project.",
                value={
                    "name": "my-backend-vm-123",
                    "project": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "offering": "b2c3d4e5-f678-9012-3456-7890abcdef12",
                    "backend_id": "vm-backend-uuid-5678",
                    "backend_metadata": {
                        "cpu_cores": 4,
                        "ram_gb": 8,
                        "storage_gb": 100,
                    },
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Delete a backend resource",
        description="Deletes a backend resource record. This is typically done when the resource is no longer available for import.",
    ),
)
class BackendResourceViewSet(core_views.ActionsViewSet):
    """
    Manage backend resources that are candidates for import into the marketplace.

    This viewset provides endpoints for site agents and administrators to manage the lifecycle
    of backend resources before they are imported as marketplace resources. It allows for the
    creation, listing, and deletion of these pre-import records. The `import_resource` action
    is a staff-only operation to convert a backend resource into a full marketplace resource.
    """

    lookup_field = "uuid"
    queryset = models.BackendResource.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.BackendResourceFilter
    serializer_class = serializers.BackendResourceSerializer
    disabled_actions = ["update", "partial_update"]
    import_resource_serializer_class = serializers.BackendResourceImportSerializer

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = serializer.validated_data.get("offering")

        if not offering:
            raise PermissionDenied()

        if has_permission(
            request, PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES, offering
        ) or has_permission(
            request,
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES,
            offering.customer,
        ):
            return

        raise PermissionDenied()

    create_permissions = [check_create_permissions]

    list_permissions = retrieve_permissions = destroy_permissions = (
        update_permissions
    ) = partial_update_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES,
            ["offering", "offering.customer"],
        )
    ]

    @extend_schema(
        summary="Import a backend resource (staff only)",
        description="""
        Converts a backend resource into a full marketplace resource. This action is restricted to staff users.
        Upon successful import, the original backend resource record is deleted. A fake order in the 'done'
        state is created to represent the import event.
        """,
        request=serializers.BackendResourceImportSerializer,
        responses={status.HTTP_201_CREATED: serializers.ResourceSerializer},
        examples=[
            OpenApiExample(
                "Import with a specific plan",
                summary="Importing a resource and assigning it to a specific plan.",
                value={"plan": "a1b2c3d4-e5f6-7890-1234-567890abcdef"},
            ),
            OpenApiExample(
                "Import without a plan (for private offerings)",
                summary="Importing a resource for a private offering where a plan is not required.",
                value={},
            ),
        ],
    )
    @action(detail=True, methods=["post"])
    def import_resource(self, request, uuid=None):
        import_resource_serializer = self.get_serializer(data=request.data)
        import_resource_serializer.is_valid(raise_exception=True)

        backend_resource = self.get_object()

        plan = import_resource_serializer.validated_data.get("plan", None)
        project = backend_resource.project
        offering = backend_resource.offering

        if not plan and offering.shared:
            raise rf_exceptions.ValidationError(
                {"plan": _("Plan is required when importing resources.")}
            )

        backend_id = backend_resource.backend_id
        utils.validate_backend_id(backend_id, offering)
        logger.info(
            "Importing the backend resource %s (%s)", backend_resource.name, backend_id
        )

        if models.Resource.objects.filter(
            offering=offering, backend_id=backend_id, state=ResourceStates.OK
        ).exists():
            raise rf_exceptions.ValidationError(
                _("Resource has been imported already.")
            )

        limits = backend_resource.backend_metadata.get("limits", {})
        resource = models.Resource(
            project=project,
            offering=offering,
            backend_id=backend_id,
            plan=plan,
            state=ResourceStates.OK,
            name=backend_resource.name,
            limits=limits,
        )
        resource.init_cost()
        resource.save()

        logger.info(
            "The backend resource %s (%s) has been imported, creating a fake order",
            backend_resource.name,
            backend_id,
        )

        order = models.Order(
            created=resource.created,
            created_by=request.user,
            resource=resource,
            offering=resource.offering,
            project=resource.project,
            limits=resource.limits,
            state=OrderStates.DONE,
            consumer_reviewed_by=request.user,
            provider_reviewed_by=request.user,
            consumer_reviewed_at=resource.created,
            provider_reviewed_at=resource.created,
        )
        order.save()

        logger.info(
            "The automatic order for resource %s (%s) has been created",
            resource.name,
            resource.backend_id,
        )

        logger.info(
            "Deleting BackendResource instance %s (%s) after succesful import",
            backend_resource.name,
            backend_resource.backend_id,
        )

        backend_resource.delete()

        resource_serializer = serializers.ResourceSerializer(
            resource, context=self.get_serializer_context()
        )
        return Response(data=resource_serializer.data, status=status.HTTP_201_CREATED)

    import_resource_permissions = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List backend resource requests",
        description="Returns a paginated list of requests for backend resources.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a backend resource request",
        description="Returns the details of a specific backend resource request.",
    ),
    create=extend_schema(
        summary="Create a backend resource request",
        description="Creates a new request to fetch a list of importable resources from a backend. This is typically used by staff to trigger a site agent to report available resources.",
        examples=[
            OpenApiExample(
                "Request resources for an offering",
                value={"offering": "a1b2c3d4-e5f6-7890-1234-567890abcdef"},
            )
        ],
    ),
)
class BackendResourceRequestViewSet(core_views.ActionsViewSet):
    """
    Manage requests for lists of importable backend resources.

    This viewset provides endpoints for creating and managing requests that are sent to site agents.
    After a request is created, an agent is expected to process it by creating `BackendResource`
    instances and updating the request's state through the available actions (`start_processing`, `set_done`, `set_erred`).
    """

    lookup_field = "uuid"
    queryset = models.BackendResourceRequest.objects.all().order_by("-created")
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.BackendResourceRequestFilter
    serializer_class = serializers.BackendResourceReqSerializer
    disabled_actions = ["update", "partial_update", "destroy"]
    create_permissions = [structure_permissions.is_staff]

    def perform_create(self, serializer) -> None:
        serializer.save()
        request = serializer.instance
        utils.publish_backend_resource_request(request)

    @extend_schema(
        summary="Start processing a request",
        description="Transitions the request state from 'Sent' to 'Processing'. This is used by a site agent to acknowledge that it has started fetching the resource list.",
        request=None,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def start_processing(self, request, uuid=None):
        resource_request = self.get_object()
        resource_request.start_processing()
        resource_request.save()

        return Response(
            {"status": _("Request state set to processing.")}, status=status.HTTP_200_OK
        )

    start_processing_validators = [
        core_validators.StateValidator(models.BackendResourceRequest.States.SENT)
    ]

    @extend_schema(
        summary="Mark a request as done",
        description="Transitions the request state from 'Processing' to 'Done'. This is used by a site agent to signal that it has successfully reported all available resources.",
        request=None,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def set_done(self, request, uuid=None):
        resource_request = self.get_object()

        resource_request.set_done()
        resource_request.save()

        return Response(
            {"status": _("Request state set to done.")}, status=status.HTTP_200_OK
        )

    set_done_validators = [
        core_validators.StateValidator(models.BackendResourceRequest.States.PROCESSING)
    ]

    @extend_schema(
        summary="Mark a request as erred",
        description="Transitions the request state to 'Erred'. This is used by a site agent to report a failure during the resource fetching process. An error message and traceback should be provided.",
        request=serializers.BackendResourceRequestSetErredSerializer,
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
        examples=[
            OpenApiExample(
                "Report an error",
                value={
                    "error_message": "Failed to connect to the backend API.",
                    "error_traceback": "Traceback(...)",
                },
            )
        ],
    )
    @action(detail=True, methods=["post"])
    def set_erred(self, request, uuid=None):
        resource_request = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        error_message = serializer.validated_data["error_message"]
        error_traceback = serializer.validated_data["error_traceback"]

        resource_request.set_erred()
        resource_request.error_message = error_message
        resource_request.error_traceback = error_traceback
        resource_request.save(
            update_fields=["error_message", "error_traceback", "state", "finished"]
        )
        resource_request.save()

        return Response(
            {"status": _("Request state set to erred.")}, status=status.HTTP_200_OK
        )

    set_erred_serializer_class = serializers.BackendResourceRequestSetErredSerializer

    start_processing_permissions = set_done_permissions = set_erred_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES,
            ["offering", "offering.customer"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List maintenance announcements",
        description="Returns a paginated list of maintenance announcements.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a maintenance announcement",
        description="Returns the details of a specific maintenance announcement.",
    ),
    create=extend_schema(
        summary="Create a maintenance announcement",
        description="Creates a new maintenance announcement in the 'Draft' state.",
    ),
    update=extend_schema(
        summary="Update a maintenance announcement",
        description="Updates an existing maintenance announcement.",
    ),
    partial_update=extend_schema(
        summary="Partially update a maintenance announcement",
        description="Partially updates an existing maintenance announcement.",
    ),
    destroy=extend_schema(
        summary="Delete a maintenance announcement",
        description="Deletes a maintenance announcement.",
    ),
)
class MaintenanceAnnouncementViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncement.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementFilter
    serializer_class = serializers.MaintenanceAnnouncementSerializer

    create_permissions = [
        marketplace_permissions.check_maintenance_announcement_create_permissions
    ]
    update_permissions = partial_update_permissions = destroy_permissions = (
        schedule_permissions
    ) = unschedule_permissions = start_maintenance_permissions = (
        complete_maintenance_permissions
    ) = cancel_maintenance_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT,
            ["service_provider.customer", "service_provider"],
        )
    ]

    update_validators = partial_update_validators = [
        core_validators.StateValidator(
            MaintenanceState.DRAFT,
            MaintenanceState.SCHEDULED,
            MaintenanceState.IN_PROGRESS,
            state_enum=MaintenanceState,
        )
    ]

    schedule_validators = [
        core_validators.StateValidator(
            MaintenanceState.DRAFT, state_enum=MaintenanceState
        )
    ]

    @extend_schema(
        summary="Schedule/publish the maintenance announcement",
        description="Transitions a 'Draft' maintenance announcement to the 'Scheduled' state, making it publicly visible.",
        request=None,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def schedule(self, request, uuid=None):
        maintenance: models.MaintenanceAnnouncement = self.get_object()

        maintenance.schedule()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been scheduled"},
            status=status.HTTP_200_OK,
        )

    unschedule_validators = [
        core_validators.StateValidator(
            MaintenanceState.SCHEDULED, state_enum=MaintenanceState
        )
    ]

    @extend_schema(
        summary="Unschedule/unpublish the maintenance announcement",
        description="Transitions a 'Scheduled' maintenance announcement back to the 'Draft' state, hiding it from public view.",
        request=None,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def unschedule(self, request, uuid=None):
        maintenance: models.MaintenanceAnnouncement = self.get_object()

        maintenance.unschedule()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been unscheduled"},
            status=status.HTTP_200_OK,
        )

    start_maintenance_validators = [
        core_validators.StateValidator(
            MaintenanceState.SCHEDULED, state_enum=MaintenanceState
        )
    ]

    @extend_schema(
        summary="Start the maintenance announcement",
        description="Transitions a 'Scheduled' maintenance announcement to 'In progress', indicating that the maintenance work has begun.",
        request=None,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def start_maintenance(self, request, uuid=None):
        maintenance: models.MaintenanceAnnouncement = self.get_object()

        maintenance.start_maintenance()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been started"},
            status=status.HTTP_200_OK,
        )

    complete_maintenance_validators = [
        core_validators.StateValidator(
            MaintenanceState.IN_PROGRESS, state_enum=MaintenanceState
        )
    ]

    @extend_schema(
        summary="Complete the maintenance announcement",
        description="Transitions an 'In progress' maintenance announcement to 'Completed', indicating that the maintenance work has finished.",
        request=None,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def complete_maintenance(self, request, uuid=None):
        maintenance: models.MaintenanceAnnouncement = self.get_object()

        maintenance.complete_maintenance()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been completed"},
            status=status.HTTP_200_OK,
        )

    cancel_maintenance_validators = [
        core_validators.StateValidator(
            MaintenanceState.DRAFT,
            MaintenanceState.SCHEDULED,
            state_enum=MaintenanceState,
        )
    ]

    @extend_schema(
        summary="Cancel the maintenance announcement",
        description="Transitions a 'Draft' or 'Scheduled' maintenance announcement to 'Cancelled'.",
        request=None,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def cancel_maintenance(self, request, uuid=None):
        maintenance: models.MaintenanceAnnouncement = self.get_object()

        maintenance.cancel_maintenance()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been cancelled"},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Get maintenance announcement statistics",
        description="Returns comprehensive statistics for maintenance announcements including counts by state, type, impact level, and daily breakdown.",
        parameters=[
            OpenApiParameter(
                name="start",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Start date in YYYY-MM-DD format. Defaults to 90 days ago.",
            ),
            OpenApiParameter(
                name="end",
                type=str,
                location=OpenApiParameter.QUERY,
                description="End date in YYYY-MM-DD format. Defaults to 30 days in the future.",
            ),
            OpenApiParameter(
                name="provider_uuid",
                type=str,
                location=OpenApiParameter.QUERY,
                description="Filter by service provider UUID.",
            ),
        ],
        responses=serializers.MaintenanceStatsResponseSerializer,
    )
    @action(detail=False, methods=["get"])
    def maintenance_stats(self, request, *args, **kwargs):
        """Return comprehensive maintenance statistics for reporting dashboards."""

        # Parse date parameters
        start_str = request.query_params.get("start")
        end_str = request.query_params.get("end")

        if start_str:
            try:
                start_date = datetime.datetime.strptime(start_str, "%Y-%m-%d").date()
            except ValueError:
                raise rf_exceptions.ValidationError(
                    "start must be in YYYY-MM-DD format"
                )
        else:
            start_date = (timezone.now() - datetime.timedelta(days=90)).date()

        if end_str:
            try:
                end_date = datetime.datetime.strptime(end_str, "%Y-%m-%d").date()
            except ValueError:
                raise rf_exceptions.ValidationError("end must be in YYYY-MM-DD format")
        else:
            end_date = (timezone.now() + datetime.timedelta(days=30)).date()

        # Build base queryset with role-based filtering applied
        # GenericRoleFilter ensures users only see announcements they have access to:
        # - Staff: all announcements
        # - Service provider owners/managers: their provider's announcements
        # - Project members: announcements affecting offerings they consume
        queryset = self.filter_queryset(self.get_queryset()).filter(
            scheduled_start__date__gte=start_date,
            scheduled_end__date__lte=end_date,
        )

        # Apply optional provider filter
        provider_uuid = request.query_params.get("provider_uuid")
        if provider_uuid:
            queryset = queryset.filter(service_provider__uuid=provider_uuid)

        # Calculate summary stats
        total = queryset.count()

        # Count currently active (in progress)
        active_count = queryset.filter(state=MaintenanceState.IN_PROGRESS).count()

        # Count scheduled (upcoming)
        scheduled_count = queryset.filter(state=MaintenanceState.SCHEDULED).count()

        # Calculate average duration (for completed maintenances with actual times)
        completed_with_times = queryset.filter(
            state=MaintenanceState.COMPLETED,
            actual_start__isnull=False,
            actual_end__isnull=False,
        )
        avg_duration_hours = None
        if completed_with_times.exists():
            durations = []
            for m in completed_with_times:
                duration = (m.actual_end - m.actual_start).total_seconds() / 3600
                durations.append(duration)
            if durations:
                avg_duration_hours = sum(durations) / len(durations)

        # On-time completion rate (completed within scheduled window)
        completed_count = queryset.filter(state=MaintenanceState.COMPLETED).count()
        on_time_count = queryset.filter(
            state=MaintenanceState.COMPLETED,
            actual_end__isnull=False,
            actual_end__lte=F("scheduled_end"),
        ).count()
        on_time_rate = (
            (on_time_count / completed_count * 100) if completed_count > 0 else None
        )

        # On-time rate within a 15-minute tolerance (fraction 0-1): completed
        # maintenances that finished no later than 15 min past scheduled_end.
        completed_within_15min = queryset.filter(
            state=MaintenanceState.COMPLETED,
            actual_end__isnull=False,
            actual_end__lte=F("scheduled_end")
            + models.MaintenanceAnnouncement.TIMING_TOLERANCE,
        ).count()
        on_time_rate_15min = (
            completed_within_15min / completed_count if completed_count > 0 else None
        )

        # Mean overrun (hours) across completed maintenances that ran over.
        overrun_agg = queryset.filter(
            state=MaintenanceState.COMPLETED,
            actual_end__isnull=False,
            actual_end__gt=F("scheduled_end"),
        ).aggregate(
            avg=Avg(
                ExpressionWrapper(
                    F("actual_end") - F("scheduled_end"),
                    output_field=DurationField(),
                )
            )
        )
        avg_overrun = overrun_agg["avg"]
        avg_overrun_hours = avg_overrun.total_seconds() / 3600 if avg_overrun else None

        emergency_count = queryset.filter(
            maintenance_type=MaintenanceType.EMERGENCY
        ).count()

        # State counts
        state_counts_raw = dict(
            queryset.values("state")
            .annotate(count=Count("id"))
            .values_list("state", "count")
        )
        state_int_to_label = dict(MaintenanceState.CHOICES)
        state_counts = {
            state_int_to_label.get(state, str(state)): count
            for state, count in state_counts_raw.items()
        }

        # Type counts
        type_counts_raw = dict(
            queryset.values("maintenance_type")
            .annotate(count=Count("id"))
            .values_list("maintenance_type", "count")
        )
        type_int_to_label = dict(MaintenanceType.CHOICES)
        type_counts = {
            type_int_to_label.get(t, str(t)): count
            for t, count in type_counts_raw.items()
        }

        # Impact level counts (max impact per announcement)
        # We need to get the max impact level from affected offerings
        impact_counts = {label: 0 for _, label in ImpactLevel.CHOICES}
        for announcement in queryset.prefetch_related("affected_offerings"):
            max_impact = 1  # Default: No impact
            for offering in announcement.affected_offerings.all():
                if offering.impact_level and offering.impact_level > max_impact:
                    max_impact = offering.impact_level
            impact_label = dict(ImpactLevel.CHOICES).get(max_impact, "No impact")
            impact_counts[impact_label] = impact_counts.get(impact_label, 0) + 1

        # Daily breakdown
        daily_stats = list(
            queryset.annotate(date=TruncDate("scheduled_start"))
            .values("date")
            .annotate(count=Count("id"))
            .order_by("date")
        )

        # Daily state breakdown
        daily_state_counts = {}
        state_by_day = (
            queryset.annotate(date=TruncDate("scheduled_start"))
            .values("date", "state")
            .annotate(count=Count("id"))
        )
        for row in state_by_day:
            date_str = row["date"].isoformat()
            if date_str not in daily_state_counts:
                daily_state_counts[date_str] = {}
            state_label = state_int_to_label.get(row["state"], str(row["state"]))
            daily_state_counts[date_str][state_label] = row["count"]

        # Combine daily data
        daily_data = []
        for row in daily_stats:
            date_str = row["date"].isoformat()
            daily_data.append(
                {
                    "date": row["date"],
                    "count": row["count"],
                    "by_state": daily_state_counts.get(date_str, {}),
                }
            )

        # Provider breakdown
        providers = list(
            queryset.values(
                "service_provider__uuid",
                "service_provider__customer__name",
            )
            .annotate(
                total=Count("id"),
                active=Count("id", filter=Q(state=MaintenanceState.IN_PROGRESS)),
                scheduled=Count("id", filter=Q(state=MaintenanceState.SCHEDULED)),
                completed=Count("id", filter=Q(state=MaintenanceState.COMPLETED)),
            )
            .order_by("-total")
        )

        result = {
            "summary": {
                "total": total,
                "active": active_count,
                "scheduled": scheduled_count,
                "completed": completed_count,
                "average_duration_hours": avg_duration_hours,
                "on_time_completion_rate": on_time_rate,
                "on_time_rate_15min": on_time_rate_15min,
                "avg_overrun_hours": avg_overrun_hours,
                "emergency_count": emergency_count,
            },
            "by_state": state_counts,
            "by_type": type_counts,
            "by_impact_level": impact_counts,
            "daily": daily_data,
            "providers": [
                {
                    "uuid": str(p["service_provider__uuid"]),
                    "name": p["service_provider__customer__name"],
                    "total": p["total"],
                    "active": p["active"],
                    "scheduled": p["scheduled"],
                    "completed": p["completed"],
                }
                for p in providers
                if p["service_provider__uuid"]
            ],
        }

        serializer = serializers.MaintenanceStatsResponseSerializer(result)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        summary="List affected offerings for maintenance",
        description="Returns a paginated list of offerings affected by maintenance announcements.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an affected offering link",
        description="Returns the details of a specific link between a maintenance announcement and an offering, including the impact level and description.",
    ),
    create=extend_schema(
        summary="Link an offering to a maintenance announcement",
        description="Creates a new association between an offering and a maintenance announcement, specifying the expected impact.",
    ),
    update=extend_schema(
        summary="Update an affected offering link",
        description="Updates the impact level or description for an offering linked to a maintenance announcement.",
    ),
    partial_update=extend_schema(
        summary="Partially update an affected offering link",
        description="Partially updates the impact level or description for an offering linked to a maintenance announcement.",
    ),
    destroy=extend_schema(
        summary="Unlink an offering from a maintenance announcement",
        description="Removes the association between an offering and a maintenance announcement.",
    ),
)
class MaintenanceAnnouncementOfferingViewSet(core_views.ActionsViewSet):
    """
    Manage the relationship between maintenance announcements and the specific offerings they affect.

    This viewset allows service providers to specify which of their offerings will be impacted
    by a maintenance event, and to describe the level and nature of that impact.
    """

    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementOffering.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.MaintenanceAnnouncementOfferingSerializer

    create_permissions = [
        marketplace_permissions.check_maintenance_announcement_offering_create_permissions
    ]
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT,
            [
                "maintenance.service_provider.customer",
                "maintenance.service_provider",
            ],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List maintenance announcement templates",
        description="Returns a paginated list of reusable templates for maintenance announcements.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a maintenance announcement template",
        description="Returns the details of a specific maintenance announcement template.",
    ),
    create=extend_schema(
        summary="Create a maintenance announcement template",
        description="Creates a new reusable template for maintenance announcements, including a default message and type.",
    ),
    update=extend_schema(
        summary="Update a maintenance announcement template",
        description="Updates an existing maintenance announcement template.",
    ),
    partial_update=extend_schema(
        summary="Partially update a maintenance announcement template",
        description="Partially updates an existing maintenance announcement template.",
    ),
    destroy=extend_schema(
        summary="Delete a maintenance announcement template",
        description="Deletes a maintenance announcement template.",
    ),
)
class MaintenanceAnnouncementTemplateViewSet(core_views.ActionsViewSet):
    """
    Manage reusable templates for maintenance announcements.

    Templates allow service providers to quickly create new maintenance announcements
    by pre-filling common information, such as the message format and maintenance type.
    """

    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementTemplate.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementTemplateFilter
    serializer_class = serializers.MaintenanceAnnouncementTemplateSerializer

    create_permissions = [
        marketplace_permissions.check_maintenance_announcement_create_permissions
    ]
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT,
            ["service_provider.customer", "service_provider"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List affected offering templates",
        description="Returns a paginated list of associations between maintenance announcement templates and offerings.",
    ),
    retrieve=extend_schema(
        summary="Retrieve an affected offering template link",
        description="Returns the details of a specific link between a maintenance announcement template and an offering.",
    ),
    create=extend_schema(
        summary="Link an offering to a maintenance template",
        description="Creates a reusable association between an offering and a maintenance announcement template, specifying a default impact level and description.",
    ),
    update=extend_schema(
        summary="Update an affected offering template link",
        description="Updates the default impact level or description for an offering linked to a maintenance template.",
    ),
    partial_update=extend_schema(
        summary="Partially update an affected offering template link",
        description="Partially updates the default impact level or description for an offering linked to a maintenance template.",
    ),
    destroy=extend_schema(
        summary="Unlink an offering from a maintenance template",
        description="Removes the association between an offering and a maintenance announcement template.",
    ),
)
class MaintenanceAnnouncementOfferingTemplateViewSet(core_views.ActionsViewSet):
    """
    Manage the default relationships between maintenance announcement templates and offerings.

    This allows service providers to pre-configure which offerings are typically affected
    by a certain type of maintenance, streamlining the creation of new announcements.
    When a new announcement is created from a template, these associations can be
    automatically applied.
    """

    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementOfferingTemplate.objects.all().order_by(
        "-created"
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementOfferingTemplateFilter
    serializer_class = serializers.MaintenanceAnnouncementOfferingTemplateSerializer

    create_permissions = [
        marketplace_permissions.check_maintenance_announcement_offering_template_create_permissions
    ]
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT,
            [
                "maintenance_template.service_provider.customer",
                "maintenance_template.service_provider",
            ],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List Terms of Service configurations",
        description="Returns a paginated list of Terms of Service configurations for offerings. Visibility depends on user permissions: staff/support see all; service providers see their own; regular users see ToS for offerings they have consented to or shared offerings.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a Terms of Service configuration",
        description="Returns the details of a specific Terms of Service configuration.",
    ),
    create=extend_schema(
        summary="Create a Terms of Service configuration",
        description="Creates a new Terms of Service configuration for an offering. Only one active ToS configuration is allowed per offering.",
        examples=[
            OpenApiExample(
                "Create a new active ToS for an offering",
                value={
                    "offering": "http://testserver/api/marketplace-provider-offerings/a1b2c3d4-e5f6-7890-1234-567890abcdef/",
                    "terms_of_service": "<h1>New Terms</h1><p>Users must agree to these terms...</p>",
                    "version": "2.0",
                    "is_active": True,
                    "requires_reconsent": True,
                },
            )
        ],
    ),
    update=extend_schema(
        summary="Update a Terms of Service configuration",
        description="Updates an existing Terms of Service configuration. Note that some fields like `version` and `requires_reconsent` are protected and cannot be changed after creation.",
    ),
    partial_update=extend_schema(
        summary="Partially update a Terms of Service configuration",
        description="Partially updates an existing Terms of Service configuration.",
    ),
    destroy=extend_schema(
        summary="Delete a Terms of Service configuration",
        description="Deletes a Terms of Service configuration. This is a hard delete and should be used with caution.",
    ),
)
class ProviderOfferingToSManagementViewset(core_views.ActionsViewSet):
    """
    Manage Terms of Service (ToS) configurations for marketplace offerings.

    This viewset allows service providers to define and manage the Terms of Service
    that users must accept before consuming an offering. It supports versioning,
    activation, and requiring users to re-consent when terms are updated.
    """

    queryset = models.OfferingTermsOfService.objects.all()
    serializer_class = serializers.OfferingTermsOfServiceSerializer
    create_serializer_class = serializers.OfferingTermsOfServiceCreateSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingTermsOfServiceFilter

    def get_queryset(self):
        """Filter queryset based on user permissions."""
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset
        consented_offerings = models.UserOfferingConsent.objects.filter(
            user=user, revocation_date__isnull=True
        ).values_list("offering_id", flat=True)
        customers = get_connected_customers(user)
        return self.queryset.filter(
            Q(offering__customer__in=customers)
            | Q(offering__id__in=consented_offerings)
            | Q(is_active=True, offering__shared=True)
        )

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = serializer.validated_data.get("offering")
        if not offering:
            raise PermissionDenied()
        if has_permission(
            request, PermissionEnum.UPDATE_OFFERING, offering
        ) or has_permission(request, PermissionEnum.UPDATE_OFFERING, offering.customer):
            return
        raise PermissionDenied()

    create_permissions = [check_create_permissions]
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["offering.customer"],
        )
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List user offering consents",
        description="Returns a paginated list of Terms of Service consents for the current user. Staff and support users can see all consents.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a user offering consent",
        description="Returns the details of a specific consent record.",
    ),
    create=extend_schema(
        summary="Grant consent to an offering's Terms of Service",
        description="Creates a consent record for the current user and a specific offering. This indicates that the user has accepted the active Terms of Service for that offering. If a consent already exists (even if revoked), it will be reactivated and updated with the current ToS version.",
        examples=[
            OpenApiExample(
                "Grant consent to an offering",
                value={"offering": "a1b2c3d4-e5f6-7890-1234-567890abcdef"},
            )
        ],
    ),
)
class UserOfferingConsentViewSet(core_views.ActionsViewSet):
    """
    Manage user consent to Terms of Service for offerings.

    Provides endpoints for:
    - Granting consent to an offering's active Terms of Service.
    - Revoking previously granted consent.
    - Listing and filtering consent records for a user or offering.

    This is a critical component for ensuring compliance and tracking user agreements.
    """

    queryset = models.UserOfferingConsent.objects.all()
    serializer_class = serializers.UserOfferingConsentSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.UserOfferingConsentFilter

    def get_queryset(self):
        """Filter queryset based on user permissions."""
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset

        return self.queryset.filter(user=user)

    create_serializer_class = serializers.UserOfferingConsentCreateSerializer

    @extend_schema(
        summary="Revoke consent to Terms of Service",
        description="Revokes a user's consent to the Terms of Service for an offering. The consent record is marked with a revocation date, and the user may lose access to related resources if consent is required.",
        request=None,
        responses={status.HTTP_200_OK: serializers.UserOfferingConsentSerializer},
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, uuid=None):
        """Revoke consent to Terms of Service."""
        consent = self.get_object()

        if not request.user.is_staff and consent.user != request.user:
            raise PermissionDenied("You don't have permission to revoke this consent.")

        consent.revoke()
        serializer = self.get_serializer(consent)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        summary="List public maintenance announcements",
        description="Returns a paginated list of public maintenance announcements. Only announcements that are 'Scheduled', 'In progress', or 'Completed' are visible. This endpoint is accessible to unauthenticated users.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a public maintenance announcement",
        description="Returns the details of a specific public maintenance announcement.",
    ),
)
class PublicMaintenanceAnnouncementViewSet(
    PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet
):
    """
    Provides public, read-only access to maintenance announcements.

    This viewset allows all users, including anonymous ones, to view upcoming and ongoing
    maintenance events. It exposes a limited set of fields, excluding sensitive
    information like the creator of the announcement.
    """

    lookup_field = "uuid"
    serializer_class = serializers.PublicMaintenanceAnnouncementSerializer
    permission_classes = (rf_permissions.AllowAny,)
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.MaintenanceAnnouncementFilter

    def get_queryset(self):
        """Filter to only show scheduled, in-progress, and completed maintenance announcements."""
        return models.MaintenanceAnnouncement.objects.filter(
            state__in=[
                MaintenanceState.SCHEDULED,
                MaintenanceState.IN_PROGRESS,
                MaintenanceState.COMPLETED,
            ]
        ).order_by("-scheduled_start")


@extend_schema_view(
    list=extend_schema(
        summary="List course accounts",
        description="Returns a paginated list of course accounts accessible to the current user.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a course account",
        description="Returns the details of a specific course account.",
    ),
    create=extend_schema(
        summary="Create a course account",
        description="Creates a new temporary course account within a specified course project.",
        examples=[
            OpenApiExample(
                "Create a course account",
                value={
                    "project": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "email": "student@example.com",
                    "description": "Account for summer course",
                },
            )
        ],
    ),
    destroy=extend_schema(
        summary="Delete (close) a course account",
        description="Deletes a course account, which triggers a 'close' operation in the backend.",
    ),
)
class CourseAccountViewSet(core_views.ActionsViewSet):
    queryset = models.CourseAccount.objects.select_related(
        "project__customer", "user"
    ).all()
    serializer_class = serializers.CourseAccountSerializer
    filterset_class = filters.CourseAccountFilter
    filter_backends = (DjangoFilterBackend,)
    lookup_field = "uuid"

    disabled_actions = ["update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs

        projects = get_connected_projects_by_permission(
            user, PermissionEnum.MANAGE_COURSE_ACCOUNT
        )
        if projects:
            return qs.filter(project__in=projects)
        return qs.none()

    def check_create_permissions(request, view, obj=None):
        # For browsable API to show the form, we need to allow the permission check
        # without parsing request.data. The actual data validation will happen
        # during perform_create.

        # Check if this is a browsable API permission check (POST without data)
        # or a real POST with data
        is_browsable_api_check = request.method == "POST" and (
            not hasattr(request, "_full_data") or not request.data
        )

        if request.method != "POST" or is_browsable_api_check:
            # For OPTIONS, GET, or browsable API checks, just check if user can create in general
            if request.user.is_authenticated and (
                request.user.is_staff or getattr(request.user, "is_support", False)
            ):
                return
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")
            # Check if user has permission to any course project
            projects = get_connected_projects_by_permission(
                request.user, PermissionEnum.MANAGE_COURSE_ACCOUNT
            )
            if not projects:
                raise PermissionDenied(
                    "You don't have permission to create course accounts"
                )
            return

        # For actual POST requests with data, validate the data
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data.get("project")
        if not project:
            raise PermissionDenied()
        if not (
            has_permission(request, PermissionEnum.MANAGE_COURSE_ACCOUNT, project)
            or has_permission(
                request, PermissionEnum.MANAGE_COURSE_ACCOUNT, project.customer
            )
        ):
            raise PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [structure_permissions.is_owner]
    destroy_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_COURSE_ACCOUNT,
            ["project", "project.customer"],
        )
    ]

    def perform_create(self, serializer):
        owner_username = self.request.user.username
        try:
            data = serializer.validated_data
            response_data = utils.create_course_account(data, owner_username)
            user, _ = core_models.User.objects.get_or_create(
                username=response_data["tempAccount"]["username"],
                defaults={
                    "email": response_data["tempAccount"]["email"],
                    "description": "Course Account",
                },
            )
            instance = serializer.save()
            instance.user = user
            instance.save(update_fields=["user"])
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            if "instance" in locals():
                instance.set_state_erred()
                instance.error_message = str(error_details)
                instance.error_traceback = traceback.format_exc()
                instance.save(
                    update_fields=["state", "error_message", "error_traceback"]
                )
            raise ValidationError({"detail": str(error_details)})

    def perform_destroy(self, instance):
        try:
            utils.close_course_account(instance)
        except httpx.HTTPError as exc:
            error_details = utils.extract_error_details_from_httpx_error(exc)
            raise ValidationError({"detail": error_details})

    destroy_validators = [
        core_validators.StateValidator(CourseAccountState.OK, CourseAccountState.ERRED)
    ]

    @extend_schema(
        summary="Retry a failed course account",
        request=None,
        responses={202: serializers.CourseAccountSerializer},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, uuid=None):
        instance = self.get_object()
        instance.error_message = ""
        instance.error_traceback = ""
        instance.set_state_pending()
        instance.save(update_fields=["state", "error_message", "error_traceback"])

        transaction.on_commit(
            lambda: tasks.create_course_account_task.delay(
                instance.uuid.hex, request.user.username
            )
        )

        serializer = self.get_serializer(instance)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    retry_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_COURSE_ACCOUNT,
            ["project", "project.customer"],
        )
    ]
    retry_validators = [core_validators.StateValidator(CourseAccountState.ERRED)]

    @extend_schema(
        summary="Bulk create course accounts",
        description="Creates multiple course accounts within a specified course project in a single request.",
        request=serializers.CourseAccountsBulkCreateSerializer,
        responses={200: serializers.CourseAccountSerializer(many=True)},
        examples=[
            OpenApiExample(
                "Bulk create three course accounts",
                value={
                    "project": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "course_accounts": [
                        {"email": "student1@example.com", "description": "Student One"},
                        {"email": "student2@example.com", "description": "Student Two"},
                        {"email": "student3@example.com"},
                    ],
                },
            )
        ],
    )
    @action(detail=False, methods=["post"])
    def create_bulk(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project = serializer.validated_data["project"]
        owner_username = request.user.username

        # Deduplicate: unique emails in this request
        seen_emails: set[str] = set()
        unique_accounts_data = []
        for account_data in serializer.validated_data["course_accounts"]:
            email = account_data["email"]
            if email not in seen_emails:
                seen_emails.add(email)
                unique_accounts_data.append(account_data)

        # Skip emails already recorded in Waldur for this project
        existing_emails = set(
            models.CourseAccount.objects.filter(
                project=project, email__in=seen_emails
            ).values_list("email", flat=True)
        )
        new_accounts_data = [
            a for a in unique_accounts_data if a["email"] not in existing_emails
        ]

        # Create placeholder CourseAccount records in PENDING state, then enqueue
        # one Celery task per record so each is processed independently.
        created_accounts = []
        with transaction.atomic():
            for account_data in new_accounts_data:
                course_account = models.CourseAccount.objects.create(
                    project=project,
                    email=account_data["email"],
                    description=account_data.get("description", ""),
                    state=CourseAccountState.PENDING,
                )
                created_accounts.append(course_account)

        for course_account in created_accounts:
            uuid_hex = course_account.uuid.hex
            transaction.on_commit(
                lambda hex=uuid_hex: tasks.create_course_account_task.delay(
                    hex, owner_username
                )
            )

        response_serializer = serializers.CourseAccountSerializer(
            created_accounts, many=True, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)

    create_bulk_permissions = [check_create_permissions]
    create_bulk_serializer_class = serializers.CourseAccountsBulkCreateSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List software catalogs",
        description="Returns a paginated list of available software catalogs, such as EESSI or Spack.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a software catalog",
        description="Returns the details of a specific software catalog, including its name, version, and the number of packages it contains.",
    ),
    create=extend_schema(
        summary="Create a software catalog",
        description="Creates a new software catalog. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a software catalog",
        description="Updates an existing software catalog. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a software catalog",
        description="Partially updates an existing software catalog. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a software catalog",
        description="Deletes a software catalog. Requires staff permissions.",
    ),
)
class SoftwareCatalogViewSet(
    PublicViewsetMixin,
    EagerLoadMixin,
    RestrictedSerializerMixin,
    core_views.ActionsViewSet,
):
    """ViewSet for SoftwareCatalog model with standard DRF patterns."""

    queryset = models.SoftwareCatalog.objects.all()
    serializer_class = serializers.SoftwareCatalogSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SoftwareCatalogFilter

    unsafe_methods_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Discover available software catalog versions",
        description=(
            "Queries upstream sources (EESSI, Spack) for available catalog versions "
            "without creating anything. Returns detected versions and whether "
            "an update is available compared to existing database records."
        ),
        responses={200: serializers.SoftwareCatalogDiscoverSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def discover(self, request):
        catalog_sources = [
            {
                "name": "EESSI",
                "catalog_type": "binary_runtime",
                "detect": detect_eessi_version,
                "detect_args": [config.SOFTWARE_CATALOG_EESSI_API_URL],
            },
            {
                "name": "Spack",
                "catalog_type": "source_package",
                "detect": detect_spack_version,
                "detect_args": [config.SOFTWARE_CATALOG_SPACK_DATA_URL],
            },
        ]

        results = []
        for source in catalog_sources:
            entry = {
                "name": source["name"],
                "catalog_type": source["catalog_type"],
                "latest_version": None,
                "existing": False,
                "existing_version": None,
                "update_available": False,
            }

            try:
                entry["latest_version"] = source["detect"](*source["detect_args"])
            except Exception as e:
                logger.warning(f"Could not detect version for {source['name']}: {e}")
                entry["latest_version"] = None

            existing = (
                models.SoftwareCatalog.objects.filter(
                    name=source["name"],
                    catalog_type=source["catalog_type"],
                )
                .order_by("-modified")
                .first()
            )
            if existing:
                entry["existing"] = True
                entry["existing_version"] = existing.version
                entry["update_available"] = (
                    entry["latest_version"] is not None
                    and entry["latest_version"] != existing.version
                )

            results.append(entry)

        response_serializer = serializers.SoftwareCatalogDiscoverSerializer(
            results, many=True
        )
        return Response(response_serializer.data)

    discover_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Import a new software catalog",
        description=(
            "Creates a new catalog record and triggers async data loading via Celery. "
            "Returns 202 Accepted immediately. Staff only."
        ),
        request=serializers.SoftwareCatalogImportSerializer,
        responses={202: None},
    )
    @action(detail=False, methods=["post"])
    def import_catalog(self, request):
        serializer = serializers.SoftwareCatalogImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        name = serializer.validated_data["name"]
        catalog_type = tasks.NAME_TO_CATALOG_TYPE[name]

        if models.SoftwareCatalog.objects.filter(
            name=name, catalog_type=catalog_type
        ).exists():
            raise rf_exceptions.ValidationError(
                f"A catalog with name={name} and type={catalog_type} already exists."
            )

        tasks.import_software_catalog.delay(name, catalog_type)
        return Response(
            {"status": "importing", "name": name},
            status=status.HTTP_202_ACCEPTED,
        )

    import_catalog_permissions = [structure_permissions.is_staff]
    import_catalog_serializer_class = serializers.SoftwareCatalogImportSerializer

    @extend_schema(
        request=None,
        summary="Trigger async update for an existing catalog",
        description=(
            "Triggers a Celery task to update the given catalog from its upstream source. "
            "Returns 202 Accepted immediately. Staff only."
        ),
        responses={202: None},
    )
    @action(detail=True, methods=["post"])
    def update_catalog(self, request, uuid=None):
        catalog = self.get_object()

        catalog_configs = tasks._get_catalog_configs()
        matched = any(
            c["name"] == catalog.name and c["catalog_type"] == catalog.catalog_type
            for c in catalog_configs
        )
        if not matched:
            raise rf_exceptions.ValidationError(
                f"No loader configuration found for {catalog.name} ({catalog.catalog_type})."
            )

        tasks.update_single_software_catalog.delay(catalog.uuid.hex)
        return Response(
            {"status": "updating", "catalog_uuid": str(catalog.uuid)},
            status=status.HTTP_202_ACCEPTED,
        )

    update_catalog_permissions = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List software packages",
        description="Returns a paginated list of software packages available in the catalogs. Can be filtered by catalog, offering, or various package attributes.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a software package",
        description="Returns the details of a specific software package, including its description, homepage, and available versions.",
    ),
    create=extend_schema(
        summary="Create a software package",
        description="Creates a new software package within a catalog. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a software package",
        description="Updates an existing software package. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a software package",
        description="Partially updates an existing software package. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a software package",
        description="Deletes a software package. Requires staff permissions.",
    ),
)
class SoftwarePackageViewSet(
    PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet
):
    """ViewSet for SoftwarePackage model with standard DRF patterns."""

    queryset = models.SoftwarePackage.objects.select_related(
        "catalog"
    ).prefetch_related("versions__targets", "parent_softwares", "extensions")
    serializer_class = serializers.SoftwarePackageSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SoftwarePackageFilter

    unsafe_methods_permissions = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List software versions",
        description="Returns a paginated list of software versions. Can be filtered by package, catalog, offering, or CPU architecture.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a software version",
        description="Returns the details of a specific software version, including its release date and target count.",
    ),
    create=extend_schema(
        summary="Create a software version",
        description="Creates a new version for a software package. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a software version",
        description="Updates an existing software version. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a software version",
        description="Partially updates an existing software version. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a software version",
        description="Deletes a software version. Requires staff permissions.",
    ),
)
class SoftwareVersionViewSet(
    PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet
):
    """ViewSet for SoftwareVersion model with standard DRF patterns."""

    queryset = models.SoftwareVersion.objects.select_related(
        "package__catalog"
    ).prefetch_related("targets")
    serializer_class = serializers.SoftwareVersionSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SoftwareVersionFilter

    unsafe_methods_permissions = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List software targets",
        description="Returns a paginated list of software targets, which represent specific builds of a software version for a given CPU architecture.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a software target",
        description="Returns the details of a specific software target, including its CPU family, microarchitecture, and path.",
    ),
    create=extend_schema(
        summary="Create a software target",
        description="Creates a new target for a software version. Requires staff permissions.",
    ),
    update=extend_schema(
        summary="Update a software target",
        description="Updates an existing software target. Requires staff permissions.",
    ),
    partial_update=extend_schema(
        summary="Partially update a software target",
        description="Partially updates an existing software target. Requires staff permissions.",
    ),
    destroy=extend_schema(
        summary="Delete a software target",
        description="Deletes a software target. Requires staff permissions.",
    ),
)
class SoftwareTargetViewSet(
    PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet
):
    """ViewSet for SoftwareTarget model with standard DRF patterns."""

    queryset = models.SoftwareTarget.objects.select_related("version__package__catalog")
    serializer_class = serializers.SoftwareTargetSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.SoftwareTargetFilter

    unsafe_methods_permissions = [structure_permissions.is_staff]


class DemoPresetViewSet(rf_viewsets.GenericViewSet):
    """
    ViewSet for managing demo data presets.

    Provides read-only access to demo presets stored in the repository,
    plus a load action to apply presets to the database.

    Staff access only.
    """

    # Required for OpenAPI schema generation (no model, using ServiceProvider as placeholder)
    queryset = models.ServiceProvider.objects.none()

    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    serializer_class = serializers.DemoPresetSerializer

    @extend_schema(
        summary="List demo presets",
        description="Returns a list of available demo data presets. Staff access only.",
        responses={status.HTTP_200_OK: serializers.DemoPresetSerializer(many=True)},
    )
    @action(detail=False, methods=["GET"], url_path="list")
    def list_presets(self, request):
        """List all available demo presets."""
        presets = DemoPresetManager.list_presets()
        serializer = serializers.DemoPresetSerializer(presets, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Get demo preset details",
        description="Returns detailed information about a specific demo preset. Staff access only.",
        parameters=[
            OpenApiParameter(
                "name",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Name of the preset",
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.DemoPresetSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    @action(detail=False, methods=["GET"], url_path=r"info/(?P<name>[\w_-]+)")
    def get_preset(self, request, name=None):
        """Get details of a specific preset."""
        preset = DemoPresetManager.get_preset_info(name)

        if not preset:
            raise rf_exceptions.NotFound(f"Preset '{name}' not found")

        serializer = serializers.DemoPresetSerializer(preset)
        return Response(serializer.data)

    @extend_schema(
        summary="Load demo preset",
        description=(
            "Load a demo preset into the database. "
            "This operation will optionally clean up existing data before loading. "
            "Staff access only."
        ),
        parameters=[
            OpenApiParameter(
                "name",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Name of the preset to load",
            ),
        ],
        request=serializers.DemoPresetLoadRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.DemoPresetLoadResponseSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    @action(detail=False, methods=["POST"], url_path=r"load/(?P<name>[\w_-]+)")
    def load_preset(self, request, name=None):
        """Load a preset into the database."""
        # Verify preset exists
        preset = DemoPresetManager.get_preset_info(name)
        if not preset:
            raise rf_exceptions.NotFound(f"Preset '{name}' not found")

        serializer = serializers.DemoPresetLoadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dry_run = serializer.validated_data.get("dry_run", False)
        cleanup_first = serializer.validated_data.get("cleanup_first", True)
        skip_users = serializer.validated_data.get("skip_users", False)
        skip_roles = serializer.validated_data.get("skip_roles", False)

        result = DemoPresetManager.load_preset(
            name=name,
            cleanup_first=cleanup_first,
            dry_run=dry_run,
            skip_users=skip_users,
            skip_roles=skip_roles,
        )

        if not result["success"]:
            raise rf_exceptions.ValidationError(result["message"])

        response_serializer = serializers.DemoPresetLoadResponseSerializer(result)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class ArticleCodeUpdateViewSet(rf_viewsets.GenericViewSet):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    serializer_class = serializers.ArticleCodeUpdatePreviewSerializer
    queryset = models.OfferingComponent.objects.none()

    def _get_filtered_components(self, validated_data):
        qs = models.OfferingComponent.objects.filter(
            article_code__contains=validated_data["search"]
        ).select_related("offering", "offering__customer", "offering__category")

        if "offering_category_uuid" in validated_data:
            qs = qs.filter(
                offering__category__uuid=validated_data["offering_category_uuid"]
            )
        if "offering_customer_uuid" in validated_data:
            qs = qs.filter(
                offering__customer__uuid=validated_data["offering_customer_uuid"]
            )
        if "offering_state" in validated_data:
            qs = qs.filter(offering__state=validated_data["offering_state"])
        if "offering_name" in validated_data:
            qs = qs.filter(offering__name__icontains=validated_data["offering_name"])
        return qs

    @extend_schema(
        summary="Preview article code replacements",
        responses={200: serializers.ArticleCodeUpdatePreviewItemSerializer(many=True)},
    )
    @action(detail=False, methods=["post"])
    def preview(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        components = self._get_filtered_components(serializer.validated_data)
        search = serializer.validated_data["search"]
        replace = serializer.validated_data.get("replace", "")

        items = []
        for comp in components:
            new_code = comp.article_code.replace(search, replace)
            if len(new_code) > 30:
                continue  # Skip components where replacement exceeds max length
            items.append(
                {
                    "component_uuid": comp.uuid,
                    "component_type": comp.type,
                    "component_name": comp.name,
                    "offering_uuid": comp.offering.uuid,
                    "offering_name": comp.offering.name,
                    "offering_customer_name": comp.offering.customer.name,
                    "old_article_code": comp.article_code,
                    "new_article_code": new_code,
                }
            )
        response_serializer = serializers.ArticleCodeUpdatePreviewItemSerializer(
            items, many=True
        )
        return Response(response_serializer.data)

    @extend_schema(
        summary="Apply article code replacements",
        request=serializers.ArticleCodeUpdateApplySerializer,
        responses={200: serializers.ArticleCodeUpdateApplyResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def apply(self, request):
        serializer = serializers.ArticleCodeUpdateApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        search = serializer.validated_data["search"]
        replace = serializer.validated_data.get("replace", "")
        component_uuids = serializer.validated_data["component_uuids"]

        with transaction.atomic():
            components = list(
                models.OfferingComponent.objects.select_for_update().filter(
                    uuid__in=component_uuids, article_code__contains=search
                )
            )
            if len(components) != len(component_uuids):
                raise ValidationError(
                    _(
                        "Some components no longer match the search string. "
                        "Please refresh the preview."
                    )
                )
            for comp in components:
                new_code = comp.article_code.replace(search, replace)
                if len(new_code) > 30:
                    raise ValidationError(
                        _(
                            "Replacement would exceed maximum article code length "
                            "for component '%(name)s' (%(type)s)."
                        )
                        % {"name": comp.name, "type": comp.type}
                    )
                comp.article_code = new_code
            models.OfferingComponent.objects.bulk_update(components, ["article_code"])

        return Response({"updated_count": len(components)})


# ---------------------------------------------------------------------------
# Per-offering usage stats — flat top-level ViewSets in marketplace, lookup
# by customer/project uuid. Registered on the marketplace router via
# marketplace.urls.register_in.
# ---------------------------------------------------------------------------


_OFFERING_UUID_PARAM = OpenApiParameter(
    "offering_uuid",
    OpenApiTypes.UUID,
    location=OpenApiParameter.QUERY,
    required=True,
    extensions={"x-waldur-operation-id": "marketplace_provider_offerings_retrieve"},
)
_COMPONENT_TYPE_PARAM = OpenApiParameter(
    "component_type",
    OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    required=False,
)
_PERIOD_OFFSET_PARAM = OpenApiParameter(
    "period_offset",
    OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    required=False,
)


def _resolve_offering(request):
    offering_uuid = request.query_params.get("offering_uuid")
    if not offering_uuid:
        raise rf_exceptions.ValidationError(
            {"offering_uuid": _("This query parameter is required.")}
        )
    try:
        return models.Offering.objects.get(uuid=offering_uuid)
    except (models.Offering.DoesNotExist, ValueError, DjangoValidationError):
        raise rf_exceptions.NotFound(_("Offering not found."))


def _resolve_period_offset(request) -> int:
    try:
        return int(request.query_params.get("period_offset") or 0)
    except (TypeError, ValueError):
        raise rf_exceptions.ValidationError({"period_offset": _("Must be an integer.")})


class OfferingUsageMixin:
    """Shared logic for customer/project per-offering usage ViewSets.

    Subclasses provide:
    - `queryset` — Customer or Project queryset (the route lookup target)
    - `_scope_resources(scope, offering=None)` — returns the non-terminated
      resources visible at this scope, optionally filtered to one offering
    """

    lookup_field = "uuid"
    filter_backends = (structure_filters.GenericRoleFilter,)
    serializer_class = EmptySerializer

    def _scope_resources(self, scope, offering=None):
        raise NotImplementedError

    @extend_schema(
        summary="Get resource usage statistics broken down per offering",
        description=(
            "Returns one row per (offering, component type, billing type) for "
            "all non-terminated resources within the scope. Each row's "
            "`usage` and `limit_usage` are aggregated using the offering's "
            "own `limit_period`."
        ),
        responses=serializers.ComponentsUsageStatsPerOfferingSerializer,
    )
    @action(detail=True, url_path="components-usage")
    def components_usage(self, request, uuid=None):
        scope = self.get_object()
        resources = filter_queryset_for_user(self._scope_resources(scope), request.user)
        components = get_components_usage_data_per_offering(resources)
        return Response({"components": components}, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Get monthly usage buckets for a single offering",
        description=(
            "Returns a per-month timeseries of `ComponentUsage` for one "
            "offering, restricted to that offering's current `limit_period`. "
            "Buckets are keyed by `billing_period` (always month-start). "
            "`period_offset` shifts the window backward by N periods."
        ),
        parameters=[_OFFERING_UUID_PARAM, _COMPONENT_TYPE_PARAM, _PERIOD_OFFSET_PARAM],
        responses=serializers.OfferingUsageTimeseriesSerializer,
    )
    @action(detail=True, url_path="components-usage-timeseries")
    def components_usage_timeseries(self, request, uuid=None):
        scope = self.get_object()
        offering = _resolve_offering(request)
        period_offset = _resolve_period_offset(request)
        resources = filter_queryset_for_user(
            self._scope_resources(scope, offering=offering), request.user
        )
        data = get_offering_usage_timeseries(
            resources,
            offering,
            request.query_params.get("component_type"),
            period_offset,
        )
        if data is None:
            raise rf_exceptions.NotFound(_("No matching component on this offering."))
        return Response(data, status=status.HTTP_200_OK)


class MarketplaceCustomerUsageViewSet(OfferingUsageMixin, rf_viewsets.GenericViewSet):
    """Customer-scoped usage stats. URL: /api/marketplace-customer-usage/<uuid>/..."""

    queryset = structure_models.Customer.objects.all()

    def _scope_resources(self, customer, offering=None):
        qs = models.Resource.objects.filter(project__customer=customer).exclude(
            state=ResourceStates.TERMINATED
        )
        if offering is not None:
            qs = qs.filter(offering=offering)
        return qs

    @extend_schema(
        summary="Get per-project usage breakdown for a single offering",
        description=(
            "Returns the customer's usage of one offering broken down by "
            "project. Each project entry includes an in-period total `usage` "
            "and a monthly `buckets` array. Projects are sorted by usage "
            "descending."
        ),
        parameters=[_OFFERING_UUID_PARAM, _COMPONENT_TYPE_PARAM, _PERIOD_OFFSET_PARAM],
        responses=serializers.OfferingUsageByProjectSerializer,
    )
    @action(detail=True, url_path="components-usage-by-project")
    def components_usage_by_project(self, request, uuid=None):
        customer = self.get_object()
        offering = _resolve_offering(request)
        period_offset = _resolve_period_offset(request)
        resources = filter_queryset_for_user(
            self._scope_resources(customer, offering=offering).select_related(
                "project"
            ),
            request.user,
        )
        data = get_offering_usage_by_project(
            resources,
            offering,
            request.query_params.get("component_type"),
            period_offset,
        )
        if data is None:
            raise rf_exceptions.NotFound(_("No matching component on this offering."))
        return Response(data, status=status.HTTP_200_OK)


class MarketplaceProjectUsageViewSet(OfferingUsageMixin, rf_viewsets.GenericViewSet):
    """Project-scoped usage stats. URL: /api/marketplace-project-usage/<uuid>/..."""

    queryset = structure_models.Project.objects.all()

    def _scope_resources(self, project, offering=None):
        qs = models.Resource.objects.filter(project=project).exclude(
            state=ResourceStates.TERMINATED
        )
        if offering is not None:
            qs = qs.filter(offering=offering)
        return qs


def user_can_approve_resource_limit_change_request(
    request, view, obj: models.ResourceLimitChangeRequest | None = None
):
    """Only users with UPDATE_RESOURCE_LIMITS on customer or project can approve/reject."""
    if not obj:
        return
    if has_permission(
        request.user,
        PermissionEnum.UPDATE_RESOURCE_LIMITS,
        obj.resource.project.customer,
    ) or has_permission(
        request.user, PermissionEnum.UPDATE_RESOURCE_LIMITS, obj.resource.project
    ):
        return
    raise PermissionDenied()


def check_order_creation_permission_for_limit_change_request(
    request, view, obj: models.ResourceLimitChangeRequest | None = None
):
    """Approving a request submits a marketplace order on the requester's
    behalf, so the approver needs order creation rights as well. Rejecting
    creates nothing and is deliberately not gated on this."""
    if not obj:
        return
    permissions.check_order_creation_permission(request, view, obj.resource)


class ResourceLimitChangeRequestViewSet(EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.ResourceLimitChangeRequest.objects.all()
    serializer_class = serializers.ResourceLimitChangeRequestSerializer
    create_serializer_class = serializers.ResourceLimitChangeRequestCreateSerializer
    # Visibility is fully scoped in get_queryset (privileged scopes or own
    # requests).
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.ResourceLimitChangeRequestFilter
    disabled_actions = ["update", "partial_update", "destroy"]
    lookup_field = "uuid"

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        # Users with UPDATE_RESOURCE_LIMITS on any customer/project see all requests
        # Others only see their own requests
        privileged_customer_ids = UserRole.objects.filter(
            user=user,
            role__permissions__permission=PermissionEnum.UPDATE_RESOURCE_LIMITS,
            content_type__model="customer",
            is_active=True,
        ).values_list("object_id", flat=True)
        privileged_project_ids = UserRole.objects.filter(
            user=user,
            role__permissions__permission=PermissionEnum.UPDATE_RESOURCE_LIMITS,
            content_type__model="project",
            is_active=True,
        ).values_list("object_id", flat=True)
        return qs.filter(
            Q(resource__project__customer_id__in=privileged_customer_ids)
            | Q(resource__project_id__in=privileged_project_ids)
            | Q(created_by=user)
        )

    reject_permissions = [user_can_approve_resource_limit_change_request]

    approve_permissions = reject_permissions + [
        check_order_creation_permission_for_limit_change_request
    ]

    @extend_schema(
        request=ReviewCommentSerializer,
        responses=serializers.OrderUUIDSerializer,
        description="Approve resource limit change request and apply limits via marketplace order.",
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, **kwargs):
        limit_change_request: models.ResourceLimitChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")

        resource = limit_change_request.resource
        requested_limits = limit_change_request.requested_limits

        if resource.state != models.Resource.States.OK:
            raise ValidationError(_("Resource is not in OK state."))

        if resource.limits == requested_limits:
            raise ValidationError(
                _("Requested limits are identical to the current resource limits.")
            )

        utils.validate_limits(requested_limits, resource.offering, resource)

        with transaction.atomic():
            order = models.Order(
                project=resource.project,
                created_by=request.user,
                resource=resource,
                offering=resource.offering,
                plan=resource.plan,
                type=OrderTypes.UPDATE,
                limits=requested_limits,
                attributes={"old_limits": resource.limits},
            )
            serializers.validate_order(order, request)
            order.init_cost()
            order.save()

            limit_change_request.approve(request.user, comment)

        return Response(
            {"order_uuid": order.uuid.hex},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=ReviewCommentSerializer,
        responses={status.HTTP_200_OK: None},
        description="Reject resource limit change request.",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, **kwargs):
        limit_change_request: models.ResourceLimitChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        limit_change_request.reject(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        responses=serializers.OrderInfoResponseSerializer,
        description="Cancel resource limit change request. Only the creator can cancel.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, **kwargs):
        limit_change_request: models.ResourceLimitChangeRequest = self.get_object()
        if limit_change_request.created_by != request.user:
            raise PermissionDenied(
                _("You can only cancel your own resource limit change requests.")
            )
        limit_change_request.cancel()
        return Response(
            {"detail": _("Resource limit change request has been canceled.")},
            status=status.HTTP_200_OK,
        )

    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer
    approve_validators = reject_validators = cancel_validators = [
        core_validators.StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)
    ]


def user_can_approve_resource_end_date_change_request(
    request, view, obj: models.ResourceEndDateChangeRequest | None = None
):
    """Deciding a request needs the same right as setting the date outright.

    SET_RESOURCE_END_DATE, checked against the resource's project and its
    customer, so approving is never harder than doing it yourself and no one can
    reach the outcome while bypassing review.

    Deliberately not ORDER.APPROVE: there is no order here, and that permission
    is also granted on the offering, which would hand a consumer-side decision
    to the provider.
    """
    if not obj:
        return
    if has_permission(
        request.user,
        PermissionEnum.SET_RESOURCE_END_DATE,
        obj.resource.project.customer,
    ) or has_permission(
        request.user, PermissionEnum.SET_RESOURCE_END_DATE, obj.resource.project
    ):
        return
    raise PermissionDenied()


class ResourceEndDateChangeRequestViewSet(EagerLoadMixin, core_views.ActionsViewSet):
    """End date change requests from users who cannot change the date themselves.

    The request records what was asked for; approving it writes the date onto
    the resource. No order is created on any path. Requests are published as
    events so an external approval system can decide instead.
    """

    queryset = models.ResourceEndDateChangeRequest.objects.all()
    serializer_class = serializers.ResourceEndDateChangeRequestSerializer
    create_serializer_class = serializers.ResourceEndDateChangeRequestCreateSerializer
    # Seeing a request follows seeing the resource it concerns: GenericRoleFilter
    # applies the model's Permissions paths, so anyone with a role on the project
    # or its customer sees every request on that resource, and nobody else sees
    # any. Deciding is a separate and narrower question, answered by
    # user_can_approve_resource_end_date_change_request.
    filter_backends = [structure_filters.GenericRoleFilter, DjangoFilterBackend]
    filterset_class = filters.ResourceEndDateChangeRequestFilter
    disabled_actions = ["update", "partial_update", "destroy"]
    lookup_field = "uuid"

    approve_permissions = reject_permissions = [
        user_can_approve_resource_end_date_change_request
    ]

    @extend_schema(
        request=ReviewCommentSerializer,
        responses={status.HTTP_200_OK: None},
        description="Approve resource end date change request and apply the date "
        "to the resource.",
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, **kwargs):
        end_date_request: models.ResourceEndDateChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")

        resource = end_date_request.resource
        requested_end_date = end_date_request.requested_end_date

        if resource.state != models.Resource.States.OK:
            raise ValidationError(_("Resource is not in OK state."))

        if resource.end_date == requested_end_date:
            raise ValidationError(
                _("Requested end date is identical to the current end date.")
            )

        # Re-checked here rather than trusted from creation time, for the same
        # reason the date is: the offering may have stopped accepting these
        # while the request waited — the option turned off, or a prepaid
        # component added, which routes extensions through renewal instead.
        if not utils.offering_allows_end_date_change_requests(resource.offering):
            raise ValidationError(
                _("This offering no longer accepts end date change requests.")
            )

        # Likewise the date: it may have stopped being acceptable while the
        # request waited, for instance because the project end date moved in.
        utils.validate_end_date_for_resource(resource, requested_end_date)

        # The end date is a Waldur-side concept — moving it provisions and
        # releases nothing — so the approval writes it directly. The approver is
        # recorded as the requester of the date, mirroring the direct path.
        with transaction.atomic():
            resource.end_date = requested_end_date
            resource.end_date_requested_by = request.user
            resource.save(update_fields=["end_date", "end_date_requested_by"])

            end_date_request.approve(request.user, comment)

        transaction.on_commit(
            lambda: tasks.notify_about_resource_termination.delay(
                resource.uuid.hex, request.user.uuid.hex, False
            )
        )
        log.log_resource_end_date_has_been_updated(
            resource,
            request.user,
            "End date of marketplace resource %(resource_name)s has been updated"
            " through an approved change request."
            " End date: %(end_date)s."
            " User: %(user)s.",
        )

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=ReviewCommentSerializer,
        responses={status.HTTP_200_OK: None},
        description="Reject resource end date change request.",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, **kwargs):
        end_date_request: models.ResourceEndDateChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        end_date_request.reject(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Set end date change request backend ID",
        description="Records the identifier this request has in an external "
        "approval system, so that system can correlate its own record with the "
        "Waldur request when reporting a verdict.",
        request=serializers.ResourceEndDateChangeRequestBackendIDSerializer,
        responses={status.HTTP_200_OK: StatusSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, **kwargs):
        """Record the external approval system's identifier for this request.

        Deliberately *not* modelled on OrderViewSet.set_backend_id, despite the
        name. That one carries SET_RESOURCE_BACKEND_ID scoped to
        ["offering", "offering.customer"], and every backend id in the
        marketplace is provider-only for the same reason — see
        ResourceBackendIDTest, which asserts the consumer side gets 404.

        This identifier is a different thing: the consumer's own approval
        workflow item reference, on a consumer-owned request. Reusing
        SET_RESOURCE_BACKEND_ID here would make one permission mean
        "provider may set identifiers" in three places and the opposite in a
        fourth, so the approve permission gates it instead. Nothing is granted
        by that in practice: an external approver both correlates and decides,
        and when the decision stays inside Waldur no backend id is ever set.
        """
        end_date_request: models.ResourceEndDateChangeRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data["backend_id"]
        old_backend_id = end_date_request.backend_id
        if new_backend_id != old_backend_id:
            end_date_request.backend_id = new_backend_id
            end_date_request.save(update_fields=["backend_id"])
            logger.info(
                "%s has changed end date change request %s backend_id from %s to %s",
                request.user.full_name,
                end_date_request.uuid.hex,
                old_backend_id,
                new_backend_id,
            )
        return Response(
            {"status": _("Request backend_id has been changed.")},
            status=status.HTTP_200_OK,
        )

    # Not SET_RESOURCE_BACKEND_ID: that permission is provider-scoped
    # everywhere else and this request is consumer-owned. See set_backend_id.
    set_backend_id_permissions = [user_can_approve_resource_end_date_change_request]
    set_backend_id_serializer_class = (
        serializers.ResourceEndDateChangeRequestBackendIDSerializer
    )

    @extend_schema(
        responses=serializers.OrderInfoResponseSerializer,
        description="Cancel resource end date change request. Only the creator can cancel.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, **kwargs):
        end_date_request: models.ResourceEndDateChangeRequest = self.get_object()
        if end_date_request.created_by != request.user:
            raise PermissionDenied(
                _("You can only cancel your own resource end date change requests.")
            )
        end_date_request.cancel()
        return Response(
            {"detail": _("Resource end date change request has been canceled.")},
            status=status.HTTP_200_OK,
        )

    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer
    approve_validators = reject_validators = cancel_validators = (
        set_backend_id_validators
    ) = [core_validators.StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)]


class ProjectOrderAutoApprovalViewSet(core_views.ActionsViewSet):
    """Per-project auto-approval rule for marketplace orders.

    Owners and project managers with APPROVE_ORDER on the project (or its
    customer) may CRUD the rule; staff users may also CRUD even when they
    do not hold APPROVE_ORDER on the scope.
    """

    queryset = models.ProjectOrderAutoApproval.objects.select_related(
        "project", "project__customer", "created_by", "modified_by"
    ).order_by("-created")
    serializer_class = serializers.ProjectOrderAutoApprovalSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ProjectOrderAutoApprovalFilter
    lookup_field = "uuid"

    @staticmethod
    def check_create_permissions(request, view, obj=None):
        user = request.user
        if user.is_staff or user.is_support:
            return
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data.get("project")
        if project is None:
            raise rf_exceptions.PermissionDenied()
        if has_permission(
            request, PermissionEnum.APPROVE_ORDER, project
        ) or has_permission(request, PermissionEnum.APPROVE_ORDER, project.customer):
            return
        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    update_permissions = partial_update_permissions = destroy_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER, ["project", "project.customer"]
        )
    ]
