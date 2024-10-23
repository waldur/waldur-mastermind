import copy
import datetime
import logging
import textwrap

import reversion
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.db.models import (
    Count,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    PositiveSmallIntegerField,
    Q,
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
from rest_framework import exceptions as rf_exceptions
from rest_framework import mixins, status, views
from rest_framework import permissions as rf_permissions
from rest_framework import viewsets as rf_viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.response import Response

from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.core.renderers import PlainTextRenderer
from waldur_core.core.utils import (
    SubqueryCount,
    is_uuid_like,
    month_start,
    order_with_nulls,
)
from waldur_core.logging.loggers import event_logger
from waldur_core.permissions.enums import PermissionEnum
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
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.executors import ServiceSettingsPullExecutor
from waldur_core.structure.managers import (
    filter_queryset_for_user,
    get_connected_customers,
    get_connected_projects,
    get_organization_groups,
    get_project_users,
    get_visible_users,
)
from waldur_core.structure.registry import SupportedServices, get_resource_type
from waldur_core.structure.serializers import (
    get_resource_serializer_class,
)
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import serializers as invoice_serializers
from waldur_mastermind.marketplace import PLUGIN_NAME as BASIC_PLUGIN_NAME
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace.managers import filter_offering_permissions
from waldur_mastermind.marketplace.utils import (
    validate_attributes,
)
from waldur_mastermind.marketplace_slurm_remote import (
    PLUGIN_NAME as SLURM_REMOTE_PLUGIN_NAME,
)
from waldur_mastermind.marketplace_support import PLUGIN_NAME as SUPPORT_PLUGIN_NAME
from waldur_mastermind.promotions import models as promotions_models
from waldur_mastermind.support import models as support_models
from waldur_pid import models as pid_models

from . import filters, log, models, permissions, plugins, serializers, tasks, utils

logger = logging.getLogger(__name__)

User = get_user_model()


class BaseMarketplaceView(core_views.ActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    update_permissions = partial_update_permissions = destroy_permissions = [
        structure_permissions.is_owner
    ]


class PublicViewsetMixin:
    def get_permissions(self):
        if settings.WALDUR_MARKETPLACE[
            "ANONYMOUS_USER_CAN_VIEW_OFFERINGS"
        ] and self.action in ["list", "retrieve"]:
            return [rf_permissions.AllowAny()]
        else:
            return super().get_permissions()


class ConnectedOfferingDetailsMixin:
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


class ServiceProviderViewSet(PublicViewsetMixin, BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all().order_by("customer__name")
    serializer_class = serializers.ServiceProviderSerializer
    filterset_class = filters.ServiceProviderFilter
    api_secret_code_permissions = projects_permissions = (
        project_permissions_permissions
    ) = keys_permissions = users_permissions = set_offerings_username_permissions = [
        structure_permissions.is_owner
    ]

    @action(detail=True, methods=["GET", "POST"])
    def api_secret_code(self, request, uuid=None):
        """On GET request - return service provider api_secret_code.
        On POST - generate new service provider api_secret_code.
        """
        service_provider = self.get_object()
        if request.method == "GET":
            return Response(
                {"api_secret_code": service_provider.api_secret_code},
                status=status.HTTP_200_OK,
            )
        else:
            service_provider.generate_api_secret_code()
            service_provider.save()
            return Response(
                {
                    "detail": _("Api secret code updated."),
                    "api_secret_code": service_provider.api_secret_code,
                },
                status=status.HTTP_200_OK,
            )

    def get_customer_project_ids(self):
        service_provider = self.get_object()
        return utils.get_service_provider_project_ids(service_provider)

    def get_customer_user_ids(self):
        service_provider = self.get_object()
        return utils.get_service_provider_user_ids(self.request.user, service_provider)

    @action(detail=True, methods=["GET"])
    def customers(self, request, uuid=None):
        service_provider = self.get_object()
        customer_ids = utils.get_service_provider_customer_ids(service_provider)
        customers = structure_models.Customer.objects.filter(id__in=customer_ids)
        page = self.paginate_queryset(customers)
        serializer = serializers.ProviderCustomerSerializer(
            page,
            many=True,
            context={
                "service_provider": service_provider,
                **self.get_serializer_context(),
            },
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def customer_projects(self, request, uuid=None):
        service_provider = self.get_object()
        customer_uuid = request.query_params.get("project_customer_uuid")
        if not customer_uuid or not is_uuid_like(customer_uuid):
            return self.get_paginated_response([])
        project_ids = (
            utils.get_service_provider_resources(service_provider)
            .filter(project__customer__uuid=customer_uuid)
            .values_list("project_id", flat=True)
        )
        projects = structure_models.Project.available_objects.filter(id__in=project_ids)
        page = self.paginate_queryset(projects)
        context = self.get_serializer_context()
        context["service_provider"] = service_provider
        serializer = serializers.ProviderCustomerProjectSerializer(
            page, many=True, context=context
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def projects(self, request, uuid=None):
        project_ids = self.get_customer_project_ids()
        projects = structure_models.Project.available_objects.filter(id__in=project_ids)
        page = self.paginate_queryset(projects)
        serializer = structure_serializers.ProjectSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def project_permissions(self, request, uuid=None):
        project_ids = self.get_customer_project_ids()
        content_type = ContentType.objects.get_for_model(structure_models.Project)
        permissions = UserRole.objects.filter(
            content_type=content_type,
            object_id__in=project_ids,
            is_active=True,
            user__is_active=True,
        )
        page = self.paginate_queryset(permissions)
        serializer = structure_serializers.ProjectPermissionLogSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def keys(self, request, uuid=None):
        user_ids = self.get_customer_user_ids()
        keys = core_models.SshPublicKey.objects.filter(user_id__in=user_ids)
        page = self.paginate_queryset(keys)
        serializer = structure_serializers.SshKeySerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def users(self, request, uuid=None):
        service_provider = self.get_object()
        user_ids = self.get_customer_user_ids()
        users = core_models.User.objects.filter(id__in=user_ids)
        filtered_users = structure_filters.UserFilter(request.GET, queryset=users)
        page = self.paginate_queryset(filtered_users.qs)
        context = self.get_serializer_context()
        context["service_provider"] = service_provider
        serializer = serializers.DetailedProviderUserSerializer(
            page, many=True, context=context
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def user_customers(self, request, uuid=None):
        service_provider = self.get_object()
        user_uuid = request.query_params.get("user_uuid")
        if not user_uuid or not is_uuid_like(user_uuid):
            self.paginate_queryset(structure_models.Customer.objects.none())
            return self.get_paginated_response([])

        try:
            user = User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            self.paginate_queryset(structure_models.Customer.objects.none())
            return self.get_paginated_response([])

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

        customers = structure_models.Customer.objects.filter(
            id__in=project_customers.union(valid_customers)
        )
        page = self.paginate_queryset(customers)
        context = self.get_serializer_context()
        context["service_provider"] = service_provider
        serializer = serializers.ProviderCustomerSerializer(
            page, many=True, context=context
        )
        return self.get_paginated_response(serializer.data)

    def check_related_resources(request, view, obj=None):
        if obj and obj.has_active_offerings:
            raise rf_exceptions.ValidationError(
                _("Service provider has active offerings. Please archive them first.")
            )

    destroy_permissions = [structure_permissions.is_owner, check_related_resources]

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
            models.Resource.objects.exclude(state=models.Resource.States.TERMINATED)
            .filter(
                project_id__in=user_projects_ids,
                offering__customer=self.get_object().customer,
            )
            .values_list("offering_id", flat=True)
        )

        for offering_id in offering_ids:
            models.OfferingUser.objects.update_or_create(
                user=user, offering_id=offering_id, defaults={"username": username}
            )

        return Response(
            {
                "detail": _("Offering users have been set."),
            },
            status=status.HTTP_201_CREATED,
        )

    set_offerings_username_serializer_class = serializers.SetOfferingsUsernameSerializer

    @action(detail=True, methods=["GET"])
    def offerings(self, request, uuid=None):
        service_provider = self.get_object()

        offerings = models.Offering.objects.filter(
            customer=service_provider.customer,
            billable=True,
            shared=True,
        )

        filtered_offerings = filters.OfferingFilter(request.GET, queryset=offerings)
        page = self.paginate_queryset(filtered_offerings.qs)
        serializer = serializers.ProviderOfferingSerializer(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["GET"])
    def stat(self, request, uuid=None):
        to_day = timezone.datetime.today().date()
        service_provider = self.get_object()

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
            .exclude(state=models.Resource.States.TERMINATED)
            .order_by()
            .values_list("project__customer", flat=True)
            .distinct()
            .count()
        )

        active_resources = models.Resource.objects.filter(
            offering__customer=service_provider.customer,
        ).exclude(state=models.Resource.States.TERMINATED)

        active_and_paused_offerings = models.Offering.objects.filter(
            customer=service_provider.customer,
            billable=True,
            shared=True,
            state__in=(models.Offering.States.ACTIVE, models.Offering.States.PAUSED),
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
            state=models.Order.States.PENDING_PROVIDER,
        ).count()

        erred_resources = models.Resource.objects.filter(
            offering__customer=service_provider.customer,
            state=models.Resource.States.ERRED,
        ).count()

        return Response(
            {
                "active_campaigns": active_campaigns,
                "current_customers": current_customers,
                "customers_number_change": utils.count_customers_number_change(
                    service_provider
                ),
                "active_resources": active_resources.count(),
                "resources_number_change": utils.count_resources_number_change(
                    service_provider
                ),
                "active_and_paused_offerings": active_and_paused_offerings,
                "unresolved_tickets": unresolved_tickets,
                "pending_orders": pending_orders,
                "erred_resources": erred_resources,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["GET"])
    def revenue(self, request, uuid=None):
        start = month_start(timezone.datetime.today()) - relativedelta(years=1)
        service_provider = self.get_object()
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

    @action(detail=True, methods=["GET"])
    def robot_account_customers(self, request, uuid=None):
        service_provider = self.get_object()
        qs = models.RobotAccount.objects.filter(
            resource__offering__customer=service_provider.customer
        )
        customer_name = request.query_params.get("customer_name")
        if customer_name:
            qs = qs.filter(resource__project__customer__name__icontains=customer_name)
        customer_ids = qs.values_list("resource__project__customer_id").distinct()
        customers = structure_models.Customer.objects.filter(
            id__in=customer_ids
        ).order_by("name")
        page = self.paginate_queryset(customers)
        data = [{"name": row.name, "uuid": row.uuid} for row in page]
        return self.get_paginated_response(data)

    @action(detail=True, methods=["GET"])
    def robot_account_projects(self, request, uuid=None):
        service_provider = self.get_object()
        qs = models.RobotAccount.objects.filter(
            resource__offering__customer=service_provider.customer
        )
        project_name = request.query_params.get("project_name")
        if project_name:
            qs = qs.filter(resource__offering__project__name__icontains=project_name)
        project_ids = qs.values_list("resource__project_id").distinct()
        projects = structure_models.Project.objects.filter(id__in=project_ids).order_by(
            "name"
        )
        page = self.paginate_queryset(projects)
        data = [{"name": row.name, "uuid": row.uuid} for row in page]
        return self.get_paginated_response(data)


class CategoryViewSet(PublicViewsetMixin, EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.CategorySerializer
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


def can_update_offering(request, view, obj=None):
    offering = obj

    if not offering:
        return

    if offering.state == models.Offering.States.DRAFT:
        if has_permission(
            request, PermissionEnum.UPDATE_OFFERING, offering
        ) or has_permission(request, PermissionEnum.UPDATE_OFFERING, offering.customer):
            return
        else:
            raise rf_exceptions.PermissionDenied()
    else:
        structure_permissions.is_staff(request, view)


def validate_offering_update(offering):
    if offering.state == models.Offering.States.ARCHIVED:
        raise rf_exceptions.ValidationError(
            _("It is not possible to update archived offering.")
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
                        models.Resource.States.OK,
                        models.Resource.States.UPDATING,
                        models.Resource.States.TERMINATING,
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
        offering = self.get_object()
        serializer = serializers.ProviderOfferingSerializer(
            offering, many=False, context=self.get_serializer_context()
        )
        if self.request.user.is_staff is not True:
            if serializer.data["resources_count"] != 0:
                return Response(
                    {"detail": _("Offering was not deleted since it has resources.")},
                    status=status.HTTP_403_FORBIDDEN,
                )
            elif offering.state != models.Offering.States.DRAFT:
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

    @action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        return self._update_state("activate")

    @action(detail=True, methods=["post"])
    def draft(self, request, uuid=None):
        return self._update_state("draft")

    @action(detail=True, methods=["post"])
    def pause(self, request, uuid=None):
        return self._update_state("pause", request)

    pause_serializer_class = serializers.OfferingPauseSerializer

    @action(detail=True, methods=["post"])
    def unpause(self, request, uuid=None):
        return self._update_state("unpause", request)

    @action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        return self._update_state("archive")

    def _update_state(self, action, request=None):
        offering = self.get_object()

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
                offering = serializer.save()

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
            ["*", "customer"],
        )
    ]

    unpause_permissions = [
        permission_factory(
            PermissionEnum.UNPAUSE_OFFERING,
            ["*", "customer"],
        )
    ]

    archive_permissions = [
        permission_factory(
            PermissionEnum.ARCHIVE_OFFERING,
            ["*", "customer"],
        )
    ]

    activate_permissions = [structure_permissions.is_staff]

    activate_validators = pause_validators = archive_validators = destroy_validators = [
        structure_utils.check_customer_blocked_or_archived
    ]

    update_permissions = [can_update_offering]

    update_validators = [
        validate_offering_update,
        structure_utils.check_customer_blocked_or_archived,
    ]

    def perform_create(self, serializer):
        customer = serializer.validated_data["customer"]
        structure_utils.check_customer_blocked_or_archived(customer)

        super().perform_create(serializer)

    @action(detail=True, methods=["get"])
    def importable_resources(self, request, uuid=None):
        offering = self.get_object()
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

    @action(detail=True, methods=["post"])
    def import_resource(self, request, uuid=None):
        import_resource_serializer = self.get_serializer(data=request.data)
        import_resource_serializer.is_valid(raise_exception=True)

        plan = import_resource_serializer.validated_data.get("plan", None)
        project = import_resource_serializer.validated_data["project"]
        backend_id = import_resource_serializer.validated_data["backend_id"]

        offering = self.get_object()
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
        if resource_model.objects.filter(
            **{field: offering.scope}, backend_id=backend_id
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
                    backend_id=backend_id, project=project
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

    @action(detail=True, methods=["post"])
    def update_attributes(self, request, uuid=None):
        offering = self.get_object()
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
            ["*", "customer"],
        )
    ]
    update_attributes_validators = update_validators

    def _update_action(self, request):
        offering = self.get_object()
        serializer = self.get_serializer(offering, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def update_location(self, request, uuid=None):
        return self._update_action(request)

    update_location_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_LOCATION,
            ["*", "customer"],
        )
    ]
    update_location_validators = update_validators
    update_location_serializer_class = serializers.OfferingLocationUpdateSerializer

    @action(detail=True, methods=["post"])
    def update_description(self, request, uuid=None):
        return self._update_action(request)

    update_description_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_DESCRIPTION,
            ["*", "customer"],
        )
    ]
    update_description_validators = update_validators
    update_description_serializer_class = (
        serializers.OfferingDescriptionUpdateSerializer
    )

    @action(detail=True, methods=["post"])
    def update_overview(self, request, uuid=None):
        return self._update_action(request)

    update_overview_permissions = [can_update_offering]
    update_overview_validators = update_validators
    update_overview_serializer_class = serializers.OfferingOverviewUpdateSerializer

    @action(detail=True, methods=["post"])
    def update_options(self, request, uuid=None):
        return self._update_action(request)

    update_options_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer"],
        )
    ]
    update_options_validators = update_validators
    update_options_serializer_class = serializers.OfferingOptionsUpdateSerializer

    @action(detail=True, methods=["post"])
    def update_resource_options(self, request, uuid=None):
        return self._update_action(request)

    update_resource_options_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_OPTIONS,
            ["*", "customer"],
        )
    ]
    update_resource_options_validators = update_validators
    update_resource_options_serializer_class = (
        serializers.OfferingResourceOptionsUpdateSerializer
    )

    @action(detail=True, methods=["post"])
    def update_integration(self, request, uuid=None):
        return self._update_action(request)

    update_integration_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION,
            ["*", "customer"],
        )
    ]
    update_integration_validators = update_validators
    update_integration_serializer_class = (
        serializers.OfferingIntegrationUpdateSerializer
    )

    @action(detail=True, methods=["post"])
    def update_thumbnail(self, request, uuid=None):
        offering = self.get_object()
        serializer = serializers.OfferingThumbnailSerializer(
            instance=offering, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_thumbnail_permissions = [permissions.user_can_update_thumbnail]

    @action(detail=True, methods=["post"])
    def delete_thumbnail(self, request, uuid=None):
        offering = self.get_object()
        offering.thumbnail.delete()
        offering.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_thumbnail_permissions = update_thumbnail_permissions

    @action(detail=True)
    def customers(self, request, uuid):
        offering = self.get_object()
        active_customers = utils.get_active_customers(request, self)
        customer_queryset = utils.get_offering_customers(offering, active_customers)
        serializer_class = structure_serializers.CustomerSerializer
        serializer = serializer_class(
            instance=customer_queryset, many=True, context=self.get_serializer_context()
        )
        page = self.paginate_queryset(serializer.data)
        return self.get_paginated_response(page)

    customers_permissions = [structure_permissions.is_owner]

    def get_stats(self, get_queryset, serializer, serializer_context=None):
        offering = self.get_object()
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

    @action(detail=True)
    def costs(self, *args, **kwargs):
        return self.get_stats(utils.get_offering_costs, serializers.CostsSerializer)

    costs_permissions = [structure_permissions.is_owner]

    @action(detail=True)
    def component_stats(self, *args, **kwargs):
        offering = self.get_object()
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
        offering = self.get_object()
        resources_count = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
            .count()
        )
        customers_count = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
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

    @action(detail=True, methods=["post"])
    def update_organization_groups(self, request, uuid):
        offering = self.get_object()
        serializer = serializers.OrganizationGroupsSerializer(
            instance=offering, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_organization_groups_permissions = [structure_permissions.is_owner]
    update_organization_groups_validators = update_validators

    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        offering = self.get_object()
        offering.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions
    delete_organization_groups_validators = update_validators

    @action(detail=True, methods=["post"])
    def add_endpoint(self, request, uuid=None):
        offering = self.get_object()
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
            ["*", "customer"],
        )
    ]
    add_endpoint_serializer_class = serializers.NestedEndpointSerializer
    add_endpoint_validators = update_validators

    @action(detail=True, methods=["post"])
    def delete_endpoint(self, request, uuid=None):
        offering = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering.endpoints.filter(uuid=serializer.validated_data["uuid"]).delete()
        return Response(
            status=status.HTTP_204_NO_CONTENT,
        )

    delete_endpoint_serializer_class = serializers.EndpointDeleteSerializer
    delete_endpoint_permissions = [
        permission_factory(
            PermissionEnum.DELETE_OFFERING_ENDPOINT,
            ["*", "customer"],
        )
    ]
    delete_endpoint_validators = update_validators

    @action(detail=False, permission_classes=[], filter_backends=[DjangoFilterBackend])
    def groups(self, *args, **kwargs):
        OFFERING_LIMIT = 4
        qs = self.filter_queryset(
            self.get_queryset().filter(shared=True, state=models.Offering.States.ACTIVE)
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

    @action(detail=True, methods=["GET"], renderer_classes=[PlainTextRenderer])
    def glauth_users_config(self, request, uuid=None):
        """
        This endpoint provides a config file for GLauth
        Example: https://github.com/glauth/glauth/blob/master/v2/sample-simple.cfg
        It is assumed that the config is used by an external agent,
        which synchronizes data from Waldur to GLauth
        """
        offering = self.get_object()

        if not offering.secret_options.get(
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

    @action(detail=True, methods=["GET"])
    def user_has_resource_access(self, request, uuid=None):
        offering = self.get_object()
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

    @action(detail=True, methods=["post"])
    def update_offering_component(self, request, uuid=None):
        offering = self.get_object()

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
            ["*", "customer"],
        )
    ]
    update_offering_component_validators = update_validators

    @action(detail=True, methods=["post"])
    def remove_offering_component(self, request, uuid=None):
        offering = self.get_object()
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

    remove_offering_component_serializer_class = serializers.OfferingComponentSerializer
    remove_offering_component_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            ["*", "customer"],
        )
    ]
    remove_offering_component_validators = update_validators

    @action(detail=True, methods=["post"])
    def create_offering_component(self, request, uuid=None):
        offering = self.get_object()
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
            ["*", "customer"],
        )
    ]
    create_offering_component_validators = update_validators

    @action(detail=True, methods=["post"])
    def sync(self, request, uuid=None):
        offering = self.get_object()
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
            structure_models.ServiceSettings.States.OK,
            structure_models.ServiceSettings.States.ERRED,
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
            ["*", "customer"],
        )
    ]

    @action(detail=True, methods=["POST"])
    def set_backend_metadata(self, request, uuid=None):
        offering = self.get_object()
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
            ["*", "customer"],
        )
    ]

    @action(detail=True, methods=["GET"])
    def list_customer_projects(self, request, uuid=None):
        offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("project_id", flat=True)
        )
        projects = structure_models.Project.objects.filter(id__in=project_ids)
        serializer = structure_serializers.ProjectSerializer(
            instance=projects, many=True, context={"request": request}
        )
        return Response(
            status=status.HTTP_200_OK,
            data=serializer.data,
        )

    @action(detail=True, methods=["GET"])
    def list_customer_users(self, request, uuid=None):
        offering = self.get_object()
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("project_id", flat=True)
        )
        ctype = ContentType.objects.get_for_model(structure_models.Project)
        user_ids = get_user_ids(ctype, project_ids)
        users = core_models.User.objects.filter(id__in=user_ids)
        serializer = structure_serializers.UserSerializer(
            instance=users, many=True, context={"request": request}
        )
        return Response(
            status=status.HTTP_200_OK,
            data=serializer.data,
        )

    list_customer_projects_permissions = list_customer_users_permissions = [
        structure_permissions.is_owner
    ]


