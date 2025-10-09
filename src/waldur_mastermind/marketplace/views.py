import copy
import datetime
import logging
import textwrap
import traceback
import uuid
from typing import cast

import httpx
import reversion
from constance import config
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.db.models import (
    Count,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    PositiveSmallIntegerField,
    Prefetch,
    Q,
    Subquery,
)
from django.db.models.aggregates import Sum
from django.db.models.fields import FloatField, IntegerField
from django.db.models.functions import Coalesce
from django.db.models.functions.math import Ceil
from django.http.response import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import exceptions as rf_exceptions
from rest_framework import generics, mixins, status, views
from rest_framework import permissions as rf_permissions
from rest_framework import viewsets as rf_viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.mixins import ReviewerChecklistMixin, UserChecklistMixin
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.core.models import User
from waldur_core.core.renderers import PlainTextRenderer
from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.utils import (
    SubqueryCount,
    is_uuid_like,
    month_start,
    order_with_nulls,
)
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.filters import UserPermissionFilter
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
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
    get_project_users,
    get_visible_users,
)
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.analytics import models as analytics_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import serializers as invoice_serializers
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    BillingTypes,
    MaintenanceState,
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
)
from waldur_mastermind.marketplace.utils import (
    get_model_serializer,
    validate_attributes,
)
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.support import models as support_models
from waldur_pid import models as pid_models

