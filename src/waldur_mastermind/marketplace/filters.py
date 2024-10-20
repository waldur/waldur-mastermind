import json

import django_filters
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _
from django_filters.widgets import BooleanWidget
from rest_framework import exceptions as rf_exceptions
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core.filters import LooseMultipleChoiceFilter
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_customers_by_permission,
    get_connected_projects,
    get_connected_projects_by_permission,
    get_project_users,
)
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.managers import get_connected_offerings
from waldur_mastermind.proposal import models as proposal_models
from waldur_pid import models as pid_models

from . import models

User = get_user_model()


class ServiceProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    customer_keyword = django_filters.CharFilter(method="filter_customer_keyword")
    o = django_filters.OrderingFilter(fields=(("customer__name", "customer_name"),))

    class Meta:
        model = models.ServiceProvider
        fields = []

    def filter_customer_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(customer__name__icontains=value)
            | Q(customer__abbreviation__icontains=value)
            | Q(customer__native_name__icontains=value)
        )


class OfferingFilter(structure_filters.NameFilterSet, django_filters.FilterSet):
    class Meta:
        model = models.Offering
        fields = []

    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")
    allowed_customer_uuid = django_filters.UUIDFilter(
        method="filter_allowed_customer", label="Allowed customer UUID"
    )
    service_manager_uuid = django_filters.UUIDFilter(
        method="filter_service_manager", label="Service manager UUID"
    )
    project_uuid = django_filters.UUIDFilter(
        method="filter_project", label="Project UUID"
    )
    parent_uuid = django_filters.UUIDFilter(field_name="parent__uuid")
    attributes = django_filters.CharFilter(method="filter_attributes")
    state = core_filters.MappedMultipleChoiceFilter(models.Offering.States.CHOICES)
    organization_group_uuid = LooseMultipleChoiceFilter(
        field_name="organization_groups__uuid"
    )
    category_uuid = django_filters.UUIDFilter(field_name="category__uuid")
    category_group_uuid = django_filters.UUIDFilter(field_name="category__group__uuid")
    billable = django_filters.BooleanFilter(widget=BooleanWidget)
    shared = django_filters.BooleanFilter(widget=BooleanWidget)
    description = django_filters.CharFilter(lookup_expr="icontains")
    keyword = django_filters.CharFilter(method="filter_keyword", label="Keyword")
    scope_uuid = django_filters.UUIDFilter(
        method=core_filters.get_generic_field_filter(
            [structure_models.ServiceSettings]
        ),
        label="Scope UUID",
    )
    accessible_via_calls = django_filters.BooleanFilter(
        label="Accessible via calls", method="filter_accessible_via_calls"
    )
    o = django_filters.OrderingFilter(
        fields=(
            "name",
            "created",
            "type",
            "total_customers",
            "total_cost",
            "total_cost_estimated",
            "state",
        )
    )
    type = LooseMultipleChoiceFilter()

    def filter_allowed_customer(self, queryset, name, value):
        return queryset.filter_for_customer(value)

    def filter_service_manager(self, queryset, name, value):
        return queryset.filter_for_service_manager(value)

    def filter_project(self, queryset, name, value):
        return queryset.filter_for_project(value)

    def filter_attributes(self, queryset, name, value):
        try:
            value = json.loads(value)
        except ValueError:
            raise rf_exceptions.ValidationError(
                _("Filter attribute is not valid json.")
            )

        if not isinstance(value, dict):
            raise rf_exceptions.ValidationError(
                _("Filter attribute should be an dict.")
            )

        for k, v in value.items():
            if isinstance(v, list):
                # If a filter value is a list, use multiple choice.
                queryset = queryset.filter(**{f"attributes__{k}__has_any_keys": v})
            else:
                queryset = queryset.filter(attributes__contains={k: v})
        return queryset

    def filter_keyword(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(description__icontains=value)
            | Q(customer__name__icontains=value)
            | Q(customer__abbreviation__icontains=value)
            | Q(customer__native_name__icontains=value)
        )

    def filter_queryset(self, queryset):
        for name, value in self.form.cleaned_data.items():
            extra_fields = (
                "total_customers",
                "total_cost",
                "total_cost_estimated",
            )
            if name == "o" and value and self.request.user.is_anonymous:
                for f in extra_fields:
                    (f in value) and value.remove(f)
                    ("-" + f in value) and value.remove("-" + f)

            queryset = self.filters[name].filter(queryset, value)
        return queryset

    def filter_accessible_via_calls(self, queryset, name, value):
        if value is None:
            return queryset

        from waldur_mastermind.proposal.models import Call, RequestedOffering

        offerings_ids = RequestedOffering.objects.filter(
            state=RequestedOffering.States.ACCEPTED, call__state=Call.States.ACTIVE
        ).values_list("offering_id", flat=True)

        if value:
            return queryset.filter(id__in=offerings_ids)
        else:
            return queryset.exclude(id__in=offerings_ids)


class OfferingCustomersFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        return queryset.filter_for_user(request.user)


class OfferingImportableFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset: QuerySet[models.Offering], view):
        if "importable" in request.query_params:
            queryset = queryset.filter(
                type__in=plugins.manager.get_importable_offering_types()
            )

            user = request.user

            if user.is_staff:
                return queryset

            # Import private offerings must be available for admins and managers
            projects_ids = set(
                get_connected_projects(
                    user, (RoleEnum.PROJECT_ADMIN, RoleEnum.PROJECT_MANAGER)
                )
            )

            return queryset.filter(shared=False).filter(
                Q(customer__in=get_connected_customers(user, RoleEnum.CUSTOMER_OWNER))
                | Q(project__in=projects_ids)
            )
        return queryset


class OfferingFilterMixin(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
    )
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")

    def filter_service_manager(self, queryset, name, value):
        if not is_uuid_like(value):
            return queryset.none()
        User = get_user_model()
        try:
            user = User.objects.get(uuid=value)
        except User.DoesNotExist:
            return queryset.none()
        offerings = get_connected_offerings(user)
        return queryset.filter(
            offering__shared=True,
            offering__in=offerings,
        )


class OfferingPermissionFilter(structure_filters.UserPermissionFilter):
    class Meta:
        fields = []
        model = UserRole

    offering = django_filters.UUIDFilter(method="filter_by_offering")
    customer = django_filters.UUIDFilter(method="filter_by_customer")

    def filter_by_offering(self, queryset, name, value):
        try:
            offering = models.Offering.objects.get(uuid=value)
        except models.Offering.DoesNotExist:
            return queryset.none()
        return queryset.filter(object_id=offering.id)

    def filter_by_customer(self, queryset, name, value):
        try:
            customer = structure_models.Customer.objects.get(uuid=value)
        except structure_models.Customer.DoesNotExist:
            return queryset.none()
        offerings = models.Offering.objects.filter(customer=customer)
        return queryset.filter(object_id__in=offerings.values_list("id", flat=True))


class ScreenshotFilter(OfferingFilterMixin, django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=("name", "created"))

    class Meta:
        model = models.Screenshot
        fields = []