class PublicOfferingViewSet(rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.Offering.objects.filter()
    lookup_field = "uuid"
    serializer_class = serializers.PublicOfferingDetailsSerializer
    filterset_class = filters.OfferingFilter
    permission_classes = []

    def get_queryset(self):
        user = self.request.user
        return self.queryset.filter_by_ordering_availability_for_user(user)

    @action(detail=True, methods=["get"])
    def plans(self, request, uuid=None):
        offering = self.get_object()
        return Response(
            serializers.PublicOfferingDetailsSerializer(
                context=self.get_serializer_context()
            ).get_filtered_plans(offering),
            status=status.HTTP_200_OK,
        )

    def plan_detail(self, request, uuid=None, plan_uuid=None):
        offering = self.get_object()

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
            state=models.Resource.States.TERMINATED
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
    filter_backends = (DjangoFilterBackend, filters.PlanFilterBackend)

    disabled_actions = ["destroy"]
    update_validators = partial_update_validators = [validate_plan_update]

    update_permissions = partial_update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING_PLAN,
            ["offering.customer"],
        )
    ]

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

    @action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        plan = self.get_object()
        with reversion.create_revision():
            plan.archived = True
            plan.save(update_fields=["archived"])
            reversion.set_user(self.request.user)
            reversion.set_comment("Plan has been archived.")
        return Response(
            {"detail": _("Plan has been archived.")}, status=status.HTTP_200_OK
        )

    @action(detail=False)
    def usage_stats(self, request):
        return PlanUsageReporter(self, request).get_report()

    @action(detail=True, methods=["post"])
    def update_organization_groups(self, request, uuid):
        plan = self.get_object()
        serializer = serializers.OrganizationGroupsSerializer(
            instance=plan, context={"request": request}, data=request.data
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)

    update_organization_groups_permissions = [structure_permissions.is_owner]

    @action(detail=True, methods=["post"])
    def delete_organization_groups(self, request, uuid=None):
        plan = self.get_object()
        plan.organization_groups.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)

    delete_organization_groups_permissions = update_organization_groups_permissions


class PlanComponentViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.PlanComponent.objects.filter()
    serializer_class = serializers.PlanComponentSerializer
    filterset_class = filters.PlanComponentFilter
    lookup_field = "uuid"

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

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)

        return self.queryset.filter(
            Q(project__in=connected_projects)
            | Q(project__customer__in=connected_customers)
            | Q(offering__customer__in=connected_customers)
        ).distinct()

    approve_by_consumer_validators = [
        structure_utils.check_customer_blocked_or_archived,
        structure_utils.check_project_end_date,
        core_validators.StateValidator(models.Order.States.PENDING_CONSUMER),
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
        return super().list(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def approve_by_consumer(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.review_by_consumer(request.user)
        if order.project.start_date and order.project.start_date > timezone.now():
            order.state = models.Order.States.PENDING_PROJECT
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
            order.state = models.Order.States.PENDING_PROVIDER
            order.save(update_fields=["state"])
            transaction.on_commit(
                lambda: tasks.notify_provider_about_pending_order.delay(order.uuid)
            )
        return Response(status=status.HTTP_200_OK)

    approve_by_provider_validators = [
        structure_utils.check_customer_blocked_or_archived,
        structure_utils.check_project_end_date,
        core_validators.StateValidator(models.Order.States.PENDING_PROVIDER),
    ]

    approve_by_provider_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]

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
        core_validators.StateValidator(models.Order.States.PENDING_CONSUMER),
    ]

    reject_by_consumer_permissions = [permissions.user_can_reject_order_as_consumer]

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
        core_validators.StateValidator(models.Order.States.PENDING_PROVIDER),
    ]

    reject_by_provider_permissions = [
        permission_factory(
            PermissionEnum.REJECT_ORDER,
            ["offering.customer"],
        )
    ]

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
            models.Order.States.PENDING_CONSUMER,
            models.Order.States.PENDING_PROVIDER,
            models.Order.States.EXECUTING,
        ),
        OfferingTypeValidator(BASIC_PLUGIN_NAME, SUPPORT_PLUGIN_NAME),
    ]

    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.cancel()
        order.save(update_fields=["state"])
        return Response(status=status.HTTP_202_ACCEPTED)

    set_state_executing_validators = [
        core_validators.StateValidator(
            models.Order.States.PENDING_CONSUMER,
            models.Order.States.PENDING_PROVIDER,
            models.Order.States.ERRED,
        ),
        OfferingTypeValidator(SLURM_REMOTE_PLUGIN_NAME),
    ]

    set_state_executing_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]

    @action(detail=True, methods=["post"])
    def set_state_executing(self, request, uuid=None):
        order: models.Order = self.get_object()
        order.set_state_executing()
        order.save(update_fields=["state"])
        return Response(status=status.HTTP_200_OK)

    set_state_done_validators = [
        core_validators.StateValidator(
            models.Order.States.EXECUTING,
        ),
        OfferingTypeValidator(
            SLURM_REMOTE_PLUGIN_NAME, BASIC_PLUGIN_NAME, SUPPORT_PLUGIN_NAME
        ),
    ]

    set_state_done_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]

    @action(detail=True, methods=["post"])
    def set_state_done(self, request, uuid=None):
        order: models.Order = self.get_object()
        callbacks.sync_order_state(order, models.Order.States.DONE)
        return Response(status=status.HTTP_200_OK)

    set_state_erred_validators = [
        OfferingTypeValidator(SLURM_REMOTE_PLUGIN_NAME),
    ]

    set_state_erred_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["offering.customer"],
        )
    ]

    @action(detail=True, methods=["post"])
    def set_state_erred(self, request, uuid=None):
        order: models.Order = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        error_message = serializer.validated_data["error_message"]
        error_traceback = serializer.validated_data["error_traceback"]

        callbacks.sync_order_state(order, models.Order.States.ERRED)
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
            models.Order.States.PENDING_CONSUMER,
            models.Order.States.PENDING_PROVIDER,
        ),
        structure_utils.check_customer_blocked_or_archived,
    ]

    @action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        if not request.user.is_staff:
            raise PermissionDenied()
        obj = self.get_object()
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        return super().list(request, *args, **kwargs)

    @action(detail=True, methods=["get"])
    def details(self, request, uuid=None):
        resource = self.get_object()
        if not resource.scope:
            return Response(status=status.HTTP_404_NOT_FOUND)
        resource_type = get_resource_type(resource.scope)
        serializer_class = get_resource_serializer_class(resource_type)
        if not serializer_class:
            return Response(status.HTTP_204_NO_CONTENT)
        serializer = serializer_class(
            instance=resource.scope, context=self.get_serializer_context()
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def set_as_erred(self, request, uuid=None):
        resource = self.get_object()
        resource.set_state_erred()
        resource.save()

        if resource.scope and hasattr(resource.scope, "set_erred"):
            resource.scope.set_erred()
            resource.scope.save()

        return Response(status=status.HTTP_200_OK)

    set_as_erred_permissions = [structure_permissions.is_staff]

    @action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        """
        Delete marketplace resource and related plugin resource from the database without scheduling operations on backend
        and without checking current state of the resource. It is intended to be used
        for removing resource stuck in transitioning state.
        """
        obj = self.get_object()
        if obj.scope:
            obj.scope.delete()
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    unlink_permissions = [structure_permissions.is_staff]

    def create_resource_order(self, request, resource, **kwargs):
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
            order.save()

        return Response({"order_uuid": order.uuid.hex}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def terminate(self, request, uuid=None):
        resource: models.Resource = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attributes = serializer.validated_data.get("attributes", {})

        return self.create_resource_order(
            request=request,
            resource=resource,
            type=models.Order.Types.TERMINATE,
            attributes=attributes,
        )

    terminate_serializer_class = serializers.ResourceTerminateSerializer

    terminate_permissions = [permissions.user_can_terminate_resource]

    terminate_validators = [
        core_validators.StateValidator(
            models.Resource.States.OK, models.Resource.States.ERRED
        ),
    ]

    @action(detail=True, methods=["get"])
    def plan_periods(self, request, uuid=None):
        resource = self.get_object()
        qs = models.ResourcePlanPeriod.objects.filter(resource=resource)
        qs = qs.filter(Q(end=None) | Q(end__gte=month_start(timezone.now())))
        serializer = serializers.ResourcePlanPeriodSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def move_resource(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = serializer.validated_data["project"]
        try:
            utils.move_resource(resource, project)
        except utils.MoveResourceException as exception:
            error_message = str(exception)
            return JsonResponse({"error_message": error_message}, status=409)

        serialized_resource = serializers.ResourceSerializer(
            resource, context=self.get_serializer_context()
        )

        return Response(serialized_resource.data, status=status.HTTP_200_OK)

    move_resource_serializer_class = serializers.MoveResourceSerializer
    move_resource_permissions = [structure_permissions.is_staff]

    @action(detail=True, methods=["post"])
    def set_slug(self, request, uuid=None):
        resource = self.get_object()
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
        resource = self.get_object()
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
            log.log_marketplace_resource_end_date_has_been_updated_by_provider(
                resource, request.user
            )
        else:
            log.log_marketplace_resource_end_date_has_been_updated_by_staff(
                resource, request.user
            )

        return Response(status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def set_end_date_by_staff(self, request, uuid=None):
        return self._set_end_date(request, True)

    set_end_date_by_staff_permissions = [structure_permissions.is_staff]

    @action(detail=True, methods=["get"], renderer_classes=[PlainTextRenderer])
    def glauth_users_config(self, request, uuid=None):
        resource: models.Resource = self.get_object()
        project = resource.project
        offering = resource.offering

        if not offering.secret_options.get(
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

    @action(detail=True, methods=["get"])
    def offering_for_subresources(self, request, uuid=None):
        resource = self.get_object()

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

    @action(detail=True, methods=["get"])
    def team(self, request, uuid=None):
        resource = self.get_object()
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


class ResourceViewSet(BaseResourceViewSet):
    def get_queryset(self):
        return self.queryset.filter_for_user(self.request.user)

    @action(detail=False, methods=["post"])
    def suggest_name(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project: structure_models.Project = serializer.validated_data["project"]
        offering: models.Offering = serializer.validated_data["offering"]
        resource_count = models.Resource.objects.filter(
            project=project, offering=offering
        ).count()
        parts = [
            project.customer.slug,
            project.slug,
            offering.slug,
        ]
        result = "-".join(parts) + "-" + str(resource_count + 1)
        return Response({"name": result})

    suggest_name_serializer_class = serializers.ResourceSuggestNameSerializer

    @action(detail=True, methods=["post"])
    def switch_plan(self, request, uuid=None):
        resource = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data["plan"]

        return self.create_resource_order(
            request=request,
            resource=resource,
            old_plan=resource.plan,
            plan=plan,
            type=models.Order.Types.UPDATE,
            limits=resource.limits or {},
        )

    switch_plan_serializer_class = serializers.ResourceSwitchPlanSerializer

    @action(detail=True, methods=["post"])
    def update_limits(self, request, uuid=None):
        resource = self.get_object()

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
            type=models.Order.Types.UPDATE,
            limits=limits,
            attributes={"old_limits": resource.limits},
        )

    update_limits_serializer_class = serializers.ResourceUpdateLimitsSerializer

    switch_plan_permissions = [
        permission_factory(
            PermissionEnum.SWITCH_RESOURCE_PLAN,
            ["project", "project.customer"],
        )
    ]

    update_limits_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_LIMITS,
            ["project", "project.customer"],
        )
    ]

    switch_plan_validators = update_limits_validators = [
        core_validators.StateValidator(models.Resource.States.OK),
    ]

    @action(detail=True, methods=["post"])
    def update_options(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data, instance=resource)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response(
            {"status": _("Resource options are submitted")}, status=status.HTTP_200_OK
        )

    update_options_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE_OPTIONS,
            ["project", "project.customer"],
        )
    ]
    update_options_serializer_class = serializers.ResourceOptionsSerializer


class ProviderResourceViewSet(BaseResourceViewSet):
    def get_queryset(self):
        return self.queryset.filter_for_offering_customer(self.request.user)

    @action(detail=True, methods=["post"])
    def set_end_date_by_provider(self, request, uuid=None):
        return self._set_end_date(request, False)

    set_end_date_by_provider_permissions = [
        permissions.user_can_set_end_date_by_provider
    ]

    @action(detail=True, methods=["post"])
    def set_backend_id(self, request, uuid=None):
        resource = self.get_object()
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

    @action(detail=True, methods=["post"])
    def submit_report(self, request, uuid=None):
        resource = self.get_object()
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

    @action(detail=True, methods=["post"])
    def set_backend_metadata(self, request, uuid=None):
        resource = self.get_object()
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


class ResourceOfferingsViewSet(ListAPIView):
    serializer_class = serializers.ResourceOfferingSerializer

    def get_category(self):
        category_uuid = self.kwargs["category_uuid"]
        if not is_uuid_like(category_uuid):
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data="Category UUID is invalid."
            )
        return get_object_or_404(models.Category, uuid=category_uuid)

    def get_queryset(self):
        user = self.request.user
        category = self.get_category()
        offerings = (
            models.Resource.objects.all()
            .filter_for_user(user)
            .filter(offering__category=category)
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list("offering_id", flat=True)
        )
        return models.Offering.objects.filter(pk__in=offerings)