from . import filters, log, models, permissions, plugins, serializers, tasks, utils
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

    queryset = queryset.filter(
        # Exclude offerings with disabled OfferingUsers feature
        Q(offering__plugin_options__service_provider_can_create_offering_user=True)
        &
        # user can see own remote offering user
        (
            Q(user=request_user)
            | (
                # service provider can see all records related to managed offerings
                # but only for users with active consent
                (
                    Q(offering__customer__in=managed_customers)
                    | Q(user__in=visible_users)
                )
                & (
                    # only offerings managed by customer where the current user has a role
                    Q(offering__customer__id__in=visible_customers)
                    |
                    # only offerings from organization_groups including the current user's customers
                    Q(offering__organization_groups__in=visible_organization_groups)
                )
            )
        )
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

    return queryset


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

    @extend_schema(responses=serializers.PublicOfferingDetailsSerializer, filters=False)
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


class ServiceProviderViewSet(UserRoleMixin, PublicViewsetMixin, BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all().order_by("customer__name")
    serializer_class = serializers.ServiceProviderSerializer
    filterset_class = filters.ServiceProviderFilter

    @extend_schema(
        operation_id="service_provider_api_secret_code_retrieve",
        description="Return service provider API secret code.",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderApiSecretCodeSerializer
        },
        filters=False,
        methods=["GET"],
    )
    @extend_schema(
        operation_id="service_provider_api_secret_code_generate",
        description="Generate new service provider API secret code.",
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
        request=serializers.SetOfferingsUsernameSerializer,
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
            state__in=(OfferingStates.ACTIVE, OfferingStates.PAUSED),
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
        responses=serializers.NameUUIDSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="customer_name", type=str, location=OpenApiParameter.QUERY
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
        responses=serializers.NameUUIDSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="project_name", type=str, location=OpenApiParameter.QUERY
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


SERVICE_PROVIDER_UUID = OpenApiParameter(
    name="service_provider_uuid",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
)


@extend_schema_view(
    list=extend_schema(
        description="Return customers of service provider.",
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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        description="Return customer projects of service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        description="Return projects of service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderProjectsViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = structure_serializers.ProjectSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.ProjectFilter
    queryset = structure_models.Project.available_objects.all()

    def get_service_provider(self):
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        description="Return project permissions of service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderProjectPermissionsViewSet(
    mixins.ListModelMixin, rf_viewsets.GenericViewSet
):
    serializer_class = structure_serializers.ProjectPermissionLogSerializer
    queryset = UserRole.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = UserPermissionFilter

    def get_service_provider(self):
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        )


@extend_schema_view(
    list=extend_schema(
        description="Return SSH keys of service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderKeysViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = structure_serializers.SshKeySerializer
    queryset = core_models.SshPublicKey.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.SshKeyFilter

    def get_service_provider(self):
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        return self.queryset.filter(user_id__in=user_ids)


@extend_schema_view(
    list=extend_schema(
        description="Return users of service provider.",
        parameters=[SERVICE_PROVIDER_UUID],
    )
)
class ServiceProviderUsersViewSet(mixins.ListModelMixin, rf_viewsets.GenericViewSet):
    serializer_class = serializers.MarketplaceServiceProviderUserSerializer
    queryset = core_models.User.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filterset_class = structure_filters.UserFilter

    def get_service_provider(self):
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        return {
            **context,
            "service_provider": self.get_service_provider(),
        }


@extend_schema_view(
    list=extend_schema(
        description="Return offerings of service provider.",
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
        return models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
        )

    def get_queryset(self):
        return self.queryset.filter(
            customer=self.get_service_provider().customer,
            billable=True,
            shared=True,
        )


@extend_schema_view(
    list=extend_schema(
        description="""Return customers that have access role for a specified user within service provider's scope.

        Checks for:
        - Customers where user has direct permissions
        - Customers with projects where user has project roles
        - Customers related to service provider's resources

        If user UUID is invalid or missing, returns empty list.""",
        parameters=[
            SERVICE_PROVIDER_UUID,
            OpenApiParameter(
                name="user_uuid",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description="UUID of user to get related customers for",
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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        description="Get compliance overview statistics for all offerings managed by this service provider.",
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
        description="List offering users with their compliance status for this service provider.",
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
                description="Filter by offering UUID",
                required=False,
            ),
            OpenApiParameter(
                name="compliance_status",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by compliance status: completed, pending, no_checklist",
                required=False,
            ),
        ],
        methods=["GET"],
    ),
    checklists_summary=extend_schema(
        operation_id="service_provider_checklists_summary",
        description="Get summary of all compliance checklists used by this service provider with usage counts.",
        responses={
            status.HTTP_200_OK: serializers.ServiceProviderChecklistSummarySerializer(
                many=True
            )
        },
        parameters=[SERVICE_PROVIDER_UUID],
        methods=["GET"],
    ),
)
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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
        )
        if not has_permission(
            self.request,
            PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS,
            service_provider.customer,
        ):
            raise PermissionDenied()
        return service_provider

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
            .select_related("category")
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
                    "category_name": checklist.category.name
                    if checklist.category
                    else None,
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
                "category_name": checklist.category.name
                if checklist.category
                else None,
            }
            for checklist in checklists_queryset
        ]

        serializer = serializers.ServiceProviderChecklistSummarySerializer(
            summary_data, many=True
        )
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(
        description="""Return project service accounts that have access to resources managed by the provider.

        Checks for:
        - Projects with active service provider's resources
        - Service accounts with non-blank usernames

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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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
        description="""Return course project accounts that have access to resources managed by the provider.

        Checks for:
        - Projects with active service provider's resources
        - Course accounts with non-blank users

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
        service_provider = models.ServiceProvider.objects.get(
            uuid=self.kwargs["service_provider_uuid"]
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


class CategoryViewSet(PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.MarketplaceCategorySerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryFilter

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


class CategoryColumnsViewSet(PublicViewsetMixin, core_views.ActionsViewSet):
    queryset = models.CategoryColumn.objects.all()
    serializer_class = serializers.CategoryColumnSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryColumnFilter

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


class CategoryGroupViewSet(PublicViewsetMixin, core_views.ActionsViewSet):
    queryset = models.CategoryGroup.objects.all()
    serializer_class = serializers.CategoryGroupSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CategoryGroupFilter

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [structure_permissions.is_staff]


# State transition validators and permissions will be handled by individual methods


def can_update_offering(request, view, obj: models.Offering | None = None):
    offering = obj

    if not offering:
        return

    if offering.state == OfferingStates.DRAFT:
        if any(
            has_permission(request, PermissionEnum.UPDATE_OFFERING, scope)
            for scope in (
                offering,
                offering.customer,
                offering.customer.serviceprovider,
            )
        ):
            return
        else:
            raise rf_exceptions.PermissionDenied()
    else:
        structure_permissions.is_staff(request, view)


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


class ProviderOfferingViewSet(
    UserRoleMixin,
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

        return queryset

    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING,
            ["customer"],
        )
    ]

    def destroy(self, request, *args, **kwargs):
        offering: models.Offering = self.get_object()
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
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        return self._update_state("activate")

    @extend_schema(
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def draft(self, request, uuid=None):
        return self._update_state("draft")

    @extend_schema(
        responses=serializers.OrderDetailsSerializer(many=True), filters=True
    )
    @action(detail=True, methods=["get"], filter_backends=[])
    def orders(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        # Only allow staff, support and offering managers
        if not (request.user.is_staff or request.user.is_support):
            if not (
                offering.has_user(request.user, OfferingRole.MANAGER)
                or offering.customer.has_user(request.user, CustomerRole.OWNER)
            ):
                raise PermissionDenied()
        queryset = models.Order.objects.filter(offering=offering)
        filterset = filters.OrderFilter(request.query_params, queryset=queryset)
        queryset = filterset.qs
        # Paginate queryset
        page = self.paginate_queryset(queryset)
        serializer = serializers.OrderDetailsSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(responses=serializers.OrderDetailsSerializer)
    def order_detail(self, request, uuid=None, order_uuid=None):
        offering: models.Offering = self.get_object()
        if not (request.user.is_staff or request.user.is_support):
            if not (
                offering.has_user(request.user, OfferingRole.MANAGER)
                or offering.customer.has_user(request.user, CustomerRole.OWNER)
            ):
                raise PermissionDenied()
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
        responses=serializers.DetailStateSerializer,
        request=serializers.OfferingPauseSerializer,
    )
    @action(detail=True, methods=["post"])
    def pause(self, request, uuid=None):
        return self._update_state("pause", request)

    pause_serializer_class = serializers.OfferingPauseSerializer

    @extend_schema(
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def unpause(self, request, uuid=None):
        return self._update_state("unpause")

    @extend_schema(
        request=None,
        responses=serializers.DetailStateSerializer,
    )
    @action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        return self._update_state("archive")

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
        permission_factory(
            PermissionEnum.PAUSE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    unpause_permissions = [
        permission_factory(
            PermissionEnum.UNPAUSE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    archive_permissions = [
        permission_factory(
            PermissionEnum.ARCHIVE_OFFERING,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]

    activate_permissions = [structure_permissions.is_staff]

    activate_validators = pause_validators = archive_validators = destroy_validators = [
        structure_utils.check_customer_blocked_or_archived
    ]

    activate_validators += [validate_offering_has_plans]
    unpause_validators = [validate_offering_has_plans]

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
        filters=False,
        description="List importable resources for offering.",
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
        request=dict,
        responses=None,
        description="Update offering attributes.",
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

    @extend_schema(
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
        request=serializers.OfferingThumbnailSerializer,
        description="Update offering thumbnail.",
        responses={200: None},
    )
    @action(detail=True, methods=["post"])
    def update_thumbnail(self, request, uuid=None):
        return self._update_media(request, serializers.OfferingThumbnailSerializer)

    @extend_schema(
        request=None,
        responses={204: None},
        description="Delete offering thumbnail.",
    )
    @action(detail=True, methods=["post"])
    def delete_thumbnail(self, request, uuid=None):
        return self._delete_media("thumbnail")

    @extend_schema(
        request=serializers.OfferingImageSerializer,
        description="Update offering image.",
    )
    @action(detail=True, methods=["post"])
    def update_image(self, request, uuid=None):
        return self._update_media(request, serializers.OfferingImageSerializer)

    @extend_schema(
        request=None,
        responses={200: None},
        description="Delete offering image.",
    )
    @action(detail=True, methods=["post"])
    def delete_image(self, request, uuid=None):
        return self._delete_media("image")

    media_permissions = [permissions.user_can_update_thumbnail]
    update_thumbnail_permissions = media_permissions
    delete_thumbnail_permissions = media_permissions
    update_image_permissions = media_permissions
    delete_image_permissions = media_permissions

    @extend_schema(
        responses=serializers.ProviderOfferingCustomerSerializer(many=True),
        description="Get customers for offering.",
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
        description="Get costs for offering.",
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
        description="Get statistics for offering components.",
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
        request=serializers.OrganizationGroupsSerializer,
        description="Update organization groups for offering.",
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
        request=None,
        responses=None,
        description="Delete organization groups for offering.",
    )
    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        offering.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions
    delete_organization_groups_validators = update_validators

    @extend_schema(
        request=serializers.NestedEndpointSerializer,
        description="Add endpoint to offering.",
        responses={201: serializers.EndpointUUIDSerializer},
    )
    @action(detail=True, methods=["post"])
    def add_endpoint(self, request, uuid=None):
        offering: models.Offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        endpoint = models.OfferingAccessEndpoint.objects.create(
            offering=offering,
            url=serializer.validated_data["url"],
            name=serializer.validated_data["name"],
        )

        return Response(
            {"uuid": endpoint.uuid},
            status=status.HTTP_201_CREATED,
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
        request=serializers.EndpointUUIDSerializer,
        responses={204: None},
        description="Delete endpoint from offering.",
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

    @extend_schema(request=None, responses=str, parameters=[])
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

        integration_status, _ = models.IntegrationStatus.objects.get_or_create(
            offering=offering,
            agent_type=models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
        )
        integration_status.set_last_request_timestamp()
        integration_status.service_name = request.headers.get("User-Agent", "")
        integration_status.set_backend_active()
        integration_status.save()

        offering_users = models.OfferingUser.objects.filter(offering=offering).exclude(
            username=""
        )

        offering_groups = models.OfferingUserGroup.objects.filter(offering=offering)

        user_records = utils.generate_glauth_records_for_offering_users(
            offering, offering_users
        )

        robot_accounts = models.RobotAccount.objects.filter(resource__offering=offering)

        robot_account_records = utils.generate_glauth_records_for_robot_accounts(
            offering, robot_accounts
        )

        other_group_records = []
        for group in offering_groups:
            gid = group.backend_metadata["gid"]
            record = textwrap.dedent(
                f"""
                [[groups]]
                  name = "{gid}"
                  gidnumber = {gid}
            """
            )
            other_group_records.append(record)

        response_text = "\n".join(
            user_records + robot_account_records + other_group_records
        )

        return Response(response_text)

    glauth_users_config_permissions = [structure_permissions.is_offering_manager]

    @extend_schema(
        description="Check if user has access to offering.",
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
                serializer = self.get_serializer(
                    instance=offering_component, data=request.data, partial=True
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(status=status.HTTP_200_OK)
            else:
                return Response(status=status.HTTP_404_NOT_FOUND)
        else:
            return Response(
                {"details": _("UUID for offering component was not provided.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

    update_offering_component_serializer_class = serializers.OfferingComponentSerializer
    update_offering_component_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer", "customer.serviceprovider"],
        )
    ]
    update_offering_component_validators = update_validators

    @extend_schema(
        request=serializers.RemoveOfferingComponentSerializer,
        responses=None,
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
        request=serializers.OfferingComponentSerializer,
        responses=None,
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
        responses=None,
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
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    list_customer_projects_permissions = list_customer_users_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer},
        request=None,
        description="Refresh offering user usernames.",
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
                offering_user.save(update_fields=["username"])

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
        )
        page = self.paginate_queryset(course_accounts)
        serializer = serializers.CourseAccountSerializer(
            instance=page,
            many=True,
            context={"request": request},
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(
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
        description="Return comprehensive ToS consent statistics for this offering.",
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
                scope=offering, name="revoked_consents_today"
            )
            .values("date", "usage")
            .order_by("date")
        )

        tos_version_adoption = list(
            models.UserOfferingConsent.objects.filter(offering=offering)
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
        }

        serializer = serializers.ToSConsentDashboardSerializer(dashboard_data)
        return Response(serializer.data, status=status.HTTP_200_OK)


class PublicOfferingViewSet(rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.Offering.objects.filter()
    lookup_field = "uuid"
    serializer_class = serializers.PublicOfferingDetailsSerializer
    filterset_class = filters.OfferingFilter
    permission_classes = []

    def get_queryset(self):
        user = self.request.user
        return self.queryset.filter_by_ordering_availability_for_user(user)

    @extend_schema(
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

    @extend_schema(responses=serializers.BasePublicPlanSerializer)
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


class OfferingUserRoleViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingUserRole.objects.all()
    serializer_class = serializers.OfferingUserRoleSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserRoleFilter

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        offerings = models.Offering.objects.all().filter_for_user(user)
        return qs.filter(offering__in=offerings)

    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_OFFERING_USER_ROLE,
            ["offering.customer"],
        )
    ]


class ResourceUserViewSet(core_views.ActionsViewSet):
    queryset = models.ResourceUser.objects.all()
    serializer_class = serializers.ResourceUserSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ResourceUserFilter
    disabled_actions = ["update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        resources = models.Resource.objects.all().filter_for_user(user)
        return qs.filter(resource__in=resources)

    unsafe_methods_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_RESOURCE_USERS,
            ["resource.offering.customer"],
        )
    ]


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


class ProviderPlanViewSet(core_views.UpdateReversionMixin, core_views.ActionsViewSet):
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
        request=serializers.PricesUpdateSerializer,
        responses=None,
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
        request=serializers.QuotasUpdateSerializer,
        responses=None,
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

    archive_permissions = [
        permission_factory(
            PermissionEnum.ARCHIVE_OFFERING_PLAN,
            ["offering.customer"],
        )
    ]

    archive_validators = [validate_plan_archive]

    @extend_schema(
        request=None,
        responses=None,
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
        parameters=[
            OpenApiParameter(
                name="offering_uuid", type=str, location=OpenApiParameter.QUERY
            ),
            OpenApiParameter(
                name="customer_provider_uuid", type=str, location=OpenApiParameter.QUERY
            ),
            OpenApiParameter(name="o", type=str, location=OpenApiParameter.QUERY),
        ],
        responses=serializers.PlanUsageResponseSerializer(many=True),
    )
    @action(detail=False)
    def usage_stats(self, request):
        return PlanUsageReporter(self, request).get_report()

    @extend_schema(
        request=serializers.OrganizationGroupsSerializer,
        responses=None,
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
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        plan: models.Plan = self.get_object()
        plan.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions


class PlanComponentViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
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

    @extend_schema(responses={200: serializers.PluginOfferingTypeSerializer(many=True)})
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


class OrderViewSet(ConnectedOfferingDetailsMixin, BaseMarketplaceView):
    queryset = models.Order.objects.all()
    filter_backends = (DjangoFilterBackend,)
    serializer_class = serializers.OrderDetailsSerializer
    create_serializer_class = serializers.OrderCreateSerializer
    filterset_class = filters.OrderFilter
    disabled_actions = ["update", "partial_update"]

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

        return self.queryset.filter(
            Q(project__in=connected_projects)
            | Q(project__customer__in=connected_customers)
            | Q(offering__customer__in=connected_customers)
        ).distinct()

    approve_by_consumer_validators = [
        structure_utils.check_customer_blocked_or_archived,
        structure_utils.check_project_end_date,
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER, state_enum=OrderStates
        ),
    ]

    approve_by_consumer_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["project", "project.customer"],
        )
    ]

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
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def approve_by_consumer(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.review_by_consumer(request.user)
        if (
            order.project.start_date
            and order.project.start_date > timezone.now().date()
        ):
            order.state = OrderStates.PENDING_PROJECT
            order.save(update_fields=["state"])
            return Response(status=status.HTTP_200_OK)
        if utils.order_should_not_be_reviewed_by_provider(order):
            order.set_state_executing()
            order.save(update_fields=["state"])
            logger.info(
                "Processing order %s (%s) after consumer approval, resource %s",
                order,
                order.id,
                order.resource,
            )
            tasks.process_order_on_commit(order, request.user)
        else:
            order.state = OrderStates.PENDING_PROVIDER
            order.save(update_fields=["state"])
            transaction.on_commit(
                lambda: tasks.notify_provider_about_pending_order.delay(order.uuid)
            )
        return Response(status=status.HTTP_200_OK)

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

    @extend_schema(
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def approve_by_provider(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.review_by_provider(request.user)
        order.set_state_executing()
        order.save(update_fields=["state"])
        logger.info(
            "Processing order %s (%s) after provider approval, resource %s",
            order,
            order.id,
            order.resource,
        )
        tasks.process_order_on_commit(order, request.user)
        return Response(status=status.HTTP_200_OK)

    reject_by_consumer_validators = [
        structure_utils.check_customer_blocked_or_archived,
        core_validators.StateValidator(
            OrderStates.PENDING_CONSUMER, state_enum=OrderStates
        ),
    ]

    reject_by_consumer_permissions = [permissions.user_can_reject_order_as_consumer]

    @extend_schema(
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def reject_by_consumer(self, request, uuid=None):
        order: models.Order = self.get_object()
        if permissions.order_should_not_be_reviewed_by_consumer(order):
            raise rf_exceptions.ValidationError(
                "Review of order by consumer is not required."
            )
        if order.consumer_reviewed_by:
            raise rf_exceptions.ValidationError(
                "Order is already reviewed by consumer."
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

    @extend_schema(
        request=None,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def reject_by_provider(self, request, uuid=None):
        order: models.Order = self.get_object()
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
            OrderStates.EXECUTING,
            state_enum=OrderStates,
        ),
        OfferingTypeValidator(BASIC_OFFERING, SUPPORT_OFFERING),
    ]

    @extend_schema(
        request=None,
        responses=None,
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
            ["offering.customer"],
        )
    ]

    @extend_schema(
        request=None,
        responses=None,
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
            ["offering.customer"],
        )
    ]

    @extend_schema(
        request=None,
        responses=None,
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
            ["offering.customer", "offering.customer.serviceprovider"],
        )
    ]

    @extend_schema(
        request=serializers.OrderSetStateErredSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_state_erred(self, request, uuid=None):
        order: models.Order = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        error_message = serializer.validated_data["error_message"]
        error_traceback = serializer.validated_data["error_traceback"]

        callbacks.sync_order_state(order, OrderStates.ERRED)
        order.error_message = error_message
        order.error_traceback = error_traceback
        order.save(update_fields=["error_message", "error_traceback"])
        return Response(status=status.HTTP_200_OK)

    set_state_erred_serializer_class = serializers.OrderSetStateErredSerializer

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
        request=None,
        responses={403: None, 204: None},
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
        request=serializers.OrderAttachmentSerializer,
        responses={200: serializers.OrderAttachmentSerializer},
        description="Update the attachment for a pending order.",
    )
    @action(
        detail=True,
        methods=["POST"],
    )
    def update_attachment(self, request, uuid=None):
        """
        Allows uploading or replacing an attachment for an order.
        """
        order: models.Order = self.get_object()
        serializer = self.get_serializer(order, data=request.data)
        serializer.is_valid(raise_exception=True)

        # If an old file exists, delete it before saving the new one
        if order.attachment:
            order.attachment.delete(save=False)

        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=None,
        responses={204: None},
        description="Delete the attachment from a pending order.",
    )
    @action(
        detail=True,
        methods=["POST"],
    )
    def delete_attachment(self, request, uuid=None):
        """
        Allows deleting an attachment from an order.
        """
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


class BaseResourceViewSet(ConnectedOfferingDetailsMixin, core_views.ActionsViewSet):
    queryset = models.Resource.objects.all()
    filter_backends = (DjangoFilterBackend, filters.ResourceScopeFilterBackend)
    filterset_class = filters.ResourceFilter
    lookup_field = "uuid"
    serializer_class = serializers.ResourceSerializer
    disabled_actions = ["create", "destroy"]
    update_serializer_class = partial_update_serializer_class = (
        serializers.ResourceUpdateSerializer
    )

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
        filters=False,
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
        request=None,
        responses=None,
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
        responses=serializers.OrderUUIDSerializer,
        description="Create marketplace order for resource termination.",
    )
    @action(detail=True, methods=["post"])
    def terminate(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attributes = serializer.validated_data.get("attributes", {})

        return self.create_resource_order(
            request=request,
            resource=resource,
            type=OrderTypes.TERMINATE,
            attributes=attributes,
        )

    terminate_serializer_class = serializers.ResourceTerminateSerializer

    terminate_permissions = [permissions.user_can_terminate_resource]

    terminate_validators = [
        core_validators.StateValidator(ResourceStates.OK, ResourceStates.ERRED),
    ]

    @extend_schema(
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
        description="Move resource to another project.",
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
        description="Set slug for resource.",
        request=serializers.ResourceSlugSerializer,
        responses={status.HTTP_200_OK: None},
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
            resource.save()
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

    def _set_end_date(self, request, is_staff_action):
        resource: models.Resource = self.get_object()
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
        description="Set end date of the resource by staff.",
        request=serializers.ResourceEndDateByProviderSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @action(detail=True, methods=["post"])
    def set_end_date_by_staff(self, request, uuid=None):
        return self._set_end_date(request, True)

    set_end_date_by_staff_permissions = [structure_permissions.is_staff]

    @extend_schema(
        description="""
        This endpoint provides a config file for GLauth.
        Example: https://github.com/glauth/glauth/blob/master/v2/sample-simple.cfg
        It is assumed that the config is used by an external agent,
        which synchronizes data from Waldur to GLauth.
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
        project = resource.project
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

        integration_status, _ = models.IntegrationStatus.objects.get_or_create(
            offering=offering,
            agent_type=models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
        )
        integration_status.set_last_request_timestamp()
        integration_status.service_name = request.headers.get("User-Agent", "")
        integration_status.set_backend_active()
        integration_status.save()

        user_ids = get_project_users(project.id)

        offering_users = models.OfferingUser.objects.filter(
            offering=offering,
            user__id__in=user_ids,
        ).exclude(username="")

        offering_groups = models.OfferingUserGroup.objects.filter(offering=offering)

        user_records = utils.generate_glauth_records_for_offering_users(
            offering, offering_users
        )

        robot_accounts = models.RobotAccount.objects.filter(resource__offering=offering)

        robot_account_records = utils.generate_glauth_records_for_robot_accounts(
            offering, robot_accounts
        )

        other_group_records = []
        for group in offering_groups:
            gid = group.backend_metadata["gid"]
            record = textwrap.dedent(
                f"""
                    [[groups]]
                      name = "{gid}"
                      gidnumber = {gid}
                """
            )
            other_group_records.append(record)

        response_text = "\n".join(
            user_records + robot_account_records + other_group_records
        )

        return Response(response_text)

    @extend_schema(
        filters=False, responses=serializers.SubresourceOfferingSerializer(many=True)
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def offering_for_subresources(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        try:
            scope = structure_models.ServiceSettings.objects.get(
                scope=resource.scope,
            )
        except structure_models.ServiceSettings.DoesNotExist:
            scope = resource.scope

        offerings = models.Offering.objects.filter(scope=scope)
        result = [
            {"uuid": offering.uuid.hex, "type": offering.type} for offering in offerings
        ]
        return Response(result)

    @extend_schema(
        description="Return users connected to the project.",
        request=None,
        responses=serializers.ProjectUserSerializer(many=True),
        filters=False,
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def team(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        project = resource.project

        return Response(
            serializers.ProjectUserSerializer(
                instance=project.get_users(),
                many=True,
                context={
                    "project": project,
                    "offering": resource.offering,
                    "request": request,
                },
            ).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=None,
        responses=dict[str, str],
        description="Starts process of pulling a resource",
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
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer},
        description="Update resource options.",
    )
    @action(detail=True, methods=["post"])
    def update_options(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data, instance=resource)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"status": _("Resource options are submitted")}, status=status.HTTP_200_OK
        )

    update_options_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_OPTIONS,
            ["project", "project.customer", "offering.customer"],
        ),
    ]
    update_options_serializer_class = serializers.ResourceOptionsSerializer


class ConsumerResourceViewSet(BaseResourceViewSet):
    def get_queryset(self):
        queryset = self.queryset.filter_for_service_consumer(self.request.user)
        queryset = filter_queryset_by_user_ip(queryset, self.request)
        return queryset

    @action(detail=False, methods=["post"])
    def suggest_name(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project: structure_models.Project = serializer.validated_data["project"]
        offering: models.Offering = serializer.validated_data["offering"]
        return Response({"name": utils.generate_resource_name(project, offering)})

    suggest_name_serializer_class = serializers.ResourceSuggestNameSerializer

    @extend_schema(
        responses=serializers.OrderUUIDSerializer,
        description="Create marketplace order for resource plan switch.",
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
        responses=serializers.OrderUUIDSerializer,
        description="Create marketplace order for resource limits update.",
    )
    @action(detail=True, methods=["post"])
    def update_limits(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        limits = serializer.validated_data["limits"]

        if resource.limits == limits:
            raise ValidationError(
                "Impossible to create update orders with limits set to exactly the same."
            )

        return self.create_resource_order(
            request=request,
            resource=resource,
            plan=resource.plan,
            type=OrderTypes.UPDATE,
            limits=limits,
            attributes={"old_limits": resource.limits},
        )

    update_limits_serializer_class = serializers.ResourceUpdateLimitsSerializer

    switch_plan_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.SWITCH_RESOURCE_PLAN,
            ["project", "project.customer"],
        ),
    ]

    update_limits_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            ["project", "project.customer"],
        ),
    ]

    switch_plan_validators = update_limits_validators = [
        core_validators.StateValidator(ResourceStates.OK),
    ]

    @extend_schema(
        request=serializers.ResourceRenewSerializer,
        responses={200: serializers.OrderUUIDSerializer},
        description="Create a renewal order for a prepaid resource.",
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
            "action": "renew",
            "old_limits": resource.limits,
            "old_end_date": resource.end_date.isoformat()
            if resource.end_date
            else None,
            "new_end_date": new_end_date.isoformat(),
            "extension_months": extension_months,
            "renewal_cost": float(renewal_cost),  # Store for auditing
        }

        # The renewal cost is passed as 'switch_price' to the order,
        # which is the mechanism for one-time charges on UPDATE orders.
        return self.create_resource_order(
            request=request,
            resource=resource,
            plan=resource.plan,
            type=OrderTypes.UPDATE,
            limits=final_limits,
            attributes=order_attributes,
            switch_price=renewal_cost,
        )

    renew_serializer_class = serializers.ResourceRenewSerializer

    renew_permissions = [
        permissions.check_tos_consent_permission,
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,  # Re-use existing permission
            ["project", "project.customer"],
        ),
    ]

    renew_validators = [
        core_validators.StateValidator(ResourceStates.OK, ResourceStates.ERRED),
    ]


class ProviderResourceViewSet(BaseResourceViewSet):
    def get_queryset(self):
        return self.queryset.filter_for_service_provider(self.request.user)

    @extend_schema(request=serializers.ResourceEndDateByProviderSerializer)
    @action(detail=True, methods=["post"])
    def set_end_date_by_provider(self, request, uuid=None):
        return self._set_end_date(request, False)

    set_end_date_by_provider_permissions = [
        permissions.user_can_set_end_date_by_provider
    ]

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer},
        description="Set resource backend ID.",
    )
    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        resource = cast(models.Resource, self.get_object())
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_backend_id = serializer.validated_data["backend_id"]
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
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer},
        description="Submit resource report.",
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
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer}
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
        responses={status.HTTP_200_OK: None},
        description="Set the resource as erred.",
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
            ["offering.customer"],
        )
    ]

    set_as_erred_serializer_class = serializers.ResourceSetStateErredSerializer

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: None},
        description="Set the resource as OK.",
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

    set_as_erred_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: None},
        description="Refresh the last sync time for a resource.",
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
        responses={status.HTTP_200_OK: serializers.ResourceResponseStatusSerializer}
    )
    @action(detail=True, methods=["post"])
    def set_limits(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_limits = serializer.validated_data["limits"]

        limit_based_components = resource.offering.components.filter(
            billing_type=BillingTypes.LIMIT
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
                    resource.limits[component.type],
                    new_limits[component.type],
                )

        resource.limits = new_limits
        resource.save(update_fields=["limits"])

        return Response(
            {"status": _("The resource limits are updated")}, status=status.HTTP_200_OK
        )

    set_limits_serializer_class = serializers.ResourceSetLimitsSerializer

    refresh_last_sync_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_STATE,
            ["offering.customer"],
        )
    ]


class ResourceOfferingsViewSet(ListAPIView):
    serializer_class = serializers.ResourceOfferingSerializer
    queryset = models.Offering.objects.all()  # used by OpenAPI introspector
    filterset_class = structure_filters.NameFilterSet

    def get_category(self):
        if "category_uuid" not in self.kwargs:
            raise rf_exceptions.ValidationError("Category UUID is required.")
        category_uuid = self.kwargs["category_uuid"]
        if not is_uuid_like(category_uuid):
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data="Category UUID is invalid."
            )
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
        parameters=[
            OpenApiParameter(
                name="project_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the project to filter runtime states by.",
            ),
            OpenApiParameter(
                name="category_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the category to filter runtime states by.",
            ),
        ],
        request=None,
        responses=serializers.RuntimeStatesSerializer(many=True),
        description="Retrieve available runtime states for resources, optionally filtered by project and category.",
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
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data="Customer UUID is invalid."
            )
        qs = filter_queryset_for_user(
            structure_models.Customer.objects.all(), self.request.user
        )
        return get_object_or_404(qs, uuid=customer_uuid)

    def get_queryset(self):
        customer = self.get_customer()
        qs: ResourceQuerySet = models.Resource.objects.all()
        customer_ids = (
            qs.filter_for_service_provider(self.request.user)
            .filter(offering__customer=customer)
            .values_list("project__customer_id", flat=True)
            .distinct()
        )
        return structure_models.Customer.objects.filter(id__in=customer_ids)


class CategoryComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.CategoryComponentUsage.objects.all().order_by(
        "-date", "component__type"
    )
    filter_backends = (
        DjangoFilterBackend,
        filters.CategoryComponentUsageScopeFilterBackend,
    )
    filterset_class = filters.CategoryComponentUsageFilter
    serializer_class = serializers.CategoryComponentUsageSerializer


class ComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.ComponentUsage.objects.all().order_by("-date", "component__type")
    lookup_field = "uuid"
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUsageFilter
    serializer_class = serializers.ComponentUsageSerializer

    @extend_schema(
        request=serializers.ComponentUsageCreateSerializer,
        responses={status.HTTP_201_CREATED: None},
    )
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
        request=serializers.ComponentUserUsageCreateSerializer,
        responses={status.HTTP_201_CREATED: None},
    )
    @action(detail=True, methods=["post"])
    def set_user_usage(self, request, *args, **kwargs):
        component_usage: models.ComponentUsage = self.get_object()
        serializer = self.get_serializer(
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        existing_user_usage = models.ComponentUserUsage.objects.filter(
            component_usage=component_usage, username=validated_data["username"]
        ).first()

        if existing_user_usage is None:
            serializer.validated_data["component_usage"] = component_usage
            serializer.save()
        else:
            existing_user_usage.usage = validated_data["usage"]
            existing_user_usage.save()
        return Response(status=status.HTTP_201_CREATED)

    set_user_usage_serializer_class = serializers.ComponentUserUsageCreateSerializer

    set_user_usage_permissions = [
        permission_factory(
            PermissionEnum.SET_RESOURCE_USAGE,
            ["resource.offering", "resource.offering.customer"],
        )
    ]


class ComponentUserUsageViewSet(core_views.ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    queryset = models.ComponentUserUsage.objects.all().order_by(
        "-component_usage__date", "component_usage__component__type"
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ComponentUserUsageFilter
    serializer_class = serializers.ComponentUserUsageSerializer


class MarketplaceAPIViewSet(rf_viewsets.ViewSet):
    """
    TODO: Move this viewset to  ComponentUsageViewSet.
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

    @action(detail=False, methods=["post"])
    @csrf_exempt
    def check_signature(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_200_OK)

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

        # Check if user has service provider permission
        if has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, obj.offering.customer
        ):
            return

        raise rf_exceptions.PermissionDenied()

    # User checklist permissions (for offering users filling in checklists)
    checklist_permissions = [_offering_user_or_service_provider_permission]
    completion_status_permissions = [_offering_user_or_service_provider_permission]
    submit_answers_permissions = [_offering_user_or_service_provider_permission]

    # Reviewer checklist permissions (for service providers reviewing compliance)
    checklist_review_permissions = [
        permission_factory(PermissionEnum.UPDATE_OFFERING_USER, ["offering.customer"])
    ]
    completion_review_status_permissions = [
        permission_factory(PermissionEnum.UPDATE_OFFERING_USER, ["offering.customer"])
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
            return super().get_queryset()

        return get_allowed_offering_users_for_user(
            self.request.user, include_consent_filtering=True, action=self.action
        )

    @extend_schema(
        request=serializers.OfferingUserUpdateRestrictionSerializer,
        responses=None,
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
            ["offering.customer"],
        )
    ]

    @extend_schema(
        request=None,
        responses=None,
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
        request=serializers.OfferingUserStateTransitionSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_pending_additional_validation(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserStateTransitionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment", "")
        comment_url = serializer.validated_data.get("comment_url", "")
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
            ],
            "PENDING_ADDITIONAL_VALIDATION",
        )
    ]

    @extend_schema(
        request=serializers.OfferingUserStateTransitionSerializer,
        responses=None,
    )
    @action(detail=True, methods=["post"])
    def set_pending_account_linking(self, request, uuid=None):
        offering_user: models.OfferingUser = self.get_object()
        serializer = serializers.OfferingUserStateTransitionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment", "")
        comment_url = serializer.validated_data.get("comment_url", "")
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
            ],
            "PENDING_ACCOUNT_LINKING",
        )
    ]

    @extend_schema(request=None, responses=None)
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
        request=None,
        responses=None,
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

    @extend_schema(request=None, responses=None)
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

    @extend_schema(request=None, responses=None)
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

    @extend_schema(request=None, responses=None)
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
            ["offering.customer"],
        )
    ]

    @extend_schema(request=None, responses=None)
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
            ["offering.customer"],
        )
    ]

    @extend_schema(request=None, responses=None)
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
            ["offering.customer"],
        )
    ]

    @extend_schema(
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

        # Optimize queryset with proper joins and prefetching
        queryset = (
            checklist_models.ChecklistCompletion.objects.filter(
                scope_content_type=content_type,
                # Use subquery instead of values_list to avoid evaluation
                scope_object_id__in=Subquery(allowed_offering_users.values("id")),
            )
            .select_related("checklist")
            .prefetch_related("answers", "answers__question")
        )

        return queryset

    def list(self, request, *args, **kwargs):
        """Override list to add OfferingUser data optimization."""
        queryset = self.filter_queryset(self.get_queryset())

        # Optimize by prefetching OfferingUser data in bulk
        self._attach_offering_user_data(queryset)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def _attach_offering_user_data(self, queryset):
        """Attach OfferingUser data to completion instances to avoid N+1 queries."""
        # Get all scope_object_ids from the queryset
        completion_list = list(queryset)
        scope_object_ids = [c.scope_object_id for c in completion_list]

        if not scope_object_ids:
            return

        # Fetch all OfferingUsers with their offerings in one optimized query
        offering_users_map = {
            ou.id: ou
            for ou in models.OfferingUser.objects.filter(
                id__in=scope_object_ids
            ).select_related("offering", "user")
        }

        # Attach the OfferingUser data to each completion instance
        for completion in completion_list:
            completion._offering_user_cache = offering_users_map.get(
                completion.scope_object_id
            )


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
        offering_groups = models.OfferingUserGroup.objects.filter(offering=offering)

        existing_ids = offering_groups.filter(
            backend_metadata__has_key="gid"
        ).values_list("backend_metadata__gid", flat=True)

        if len(existing_ids) == 0:
            max_group_id = int(
                offering.plugin_options.get("initial_usergroup_number", 6000)
            )
        else:
            max_group_id = max(existing_ids)

        offering_group.backend_metadata["gid"] = max_group_id + 1
        offering_group.save(update_fields=["backend_metadata"])


class StatsViewSet(rf_viewsets.GenericViewSet):
    filter_backends = []
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = EmptySerializer

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
        has_resources = models.Resource.objects.filter(
            state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            project__customer_id=OuterRef("pk"),
        )

        users_count = QuotaUsage.objects.filter(
            object_id=OuterRef("pk"),
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
            name="nc_user_count",
        )

        customers = structure_models.Customer.objects.annotate(
            count=core_utils.SubquerySum(users_count, "delta"),
            has_resources=Exists(has_resources),
        ).values("uuid", "name", "abbreviation", "count", "has_resources")

        return Response(customers)

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
                                lambda x: x["offering_uuid"]
                                == resource["offering__uuid"]
                                and x["name"] == name,
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

        for record in result:
            offering = models.Offering.objects.get(uuid=record["offering_uuid"])
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

    def _projects_limits_grouped_by_field(self, field_name):
        results = {}

        for project in structure_models.Project.objects.all():
            field_value = str(getattr(project, field_name))
            if field_value in results:
                results[field_value]["projects_ids"].append(project.id)
            else:
                results[field_value] = {
                    "projects_ids": [project.id],
                }

        for key, result in results.items():
            ids = result.pop("projects_ids")

            for resource in (
                models.Resource.objects.filter(
                    state=ResourceStates.OK, project__id__in=ids
                )
                .exclude(limits={})
                .values("offering__uuid", "limits")
            ):
                limits = resource["limits"]

                for name, value in limits.items():
                    if value > 0:
                        if name in result:
                            result[name] += value
                        else:
                            result[name] = value

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
        invoice_items = (
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start,
                invoice__created__lte=end,
            )
            .exclude(resource__offering__isnull=True)
            .values("resource__offering__uuid")
            .annotate(
                cost=Sum(
                    (Ceil(F("quantity") * F("unit_price") * 100) / 100),
                    output_field=FloatField(),
                )
            )
        )

        serializer = serializers.OfferingCostSerializer(invoice_items, many=True)

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
        result = (
            self.get_active_resources()
            .values("offering__uuid", "offering__name", "offering__country")
            .annotate(count=Count("id"))
            .order_by()
        )

        return Response(
            serializers.OfferingStatsSerializer(result, many=True).data,
            status=status.HTTP_200_OK,
        )

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
        organization_groups = structure_models.OrganizationGroup.objects.annotate(
            customer_count=Count("customers")
        ).filter(customer_count__gt=0)

        results = []

        for group in organization_groups:
            active_resources = self.get_active_resources().filter(
                offering__customer__in=group.customers.all()
            )

            resource_count = active_resources.count()

            if resource_count > 0:
                results.append(
                    {
                        "organization_group_uuid": group.uuid.hex,
                        "organization_group_name": group.name,
                        "count": resource_count,
                    }
                )

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


class BaseServiceAccountViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"

    def perform_create(self, serializer):
        username = self.request.user.username
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
                        models.ProjectServiceAccount.objects.filter(
                            project=project
                        ).count()
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
                        models.CustomerServiceAccount.objects.filter(
                            customer=customer
                        ).count()
                    )
                    if customer_service_accounts_count >= customer.max_service_accounts:
                        raise ValidationError(
                            {
                                "detail": "Maximum number of service accounts reached for this customer"
                            }
                        )

            response_data = utils.create_service_account(data, username, scope_type)
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
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
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
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
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
                {
                    "detail": exc.response.json()
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                    else str(exc)
                }
            )