class OrderFilter(OfferingFilterMixin, django_filters.FilterSet):
    query = django_filters.CharFilter(method="filter_query")
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    offering_type = core_filters.LooseMultipleChoiceFilter(
        field_name="offering__type", lookup_expr="exact"
    )
    category_uuid = django_filters.UUIDFilter(field_name="offering__category__uuid")
    provider_uuid = django_filters.UUIDFilter(field_name="offering__customer__uuid")
    customer_uuid = django_filters.UUIDFilter(field_name="project__customer__uuid")
    service_manager_uuid = django_filters.UUIDFilter(method="filter_service_manager")
    state = core_filters.MappedMultipleChoiceFilter(models.Order.States.CHOICES)
    type = core_filters.MappedMultipleChoiceFilter(models.Order.Types.CHOICES)
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    created = django_filters.DateTimeFilter(lookup_expr="gte", label="Created after")
    modified = django_filters.DateTimeFilter(lookup_expr="gte", label="Modified after")
    can_approve_as_consumer = django_filters.BooleanFilter(
        method="filter_can_approve_as_consumer",
    )
    can_approve_as_provider = django_filters.BooleanFilter(
        method="filter_can_approve_as_provider",
    )

    o = django_filters.OrderingFilter(
        fields=("created", "consumer_reviewed_at", "cost", "state")
    )

    class Meta:
        model = models.Order
        fields = []

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            if queryset.filter(uuid=value).exists():
                return queryset.filter(uuid=value)

        query = queryset.filter(
            Q(project__name__icontains=value) | Q(attributes__name__icontains=value)
        )
        return query

    def filter_can_approve_as_consumer(self, queryset, name, value):
        user = self.request.user

        queryset = queryset.filter(state=models.Order.States.PENDING_CONSUMER)

        if value and not user.is_staff:
            connected_customers = get_connected_customers_by_permission(
                user, PermissionEnum.APPROVE_ORDER
            )
            connected_projects = get_connected_projects_by_permission(
                user, PermissionEnum.APPROVE_ORDER
            )
            queryset = queryset.filter(
                Q(project__customer__in=connected_customers)
                | Q(project__in=connected_projects)
            )

        return queryset

    def filter_can_approve_as_provider(self, queryset, name, value):
        user = self.request.user

        queryset = queryset.filter(state=models.Order.States.PENDING_PROVIDER)

        if value and not user.is_staff:
            connected_customers = get_connected_customers_by_permission(
                user, PermissionEnum.APPROVE_ORDER
            )
            queryset = queryset.filter(offering__customer__in=connected_customers)

        return queryset


class ResourceFilter(
    OfferingFilterMixin,
    structure_filters.NameFilterSet,
    core_filters.CreatedModifiedFilter,
):
    query = django_filters.CharFilter(method="filter_query", label="Query")
    offering_type = django_filters.CharFilter(field_name="offering__type")
    offering_billable = django_filters.UUIDFilter(field_name="offering__billable")
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    project_name = django_filters.CharFilter(field_name="project__name")
    customer_uuid = django_filters.UUIDFilter(field_name="project__customer__uuid")
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="project__customer__uuid"
    )
    service_manager_uuid = django_filters.UUIDFilter(
        method="filter_service_manager", label="Service Manager UUID"
    )
    category_uuid = django_filters.UUIDFilter(field_name="offering__category__uuid")
    provider_uuid = django_filters.UUIDFilter(field_name="offering__customer__uuid")
    backend_id = django_filters.CharFilter(label="Backend ID")
    state = core_filters.MappedMultipleChoiceFilter(models.Resource.States.CHOICES)
    runtime_state = django_filters.CharFilter(
        field_name="backend_metadata__runtime_state", label="Runtime state"
    )
    downscaled = django_filters.BooleanFilter(field_name="downscaled")
    restrict_member_access = django_filters.BooleanFilter(
        field_name="restrict_member_access"
    )
    paused = django_filters.BooleanFilter(field_name="paused")
    lexis_links_supported = django_filters.BooleanFilter(
        method="filter_lexis_links_supported", label="LEXIS links supported"
    )
    visible_to_username = django_filters.CharFilter(
        method="filter_visible_to_username", label="Visible to username"
    )
    o = django_filters.OrderingFilter(
        fields=(
            "name",
            "created",
        )
    )

    class Meta:
        model = models.Resource
        fields = []

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            if queryset.filter(uuid=value).exists():
                return queryset.filter(uuid=value)

        query = queryset.filter(
            Q(name__icontains=value)
            | Q(backend_id__iexact=value)
            | Q(effective_id__iexact=value)
            | Q(backend_metadata__external_ips__icontains=value)
            | Q(backend_metadata__internal_ips__icontains=value)
            | Q(backend_metadata__hypervisor_hostname__icontains=value)
            | Q(backend_metadata__router_fixed_ips__icontains=value)
        )

        # TODO: Drop union once plugin UUID is deprecated
        if is_uuid_like(value):
            plugin_resources_qs = self.filter_scope_uuid(queryset, name, value)
            if plugin_resources_qs.exists():
                return plugin_resources_qs
            else:
                return query
        else:
            return query

    def filter_scope_uuid(self, queryset, name, value):
        for offering_type in plugins.manager.get_offering_types():
            resource_model = plugins.manager.get_resource_model(offering_type)

            if not resource_model:
                continue

            try:
                obj = resource_model.objects.get(uuid=value)
                ct = ContentType.objects.get_for_model(resource_model)

                if queryset.filter(content_type=ct, object_id=obj.id).exists():
                    return queryset.filter(content_type=ct, object_id=obj.id)

            except resource_model.DoesNotExist:
                continue

        return queryset.none()

    def filter_lexis_links_supported(self, queryset, name, value):
        if value:
            return queryset.filter(offering__plugin_options__has_key="heappe_url")
        else:
            return queryset.exclude(offering__plugin_options__has_key="heappe_url")

    def filter_visible_to_username(self, queryset, name, value):
        if value:
            user = User.objects.filter(username=value).first()

            if not user:
                return queryset.none()

            return queryset.filter_for_user(user)
        else:
            return queryset


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return []

    def get_field_name(self):
        return "scope"


class RobotAccountFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    project_uuid = django_filters.UUIDFilter(field_name="resource__project__uuid")
    customer_uuid = django_filters.UUIDFilter(
        field_name="resource__project__customer__uuid"
    )
    provider_uuid = django_filters.UUIDFilter(
        field_name="resource__offering__customer__uuid"
    )

    class Meta:
        model = models.RobotAccount
        fields = ["type"]


class ResourceUserFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    role_uuid = django_filters.UUIDFilter(field_name="role__uuid")
    role_name = django_filters.CharFilter(field_name="role__name")
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")

    class Meta:
        model = models.ResourceUser
        fields = []


# TODO: Remove after migration of clients to a new endpoint
class PlanFilter(OfferingFilterMixin, django_filters.FilterSet):
    class Meta:
        model = models.Plan
        fields = []

    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")


class CategoryComponentUsageScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return [structure_models.Project, structure_models.Customer]

    def get_field_name(self):
        return "scope"


class CategoryComponentUsageFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryComponentUsage
        fields = []

    date_before = django_filters.DateFilter(field_name="date", lookup_expr="lte")
    date_after = django_filters.DateFilter(field_name="date", lookup_expr="gte")


class ComponentUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    offering_uuid = django_filters.UUIDFilter(field_name="resource__offering__uuid")
    project_uuid = django_filters.UUIDFilter(field_name="resource__project__uuid")
    customer_uuid = django_filters.UUIDFilter(
        field_name="resource__project__customer__uuid"
    )
    date_before = django_filters.DateFilter(field_name="date__date", lookup_expr="lte")
    date_after = django_filters.DateFilter(field_name="date__date", lookup_expr="gte")
    type = django_filters.CharFilter(field_name="component__type")

    class Meta:
        model = models.ComponentUsage
        fields = ["billing_period"]


class ComponentUserUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="component_usage__resource__uuid",
    )
    resource_uuid = django_filters.UUIDFilter(
        field_name="component_usage__resource__uuid"
    )
    offering_uuid = django_filters.UUIDFilter(
        field_name="component_usage__resource__offering__uuid"
    )
    project_uuid = django_filters.UUIDFilter(
        field_name="component_usage__resource__project__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(
        field_name="component_usage__resource__project__customer__uuid"
    )
    date_before = django_filters.DateFilter(
        field_name="component_usage__date__date", lookup_expr="lte"
    )
    date_after = django_filters.DateFilter(
        field_name="component_usage__date__date", lookup_expr="gte"
    )
    type = django_filters.CharFilter(field_name="component_usage__component__type")

    class Meta:
        model = models.ComponentUserUsage
        fields = ["component_usage__billing_period"]


class OfferingReferralFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(
        fields=(
            "published",
            "relation_type",
            "resource_type",
        )
    )

    class Meta:
        model = pid_models.DataciteReferral
        fields = []


class OfferingReferralScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def is_anonymous_allowed(self):
        return settings.WALDUR_MARKETPLACE["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"]

    def get_related_models(self):
        return [models.Offering]

    def get_field_name(self):
        return "scope"


class OfferingFileFilter(OfferingFilterMixin, django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=("name", "created"))

    class Meta:
        model = models.OfferingFile
        fields = []


class ExternalOfferingFilterBackend(core_filters.ExternalFilterBackend):
    pass


class CustomerResourceFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if "has_resources" in request.query_params:
            customers = models.Resource.objects.all().values_list(
                "project__customer_id", flat=True
            )
            queryset = queryset.filter(pk__in=customers)
        return queryset


class ServiceProviderOfferingFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer_uuid = request.query_params.get("service_provider_uuid")

        if customer_uuid and is_uuid_like(customer_uuid):
            customers = models.Resource.objects.filter(
                offering__customer__uuid=customer_uuid
            ).values_list("project__customer_id", flat=True)
            queryset = queryset.filter(pk__in=customers)
        return queryset


class CustomerServiceProviderFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        is_service_provider = request.query_params.get("is_service_provider")
        if is_service_provider in ["true", "True"]:
            customers = models.ServiceProvider.objects.values_list(
                "customer_id", flat=True
            )
            return queryset.filter(pk__in=customers)
        return queryset


class CustomerCallManagingOrganisationFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        is_call_managing_organization = request.query_params.get(
            "is_call_managing_organization"
        )
        if is_call_managing_organization in ["true", "True"]:
            customers = proposal_models.CallManagingOrganisation.objects.values_list(
                "customer_id", flat=True
            )
            return queryset.filter(pk__in=customers)
        return queryset


class OfferingUserRoleFilter(OfferingFilterMixin):
    class Meta:
        model = models.OfferingUserRole
        fields = []


class OfferingUserFilter(OfferingFilterMixin, core_filters.CreatedModifiedFilter):
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")
    user_username = django_filters.CharFilter(
        field_name="user__username", lookup_expr="iexact"
    )
    is_not_propagated = django_filters.BooleanFilter(
        field_name="propagation_date", widget=BooleanWidget, lookup_expr="isnull"
    )
    propagated_after = django_filters.DateTimeFilter(
        field_name="propagation_date", lookup_expr="gte"
    )
    propagated_before = django_filters.DateTimeFilter(
        field_name="propagation_date", lookup_expr="lte"
    )
    provider_uuid = django_filters.UUIDFilter(field_name="offering__customer__uuid")
    is_restricted = django_filters.BooleanFilter(field_name="is_restricted")
    o = django_filters.OrderingFilter(
        fields=("created", "modified", "username", "propagation_date")
    )
    query = django_filters.CharFilter(method="filter_query")

    class Meta:
        model = models.OfferingUser
        fields = []

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(offering__name__icontains=value)
            | Q(username__icontains=value)
            | Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
        )


class OfferingUserGroupFilter(OfferingFilterMixin, core_filters.CreatedModifiedFilter):
    o = django_filters.OrderingFilter(fields=("created",))


class CategoryGroupFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryGroup
        fields = []

    title = django_filters.CharFilter(lookup_expr="icontains")


class CategoryFilter(django_filters.FilterSet):
    class Meta:
        model = models.Category
        fields = []

    customer_uuid = django_filters.UUIDFilter(
        method="filter_customer_uuid", label="Customer UUID"
    )

    group_uuid = django_filters.UUIDFilter(field_name="group__uuid")

    title = django_filters.CharFilter(lookup_expr="icontains")

    customers_offerings_state = django_filters.MultipleChoiceFilter(
        choices=models.Offering.States.CHOICES,
        label="Customers offerings state",
        method="filter_customers_offerings_state",
    )

    has_shared = django_filters.BooleanFilter(
        method="filter_has_shared", label="Has shared"
    )

    offering_name = django_filters.CharFilter(
        field_name="offerings__name", lookup_expr="icontains"
    )

    def filter_customer_uuid(self, queryset, name, value):
        states = self.request.GET.getlist("customers_offerings_state")
        offerings = models.Offering.objects.filter(customer__uuid=value)

        if states:
            offerings = offerings.filter(state__in=states)

        category_ids = offerings.values_list("category_id", flat=True)

        return queryset.filter(id__in=category_ids)

    def filter_customers_offerings_state(self, queryset, name, value):
        return queryset

    def filter_has_shared(self, queryset, name, value):
        category_ids = models.Offering.objects.filter(shared=True).values_list(
            "category_id", flat=True
        )
        return queryset.filter(id__in=category_ids)


class CategoryColumnFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryColumn
        fields = []

    category_uuid = django_filters.UUIDFilter(field_name="category__uuid")
    title = django_filters.CharFilter(lookup_expr="icontains")


class PlanComponentFilter(django_filters.FilterSet):
    class Meta:
        model = models.PlanComponent
        fields = []

    offering_uuid = django_filters.UUIDFilter(
        field_name="plan__offering__uuid", label="Offering UUID"
    )

    plan_uuid = django_filters.UUIDFilter(field_name="plan__uuid", label="Plan UUID")

    shared = django_filters.BooleanFilter(
        widget=BooleanWidget, field_name="plan__offering__shared"
    )

    archived = django_filters.BooleanFilter(
        field_name="plan__archived",
    )


class MarketplaceInvoiceItemsFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user

        if user.is_staff:
            return queryset

        customer_ids = get_connected_customers(
            user, [RoleEnum.CUSTOMER_OWNER, RoleEnum.CUSTOMER_MANAGER]
        )

        return queryset.filter(resource__offering__customer_id__in=customer_ids)


class MarketplaceInvoiceItemsFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(
        fields=(
            ("unit_price", "unit_price"),
            ("resource__offering__name", "resource_offering_name"),
            ("invoice__customer__name", "invoice_customer_name"),
            ("project__name", "project_name"),
        )
    )

    customer_uuid = django_filters.UUIDFilter(
        field_name="invoice__customer__uuid",
    )
    project_uuid = django_filters.UUIDFilter(
        field_name="project__uuid",
    )
    offering_uuid = django_filters.UUIDFilter(
        field_name="resource__offering__uuid",
    )
    invoice_month = django_filters.NumberFilter(field_name="invoice__month")
    invoice_year = django_filters.NumberFilter(field_name="invoice__year")

    class Meta:
        model = invoices_models.InvoiceItem
        fields = [
            "customer_uuid",
            "project_uuid",
            "offering_uuid",
            "invoice_month",
            "invoice_year",
        ]


class PlanFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user

        if user.is_staff:
            return queryset

        customer_ids = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)

        organization_group_ids = structure_models.Customer.objects.filter(
            id__in=customer_ids
        ).values_list("organization_group_id", flat=True)
        organization_groups = structure_models.OrganizationGroup.objects.filter(
            id__in=organization_group_ids
        )

        return queryset.filter(
            Q(organization_groups__isnull=True)
            | Q(organization_groups__in=organization_groups)
        )


def user_extra_query(user):
    customer_ids = get_connected_customers(
        user, (RoleEnum.CUSTOMER_OWNER, RoleEnum.CUSTOMER_MANAGER)
    )
    offering_ids = models.Offering.objects.filter(
        shared=True, customer_id__in=customer_ids
    ).values_list("id", flat=True)

    project_ids = (
        models.Resource.objects.filter(offering_id__in=offering_ids)
        .exclude(state=models.Resource.States.TERMINATED)
        .values_list("project_id", flat=True)
    )
    user_ids = get_project_users(project_ids)

    return Q(id__in=user_ids)


structure_filters.ExternalCustomerFilterBackend.register(CustomerResourceFilter())
structure_filters.ExternalCustomerFilterBackend.register(
    ServiceProviderOfferingFilter()
)
structure_filters.ExternalCustomerFilterBackend.register(
    CustomerServiceProviderFilter()
)
structure_filters.ExternalCustomerFilterBackend.register(
    CustomerCallManagingOrganisationFilter()
)

structure_filters.UserFilterBackend.register_extra_query(user_extra_query)