class RuntimeStatesViewSet(views.APIView):
    def get(self, request, project_uuid=None):
        projects = filter_queryset_for_user(
            structure_models.Project.objects.all(), request.user
        )
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

    def get_customer(self):
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
        customer_ids = (
            models.Resource.objects.all()
            .filter_for_offering_customer(self.request.user)
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

    @action(detail=True, methods=["post"])
    def set_user_usage(self, request, *args, **kwargs):
        component_usage = self.get_object()
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
            offering.customer
            and offering.customer.has_user(user, structure_models.CustomerRole.OWNER)
        ):
            return

        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [structure_permissions.is_owner]


class OfferingUsersViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    rf_viewsets.GenericViewSet,
):
    queryset = models.OfferingUser.objects.all()
    serializer_class = serializers.OfferingUserSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.OfferingUserFilter

    def perform_destroy(self, instance):
        request = self.request
        offering = instance.offering

        if not has_permission(
            request, PermissionEnum.DELETE_OFFERING_USER, offering.customer
        ):
            raise PermissionDenied(_("You do not have permission to delete this user."))
        instance.delete()

    def get_queryset(self):
        queryset = super().get_queryset()
        current_user = self.request.user
        if current_user.is_staff or current_user.is_support:
            return queryset

        visible_users = get_visible_users(current_user)
        managed_customers = get_connected_customers(current_user)
        managed_projects = get_connected_projects(current_user)
        nested_customers = structure_models.Project.objects.filter(
            id__in=managed_projects
        ).values_list("customer_id", flat=True)
        visible_customers = managed_customers.union(nested_customers)
        visible_organization_groups = structure_models.Customer.objects.filter(
            id__in=visible_customers
        ).values_list("organization_group_id", flat=True)

        queryset = queryset.filter(
            # Exclude offerings with disabled OfferingUsers feature
            Q(offering__secret_options__service_provider_can_create_offering_user=True)
            &
            # user can see own remote offering user
            (
                Q(user=current_user)
                | (
                    (
                        # service provider can see all records related to managed offerings
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
        return queryset

    @action(detail=True, methods=["post"])
    def update_restricted(self, request, uuid=None):
        offering_user = self.get_object()
        serializer = serializers.OfferingUserUpdateRestrictionSerializer(
            data=request.data, context={"request": request}, instance=offering_user
        )
        serializer.is_valid(raise_exception=True)
        offering_user.is_restricted = serializer.validated_data["is_restricted"]
        offering_user.save(update_fields=["is_restricted"])
        event_logger.marketplace_offering_user.info(
            f"Restriction status for user {offering_user.user.username} in offering {offering_user.offering.name} has been set to {offering_user.is_restricted} by {request.user.username}.",
            event_type="marketplace_offering_user_restriction_updated",
            event_context={"offering_user": offering_user},
        )
        logger.info(
            f"Restriction status for user {offering_user.user.username} in offering {offering_user.offering.name} has been set to {offering_user.is_restricted} by {request.user.username}."
        )
        return Response(status=status.HTTP_200_OK)


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


class StatsViewSet(rf_viewsets.ViewSet):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsSupport]

    @action(detail=False, methods=["get"])
    def organization_project_count(self, request, *args, **kwargs):
        data = structure_models.Project.available_objects.values(
            "customer__abbreviation", "customer__name", "customer__uuid"
        ).annotate(count=Count("customer__uuid"))
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=["get"])
    def organization_resource_count(self, request, *args, **kwargs):
        data = (
            models.Resource.objects.filter(state=models.Resource.States.OK)
            .values(
                "project__customer__abbreviation",
                "project__customer__name",
                "project__customer__uuid",
            )
            .annotate(count=Count("project__customer__uuid"))
        )
        serializer = serializers.CustomerStatsSerializer(data, many=True)
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=False, methods=["get"])
    def customer_member_count(self, request, *args, **kwargs):
        has_resources = models.Resource.objects.filter(
            state__in=(models.Resource.States.OK, models.Resource.States.UPDATING),
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

    @action(detail=False, methods=["get"])
    def resources_limits(self, request, *args, **kwargs):
        data = []

        for resource in (
            models.Resource.objects.filter(state=models.Resource.States.OK)
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

    @action(detail=False, methods=["get"])
    def count_users_of_service_providers(self, request, *args, **kwargs):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            "customer", "customer__organization_group"
        ):
            data = {
                "count": utils.get_service_provider_user_ids(
                    self.request.user, sp
                ).count()
            }
            data.update(self._get_service_provider_info(sp))
            result.append(data)

        return Response(
            result,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def count_projects_of_service_providers(self, request, *args, **kwargs):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            "customer", "customer__organization_group"
        ):
            data = {"count": utils.get_service_provider_project_ids(sp).count()}
            data.update(self._get_service_provider_info(sp))
            result.append(data)

        return Response(
            result,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def count_projects_of_service_providers_grouped_by_oecd(
        self, request, *args, **kwargs
    ):
        result = []

        for sp in models.ServiceProvider.objects.all().select_related(
            "customer", "customer__organization_group"
        ):
            project_ids = utils.get_service_provider_project_ids(sp)
            projects = (
                structure_models.Project.available_objects.filter(id__in=project_ids)
                .values("oecd_fos_2007_code")
                .annotate(count=Count("id"))
            )

            for p in projects:
                data = {
                    "count": p["count"],
                    "oecd_fos_2007_code": p["oecd_fos_2007_code"],
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

    @action(detail=False, methods=["get"])
    def projects_usages_grouped_by_oecd(self, request, *args, **kwargs):
        return Response(
            self._replace_keys_from_oecd_code_to_oecd_name(
                self._projects_usages_grouped_by_field("oecd_fos_2007_code")
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def projects_usages_grouped_by_industry_flag(self, request, *args, **kwargs):
        return Response(
            self._projects_usages_grouped_by_field("is_industry"),
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
                    state=models.Resource.States.OK, project__id__in=ids
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

    @action(detail=False, methods=["get"])
    def projects_limits_grouped_by_oecd(self, request, *args, **kwargs):
        return Response(
            self._replace_keys_from_oecd_code_to_oecd_name(
                self._projects_limits_grouped_by_field("oecd_fos_2007_code")
            ),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def projects_limits_grouped_by_industry_flag(self, request, *args, **kwargs):
        return Response(
            self._projects_limits_grouped_by_field("is_industry"),
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def total_cost_of_active_resources_per_offering(self, request, *args, **kwargs):
        start, end = utils.get_start_and_end_dates_from_request(self.request)
        invoice_items = (
            invoice_models.InvoiceItem.objects.filter(
                invoice__created__gte=start,
                invoice__created__lte=end,
            )
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
        return {
            "service_provider_uuid": service_provider.uuid.hex,
            "customer_uuid": service_provider.customer.uuid.hex,
            "customer_name": service_provider.customer.name,
            "customer_organization_group_uuid": (
                service_provider.customer.organization_group.uuid.hex
                if service_provider.customer.organization_group
                else ""
            ),
            "customer_organization_group_name": (
                service_provider.customer.organization_group.name
                if service_provider.customer.organization_group
                else ""
            ),
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
                    models.Resource.States.OK,
                    models.Resource.States.UPDATING,
                    models.Resource.States.TERMINATING,
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
                models.Resource.States.OK,
                models.Resource.States.UPDATING,
                models.Resource.States.TERMINATING,
            )
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

    @action(detail=False, methods=["get"])
    def count_active_resources_grouped_by_organization_group(
        self, request, *args, **kwargs
    ):
        result = (
            self.get_active_resources()
            .values(
                "offering__customer__organization_group__name",
                "offering__customer__organization_group__uuid",
            )
            .annotate(count=Count("id"))
            .order_by()
        )

        return Response(
            serializers.CountStatsSerializer(result, many=True).data,
            status=status.HTTP_200_OK,
        )

    def _get_count_projects_with_active_resources_grouped_by_provider_and_field(
        self, grouped_field
    ):
        return (
            structure_models.Project.objects.filter(is_removed=False)
            .filter(
                resource__state__in=(
                    models.Resource.States.OK,
                    models.Resource.States.UPDATING,
                    models.Resource.States.TERMINATING,
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
        instance = serializer.save()
        offering = instance.resource.offering
        utils.setup_linux_related_data(instance, offering)
        instance.save()

    def perform_update(self, serializer):
        instance = serializer.save()
        offering = instance.resource.offering
        utils.setup_linux_related_data(instance, offering)
        instance.save()


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
    """
    Returns count of resource categories for all resources accessible by user.
    """

    def get(self, request):
        # We need to reset ordering to avoid extra GROUP BY created field.
        resources = (
            models.Resource.objects.all()
            .order_by()
            .filter_for_user(request.user)
            .exclude(state=models.Resource.States.TERMINATED)
            .values("offering__category__uuid")
            .annotate(count=Count("*"))
        )
        return Response(
            {row["offering__category__uuid"].hex: row["count"] for row in resources}
        )