class ProjectServiceAccountViewSet(BaseServiceAccountViewSet):
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
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
            raise ValidationError({"detail": error_details})

    rotate_api_key_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["project", "project.customer"],
        )
    ]

    rotate_api_key_validators = [core_validators.StateValidator(ServiceAccountState.OK)]


class CustomerServiceAccountViewSet(BaseServiceAccountViewSet):
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
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
            raise ValidationError({"detail": error_details})

    rotate_api_key_permissions = [
        permission_factory(
            PermissionEnum.MANAGE_SERVICE_ACCOUNT,
            ["customer"],
        )
    ]

    rotate_api_key_validators = [core_validators.StateValidator(ServiceAccountState.OK)]


class RobotAccountViewSet(core_views.ActionsViewSet):
    queryset = models.RobotAccount.objects.all()
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
        request=None,
        responses=serializers.RobotAccountDetailsSerializer,
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
        request=None,
        responses=serializers.RobotAccountDetailsSerializer,
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
        request=None,
        responses=serializers.RobotAccountDetailsSerializer,
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
        request=serializers.RobotAccountErrorSerializer,
        responses=serializers.RobotAccountDetailsSerializer,
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


class SectionViewSet(rf_viewsets.ModelViewSet):
    queryset = models.Section.objects.all().order_by("title")
    lookup_field = "key"
    serializer_class = serializers.SectionSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


class CategoryHelpArticleViewSet(rf_viewsets.ModelViewSet):
    queryset = models.CategoryHelpArticle.objects.all().order_by("title")
    serializer_class = serializers.CategoryHelpArticlesSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


class CategoryComponentViewSet(rf_viewsets.ModelViewSet):
    queryset = models.CategoryComponent.objects.all().order_by("name")
    serializer_class = serializers.CategoryComponentsSerializer
    filter_backends = (DjangoFilterBackend,)
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]


class GlobalCategoriesViewSet(views.APIView):
    @extend_schema(
        description="Count of resource categories for all resources accessible by user.",
        parameters=[
            OpenApiParameter(
                name="project_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the project to filter resources by.",
            ),
            OpenApiParameter(
                name="customer_uuid",
                type=uuid.UUID,
                location=OpenApiParameter.QUERY,
                description="UUID of the customer to filter resources by.",
            ),
        ],
        responses=dict[str, int],
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


class IntegrationStatusViewSet(core_views.ReadOnlyActionsViewSet):
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


class ComponentUserUsageLimitViewSet(core_views.ActionsViewSet):
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


class BackendResourceViewSet(core_views.ActionsViewSet):
    """
    The viewset provides endpoints for management over backend resources.
    A site agent is expected to call these endpoints for creation, update and deletion of backend resources.
    The `import_resource` endpoint is responsible for importing of a backend resorce into Waldur and
    only staff can perform this operation.
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
        request=serializers.BackendResourceImportSerializer,
        responses={status.HTTP_200_OK: serializers.ResourceSerializer},
    )
    @action(detail=True, methods=["post"])
    def import_resource(self, request, uuid=None):
        import_resource_serializer = self.get_serializer(data=request.data)
        import_resource_serializer.is_valid(raise_exception=True)

        backend_resource = self.get_object()

        plan = import_resource_serializer.validated_data.get("plan", None)
        project = backend_resource.project
        offering = backend_resource.offering

        backend_id = backend_resource.backend_id
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


class BackendResourceRequestViewSet(core_views.ActionsViewSet):
    """
    The viewset provides endpoints for requesting list of ready-to-import backend resources.
    After creation of the request, a site agent is expected to process it.
    The agent should:
    - create BackendResources missing in Waldur using the BackendResourceViewSet
    - manage the state of the request using `start_processing`, `set_done`, `set_erred` endpoints in this class
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
        request=None,
        responses={status.HTTP_200_OK: dict},
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
        request=None,
        responses={status.HTTP_200_OK: dict},
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
        request=serializers.BackendResourceRequestSetErredSerializer,
        responses={status.HTTP_200_OK: dict},
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


class MaintenanceAnnouncementViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncement.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementFilter
    serializer_class = serializers.MaintenanceAnnouncementSerializer

    schedule_validators = [
        core_validators.StateValidator(
            MaintenanceState.DRAFT, state_enum=MaintenanceState
        )
    ]

    @extend_schema(
        request=EmptySerializer,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def schedule(self, request, uuid=None):
        """Schedule/publish the maintenance announcement"""
        maintenance = self.get_object()

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
        request=EmptySerializer,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def unschedule(self, request, uuid=None):
        """Unschedule/unpublish the maintenance announcement"""
        maintenance = self.get_object()

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
        request=EmptySerializer,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def start_maintenance(self, request, uuid=None):
        """Start the maintenance announcement"""
        maintenance = self.get_object()

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
        request=EmptySerializer,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def complete_maintenance(self, request, uuid=None):
        """Complete the maintenance announcement"""
        maintenance = self.get_object()

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
        request=EmptySerializer,
        responses={200: serializers.MaintenanceActionResponseSerializer},
    )
    @action(detail=True, methods=["POST"])
    def cancel_maintenance(self, request, uuid=None):
        """Cancel the maintenance announcement"""
        maintenance = self.get_object()

        maintenance.cancel_maintenance()
        maintenance.save()
        return Response(
            {"detail": "Maintenance announcement has been cancelled"},
            status=status.HTTP_200_OK,
        )


class MaintenanceAnnouncementOfferingViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementOffering.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.MaintenanceAnnouncementOfferingSerializer


class MaintenanceAnnouncementTemplateViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementTemplate.objects.all().order_by("-created")
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementTemplateFilter
    serializer_class = serializers.MaintenanceAnnouncementTemplateSerializer


class MaintenanceAnnouncementOfferingTemplateViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.MaintenanceAnnouncementOfferingTemplate.objects.all().order_by(
        "-created"
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.MaintenanceAnnouncementOfferingTemplateFilter
    serializer_class = serializers.MaintenanceAnnouncementOfferingTemplateSerializer


class ProviderOfferingToSManagementViewset(core_views.ActionsViewSet):
    """
    ViewSet for managing Terms of Service configurations for offerings.

    Service providers can create, update, and delete ToS configurations
    for their offerings. Users can view ToS configurations.
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

        customers = get_connected_customers(user)
        if customers:
            return self.queryset.filter(offering__customer__in=customers)
        return self.queryset.filter(is_active=True, offering__shared=True)

    create_permissions = update_permissions = partial_update_permissions = (
        destroy_permissions
    ) = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["offering.customer"],
        )
    ]


class UserOfferingConsentViewSet(core_views.ActionsViewSet):
    """
    ViewSet for managing user consent to Terms of Service for offerings.

    Provides endpoints for:
    - Granting consent to an offering
    - Revoking consent
    - Listing consents for a user/offering with status information

    Use standard filtering to find consents:
    - ?user_uuid=<uuid> - Filter by user
    - ?offering_uuid=<uuid> - Filter by offering
    - ?is_active=true - Filter by active status
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
        request=None,
        responses={status.HTTP_200_OK: serializers.UserOfferingConsentSerializer},
        description="Revoke consent to Terms of Service for an offering.",
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


class PublicMaintenanceAnnouncementViewSet(
    PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet
):
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


class CourseAccountViewSet(core_views.ActionsViewSet):
    queryset = models.CourseAccount.objects.all()
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
            user = core_models.User.objects.create(
                username=response_data["tempAccount"]["username"],
                email=response_data["tempAccount"]["email"],
                description="Course Account",
            )
            instance = serializer.save()
            instance.user = user
            instance.save(update_fields=["user"])
        except httpx.HTTPError as exc:
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
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
            error_details = (
                exc.response.json()
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
                else str(exc)
            )
            raise ValidationError({"detail": error_details})

    destroy_validators = [
        core_validators.StateValidator(
            ServiceAccountState.OK, ServiceAccountState.ERRED
        )
    ]

    @action(detail=False, methods=["post"])
    def create_bulk(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Prepare course accounts data with project included
        course_accounts_data = []
        for account_data in serializer.validated_data["course_accounts"]:
            account_data["project"] = serializer.validated_data["project"]
            course_accounts_data.append(account_data)

        course_accounts = utils.create_multiple_course_accounts(
            course_accounts_data, self.request.user.username
        )

        # Handle both mock (returns dicts) and real API (returns model instances)
        if course_accounts and isinstance(course_accounts[0], dict):
            # Mock backend returned dicts, return them directly
            return Response(course_accounts)
        else:
            # Real API returned model instances, serialize them
            course_accounts_serializer = serializers.CourseAccountSerializer(
                course_accounts, many=True, context={"request": request}
            )
            return Response(course_accounts_serializer.data)

    create_bulk_permissions = [check_create_permissions]
    create_bulk_serializer_class = serializers.CourseAccountsBulkCreateSerializer
