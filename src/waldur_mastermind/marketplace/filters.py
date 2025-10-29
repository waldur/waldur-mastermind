import json

import django_filters
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, F, Q, QuerySet
from django.utils.translation import gettext_lazy as _
from django_filters import DateFromToRangeFilter
from django_filters.widgets import BooleanWidget
from rest_framework import exceptions as rf_exceptions
from rest_framework.filters import BaseFilterBackend

from waldur_core.checklist import models as checklist_models
from waldur_core.core import filters as core_filters
from waldur_core.core.filters import (
    CharInFilter,
    LooseMultipleChoiceFilter,
    UUIDInFilter,
    get_generic_field_filter,
)
from waldur_core.core.models import User
from waldur_core.core.utils import is_uuid_like
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.filters import UserPermissionFilter
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
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    CourseAccountState,
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
    get_connected_offerings,
)
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import CallStates, RequestedOfferingStates
from waldur_pid import models as pid_models

from . import models


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


class OfferingFilter(
    core_filters.CreatedModifiedFilter,
    structure_filters.NameFilterSet,
    django_filters.FilterSet,
):
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
    state = core_filters.MappedMultipleChoiceFilter(OfferingStates.CHOICES)
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
        method=get_generic_field_filter(
            models_to_search=[structure_models.ServiceSettings]
        ),
        label="Scope UUID",
    )
    accessible_via_calls = django_filters.BooleanFilter(
        label="Accessible via calls", method="filter_accessible_via_calls"
    )
    resource_customer_uuid = django_filters.UUIDFilter(
        method="filter_resource_customer_uuid", label="Resource customer UUID"
    )
    resource_project_uuid = django_filters.UUIDFilter(
        method="filter_resource_project_uuid", label="Resource project UUID"
    )
    uuid_list = django_filters.CharFilter(
        method="filter_uuid_list",
        label="Comma-separated offering UUIDs",
    )
    has_terms_of_service = django_filters.BooleanFilter(
        method="filter_has_terms_of_service",
        label="Has Terms of Service",
        widget=BooleanWidget,
    )
    has_active_terms_of_service = django_filters.BooleanFilter(
        method="filter_has_active_terms_of_service",
        label="Has Active Terms of Service",
        widget=BooleanWidget,
    )
    user_has_consent = django_filters.BooleanFilter(
        method="filter_user_has_consent",
        label="User Has Consent",
        widget=BooleanWidget,
    )
    user_has_offering_user = django_filters.BooleanFilter(
        method="filter_user_has_offering_user",
        label="User Has Offering User",
        widget=BooleanWidget,
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by offering name, slug or description",
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

    def filter_resource_customer_uuid(self, queryset, name, value):
        valid_ids = (
            models.Resource.objects.filter(project__customer__uuid=value)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("offering_id", flat=True)
            .distinct()
        )
        return queryset.filter(id__in=valid_ids)

    def filter_resource_project_uuid(self, queryset, name, value):
        valid_ids = (
            models.Resource.objects.filter(project__uuid=value)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("offering_id", flat=True)
            .distinct()
        )
        return queryset.filter(id__in=valid_ids)

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

        from waldur_mastermind.proposal.models import RequestedOffering

        offerings_ids = RequestedOffering.objects.filter(
            state=RequestedOfferingStates.ACCEPTED, call__state=CallStates.ACTIVE
        ).values_list("offering_id", flat=True)

        if value:
            return queryset.filter(id__in=offerings_ids)
        else:
            return queryset.exclude(id__in=offerings_ids)

    def filter_uuid_list(self, queryset, name, value):
        if not value:
            return queryset.none()

        uuids = {u.strip() for u in value.split(",") if u.strip()}

        if not uuids:
            return queryset.none()

        return queryset.filter(uuid__in=uuids).distinct()

    def filter_has_active_terms_of_service(self, queryset, name, value):
        if value is None:
            return queryset

        if value:
            return queryset.filter(terms_of_service_configs__is_active=True).distinct()
        else:
            return queryset.exclude(terms_of_service_configs__is_active=True).distinct()

    def filter_has_terms_of_service(self, queryset, name, value):
        if value is None:
            return queryset

        if value:
            return queryset.filter(terms_of_service_configs__isnull=False).distinct()
        else:
            return queryset.filter(terms_of_service_configs__isnull=True).distinct()

    def filter_user_has_consent(self, queryset, name, value):
        if value is None:
            return queryset

        request = self.request
        if not request or not request.user:
            return queryset.none() if value else queryset

        user = request.user
        if value:
            return queryset.filter(
                user_consents__user=user, user_consents__revocation_date__isnull=True
            ).distinct()
        else:
            return queryset.exclude(
                user_consents__user=user, user_consents__revocation_date__isnull=True
            ).distinct()

    def filter_user_has_offering_user(self, queryset, name, value):
        if value is None:
            return queryset

        request = self.request
        if not request or not request.user:
            return queryset.none() if value else queryset

        user = request.user
        if value:
            return queryset.filter(offeringuser__user=user).distinct()
        else:
            return queryset.exclude(offeringuser__user=user).distinct()

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            if queryset.filter(uuid=value).exists():
                return queryset.filter(uuid=value)

        query = queryset.filter(
            Q(name__icontains=value)
            | Q(slug__icontains=value)
            | Q(description__icontains=value)
        )
        return query


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
    """Mixin to provide common offering-related filters."""

    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
    )
    offering_uuid = UUIDInFilter(field_name="offering__uuid")
    offering_slug = CharInFilter(field_name="offering__slug")
    parent_offering_uuid = django_filters.UUIDFilter(
        field_name="offering__parent__uuid"
    )

    def filter_service_manager(self, queryset, name, value):
        if not is_uuid_like(value):
            return queryset.none()

        try:
            user = User.objects.get(uuid=value)
        except User.DoesNotExist:
            return queryset.none()
        offerings = get_connected_offerings(user)
        return queryset.filter(
            offering__shared=True,
            offering__in=offerings,
        )


class OfferingPermissionFilter(UserPermissionFilter):
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


class SoftwareCatalogFilter(django_filters.FilterSet):
    """Filter for SoftwareCatalog model."""

    name = django_filters.CharFilter(lookup_expr="icontains")
    version = django_filters.CharFilter(lookup_expr="icontains")

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("version", "version"),
            ("created", "created"),
            ("modified", "modified"),
        ),
        field_labels={
            "name": "Catalog name",
            "version": "Version",
            "created": "Created date",
            "modified": "Modified date",
        },
    )

    class Meta:
        model = models.SoftwareCatalog
        fields = ["name", "version"]


class SoftwarePackageFilter(django_filters.FilterSet):
    """Filter for SoftwarePackage model."""

    query = django_filters.CharFilter(
        method="filter_query",
        label="query",
        help_text="Query packages by name, description, or version (case-insensitive partial match)",
    )
    offering_uuid = django_filters.UUIDFilter(
        method="filter_offering_uuid",
        label="Offering UUID",
        help_text="Filter packages available for a specific offering",
    )
    catalog_uuid = django_filters.UUIDFilter(
        field_name="catalog__uuid",
        label="Catalog UUID",
        help_text="Filter packages from a specific software catalog",
    )
    catalog_name = django_filters.CharFilter(
        field_name="catalog__name",
        lookup_expr="icontains",
        label="Catalog name",
        help_text="Filter packages by catalog name (case-insensitive partial match)",
    )
    catalog_version = django_filters.CharFilter(
        field_name="catalog__version",
        lookup_expr="icontains",
        label="Catalog version",
        help_text="Filter packages by catalog version (case-insensitive partial match)",
    )
    name = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Package name",
        help_text="Filter packages by name (case-insensitive partial match)",
    )
    description = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Description",
        help_text="Filter packages by description (case-insensitive partial match)",
    )
    cpu_family = django_filters.CharFilter(
        method="filter_cpu_family",
        label="CPU Family",
        help_text="Filter packages available for specific CPU family (e.g., x86_64, aarch64)",
    )
    cpu_microarchitecture = django_filters.CharFilter(
        method="filter_cpu_microarchitecture",
        label="CPU Microarchitecture",
        help_text="Filter packages available for specific CPU microarchitecture (e.g., generic, zen2, haswell)",
    )
    has_version = django_filters.CharFilter(
        method="filter_has_version",
        label="Has version",
        help_text="Filter packages that have a specific version",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("catalog__name", "catalog_name"),
            ("catalog__version", "catalog_version"),
            ("created", "created"),
            ("modified", "modified"),
        ),
        field_labels={
            "name": "Package name",
            "catalog_name": "Catalog name",
            "catalog_version": "Catalog version",
            "created": "Created date",
            "modified": "Modified date",
        },
    )

    class Meta:
        model = models.SoftwarePackage
        fields = ["catalog_uuid", "name", "description"]

    def filter_offering_uuid(self, queryset, name, value):
        """Filter packages available for specific offering."""
        return queryset.filter(catalog__offerings__offering__uuid=value).distinct()

    def filter_cpu_family(self, queryset, name, value):
        """Filter packages with versions available for CPU family."""
        return queryset.filter(versions__targets__cpu_family=value).distinct()

    def filter_cpu_microarchitecture(self, queryset, name, value):
        """Filter packages with versions available for CPU microarchitecture."""
        return queryset.filter(
            versions__targets__cpu_microarchitecture=value
        ).distinct()

    def filter_has_version(self, queryset, name, value):
        """Filter packages that have a specific version."""
        return queryset.filter(versions__version=value).distinct()

    def filter_query(self, queryset, name, value):
        """Search packages by name, description, or version."""
        if not value:
            return queryset

        return queryset.filter(
            Q(name__icontains=value)
            | Q(description__icontains=value)
            | Q(versions__version__icontains=value)
        ).distinct()


class SoftwareVersionFilter(django_filters.FilterSet):
    """Filter for SoftwareVersion model."""

    package_uuid = django_filters.UUIDFilter(field_name="package__uuid")
    catalog_uuid = django_filters.UUIDFilter(field_name="package__catalog__uuid")
    offering_uuid = django_filters.UUIDFilter(method="filter_offering_uuid")
    package_name = django_filters.CharFilter(
        field_name="package__name", lookup_expr="icontains"
    )
    version = django_filters.CharFilter(lookup_expr="icontains")
    cpu_family = django_filters.CharFilter(field_name="targets__cpu_family")
    cpu_microarchitecture = django_filters.CharFilter(
        field_name="targets__cpu_microarchitecture"
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("version", "version"),
            ("package__name", "package_name"),
            ("release_date", "release_date"),
            ("created", "created"),
        ),
        field_labels={
            "version": "Version",
            "package_name": "Package name",
            "release_date": "Release date",
            "created": "Created date",
        },
    )

    class Meta:
        model = models.SoftwareVersion
        fields = ["package_uuid", "version"]

    def filter_offering_uuid(self, queryset, name, value):
        return queryset.filter(
            package__catalog__offerings__offering__uuid=value
        ).distinct()


class SoftwareTargetFilter(django_filters.FilterSet):
    """Filter for SoftwareTarget model."""

    version_uuid = django_filters.UUIDFilter(field_name="version__uuid")
    package_uuid = django_filters.UUIDFilter(field_name="version__package__uuid")
    catalog_uuid = django_filters.UUIDFilter(
        field_name="version__package__catalog__uuid"
    )
    offering_uuid = django_filters.UUIDFilter(method="filter_offering_uuid")
    cpu_family = django_filters.CharFilter(lookup_expr="icontains")
    cpu_microarchitecture = django_filters.CharFilter(lookup_expr="icontains")
    path = django_filters.CharFilter(lookup_expr="icontains")

    o = django_filters.OrderingFilter(
        fields=(
            ("cpu_family", "cpu_family"),
            ("cpu_microarchitecture", "cpu_microarchitecture"),
            ("version__package__name", "package_name"),
            ("created", "created"),
        ),
        field_labels={
            "cpu_family": "CPU Family",
            "cpu_microarchitecture": "CPU Microarchitecture",
            "package_name": "Package name",
            "created": "Created date",
        },
    )

    class Meta:
        model = models.SoftwareTarget
        fields = ["cpu_family", "cpu_microarchitecture"]

    def filter_offering_uuid(self, queryset, name, value):
        return queryset.filter(
            version__package__catalog__offerings__offering__uuid=value
        ).distinct()


class OfferingSoftwareCatalogFilter(django_filters.FilterSet):
    """Filter for OfferingSoftwareCatalog model."""

    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    catalog_uuid = django_filters.UUIDFilter(field_name="catalog__uuid")
    catalog_name = django_filters.CharFilter(
        field_name="catalog__name", lookup_expr="icontains"
    )
    offering_name = django_filters.CharFilter(
        field_name="offering__name", lookup_expr="icontains"
    )
    partition_uuid = django_filters.UUIDFilter(field_name="partition__uuid")
    partition_name = django_filters.CharFilter(
        field_name="partition__partition_name", lookup_expr="icontains"
    )
    has_partition = django_filters.BooleanFilter(method="filter_has_partition")
    cpu_family = django_filters.CharFilter(method="filter_cpu_family")

    o = django_filters.OrderingFilter(
        fields=(
            ("offering__name", "offering_name"),
            ("catalog__name", "catalog_name"),
            ("catalog__version", "catalog_version"),
            ("partition__partition_name", "partition_name"),
            ("partition__priority_tier", "partition_priority"),
            ("created", "created"),
        ),
        field_labels={
            "offering_name": "Offering name",
            "catalog_name": "Catalog name",
            "catalog_version": "Catalog version",
            "partition_name": "Partition name",
            "partition_priority": "Partition priority",
            "created": "Created date",
        },
    )

    class Meta:
        model = models.OfferingSoftwareCatalog
        fields = ["offering_uuid", "catalog_uuid", "partition_uuid", "partition_name"]

    def filter_has_partition(self, queryset, name, value):
        """Filter by whether the catalog has an associated partition."""
        if value:
            return queryset.filter(partition__isnull=False)
        else:
            return queryset.filter(partition__isnull=True)

    def filter_cpu_family(self, queryset, name, value):
        """Filter by enabled CPU family."""
        return queryset.filter(enabled_architectures__contains=[value])

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


class OrderFilter(
    core_filters.CreatedModifiedFilter, OfferingFilterMixin, django_filters.FilterSet
):
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by order UUID, slug, project name or resource name",
    )
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    offering_type = core_filters.LooseMultipleChoiceFilter(
        field_name="offering__type", lookup_expr="exact"
    )
    category_uuid = django_filters.UUIDFilter(field_name="offering__category__uuid")
    provider_uuid = django_filters.UUIDFilter(field_name="offering__customer__uuid")
    customer_uuid = django_filters.UUIDFilter(field_name="project__customer__uuid")
    service_manager_uuid = django_filters.UUIDFilter(method="filter_service_manager")
    state = core_filters.MappedMultipleChoiceFilter(OrderStates.CHOICES)
    type = core_filters.MappedMultipleChoiceFilter(OrderTypes.CHOICES)
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
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
            Q(project__name__icontains=value)
            | Q(attributes__name__icontains=value)
            | Q(slug__icontains=value)
        )
        return query

    def filter_can_approve_as_consumer(self, queryset, name, value):
        user = self.request.user

        queryset = queryset.filter(state=OrderStates.PENDING_CONSUMER)

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

        queryset = queryset.filter(state=OrderStates.PENDING_PROVIDER)

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
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by resource UUID, name, slug, backend ID, effective ID, IPs or hypervisor",
    )

    offering_type = django_filters.CharFilter(field_name="offering__type")
    offering_billable = django_filters.BooleanFilter(field_name="offering__billable")
    plan_uuid = django_filters.UUIDFilter(field_name="plan__uuid")
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
    state = core_filters.MappedMultipleChoiceFilter(ResourceStates.CHOICES)
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
    offering_shared = django_filters.BooleanFilter(
        field_name="offering__shared", label="Offering shared"
    )
    has_terminate_date = django_filters.BooleanFilter(
        method="filter_has_termination_date", label="Has termination date"
    )
    usage_based = django_filters.BooleanFilter(
        method="filter_usage_based", label="Filter by usage-based offerings"
    )
    limit_based = django_filters.BooleanFilter(
        method="filter_limit_based", label="Filter by limit-based offerings"
    )
    only_limit_based = django_filters.BooleanFilter(
        method="filter_only_limit_based",
        label="Filter resources with only limit-based components",
    )
    only_usage_based = django_filters.BooleanFilter(
        method="filter_only_usage_based",
        label="Filter resources with only usage-based components",
    )
    component_count = django_filters.NumberFilter(
        method="filter_component_count",
        label="Filter by exact number of components",
    )
    limit_component_count = django_filters.NumberFilter(
        method="filter_limit_component_count",
        label="Filter by exact number of limit-based components",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("created", "created"),
            ("project__name", "project_name"),
            ("state", "state"),
        )
    )

    class Meta:
        model = models.Resource
        fields = []

    def filter_has_termination_date(self, queryset: ResourceQuerySet, name, value):
        return queryset.exclude(end_date__isnull=value)

    def filter_query(self, queryset: ResourceQuerySet, name, value):
        if is_uuid_like(value):
            if queryset.filter(uuid=value).exists():
                return queryset.filter(uuid=value)

        query = queryset.filter(
            Q(name__icontains=value)
            | Q(slug__icontains=value)
            | Q(backend_id__iexact=value)
            | Q(effective_id__iexact=value)
            | Q(backend_metadata__external_ips__icontains=value)
            | Q(backend_metadata__internal_ips__icontains=value)
            | Q(backend_metadata__hypervisor_hostname__icontains=value)
            | Q(backend_metadata__router_fixed_ips__icontains=value)
            | Q(backend_metadata__external_address__icontains=value)
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

    def filter_scope_uuid(self, queryset: ResourceQuerySet, name, value):
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

    def filter_lexis_links_supported(self, queryset: ResourceQuerySet, name, value):
        if value:
            return queryset.filter(offering__plugin_options__has_key="heappe_url")
        else:
            return queryset.exclude(offering__plugin_options__has_key="heappe_url")

    def filter_visible_to_username(self, queryset: ResourceQuerySet, name, value):
        if value:
            user = User.objects.filter(username=value).first()

            if not user:
                return queryset.none()

            return queryset.filter_for_service_consumer(user)
        else:
            return queryset

    def filter_usage_based(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset
        if value:
            return queryset.filter(
                offering__components__billing_type=BillingTypes.USAGE
            ).distinct()
        else:
            return queryset.exclude(
                offering__components__billing_type=BillingTypes.USAGE
            ).distinct()

    def filter_limit_based(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset
        if value:
            return queryset.filter(
                offering__components__billing_type=BillingTypes.LIMIT
            ).distinct()
        else:
            return queryset.exclude(
                offering__components__billing_type=BillingTypes.LIMIT
            ).distinct()

    def filter_only_limit_based(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset

        # Get offering IDs that have only limit-based components
        offering_ids = (
            models.Offering.objects.annotate(
                total_components=Count("components"),
                limit_components=Count(
                    "components", filter=Q(components__billing_type=BillingTypes.LIMIT)
                ),
            )
            .filter(total_components__gt=0, total_components=F("limit_components"))
            .values_list("id", flat=True)
        )

        if value:
            # Include only resources that have ONLY limit-based components
            return queryset.filter(offering__id__in=offering_ids)
        else:
            # Filter out resources that have ONLY limit-based components
            return queryset.exclude(offering__id__in=offering_ids)

    def filter_only_usage_based(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset

        # Get offering IDs that have only usage-based components
        offering_ids = (
            models.Offering.objects.annotate(
                total_components=Count("components"),
                usage_components=Count(
                    "components", filter=Q(components__billing_type=BillingTypes.USAGE)
                ),
            )
            .filter(total_components__gt=0, total_components=F("usage_components"))
            .values_list("id", flat=True)
        )

        if value:
            # Include only resources that have ONLY usage-based components
            return queryset.filter(offering__id__in=offering_ids)
        else:
            # Filter out resources that have ONLY usage-based components
            return queryset.exclude(offering__id__in=offering_ids)

    def filter_component_count(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset

        # Get offering IDs that have exactly 'value' number of components
        offering_ids = (
            models.Offering.objects.annotate(component_count=Count("components"))
            .filter(component_count=value)
            .values_list("id", flat=True)
        )

        return queryset.filter(offering__id__in=offering_ids)

    def filter_limit_component_count(self, queryset: ResourceQuerySet, name, value):
        if value is None:
            return queryset

        # Get offering IDs that have exactly 'value' number of limit-based components
        offering_ids = (
            models.Offering.objects.annotate(
                limit_component_count=Count(
                    "components", filter=Q(components__billing_type=BillingTypes.LIMIT)
                )
            )
            .filter(limit_component_count=value)
            .values_list("id", flat=True)
        )

        return queryset.filter(offering__id__in=offering_ids)


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return []

    def get_field_name(self):
        return "scope"


class BaseScopedServiceAccountFilter(django_filters.FilterSet):
    username = django_filters.CharFilter(field_name="username")
    email = django_filters.CharFilter(lookup_expr="icontains")
    state = core_filters.MappedMultipleChoiceFilter(ServiceAccountState.CHOICES)

    class Meta:
        model = models.ScopedServiceAccount
        fields = ["username", "email"]


class CustomerServiceAccountFilter(BaseScopedServiceAccountFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="customer__uuid")

    class Meta(BaseScopedServiceAccountFilter.Meta):
        model = models.CustomerServiceAccount
        fields = BaseScopedServiceAccountFilter.Meta.fields


class ProjectServiceAccountFilter(BaseScopedServiceAccountFilter):
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")

    class Meta(BaseScopedServiceAccountFilter.Meta):
        model = models.ProjectServiceAccount
        fields = BaseScopedServiceAccountFilter.Meta.fields


class RobotAccountFilter(core_filters.CreatedModifiedFilter, django_filters.FilterSet):
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
    state = django_filters.ChoiceFilter(choices=RobotAccountStates.CHOICES)

    class Meta:
        model = models.RobotAccount
        fields = ["type", "state"]


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
    billing_period_year = django_filters.NumberFilter(field_name="billing_period__year")
    billing_period_month = django_filters.NumberFilter(
        field_name="billing_period__month"
    )
    type = django_filters.CharFilter(field_name="component__type")

    o = django_filters.OrderingFilter(
        fields=(
            "billing_period",
            "usage",
        )
    )

    class Meta:
        model = models.ComponentUsage
        fields = ["billing_period"]


class ComponentUserUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="component_usage__resource__uuid",
        label="Resource URL",
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
    username = django_filters.CharFilter(field_name="username", lookup_expr="icontains")
    billing_period_year = django_filters.NumberFilter(
        field_name="component_usage__billing_period__year"
    )
    billing_period_month = django_filters.NumberFilter(
        field_name="component_usage__billing_period__month"
    )
    type = django_filters.CharFilter(field_name="component_usage__component__type")

    o = django_filters.OrderingFilter(
        fields=("component_usage__billing_period", "usage", "username")
    )

    class Meta:
        model = models.ComponentUserUsage
        fields = ["component_usage__billing_period"]


class ComponentUserUsageLimitFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = django_filters.UUIDFilter(field_name="resource__uuid")
    offering_uuid = django_filters.UUIDFilter(field_name="resource__offering__uuid")
    component_type = django_filters.CharFilter(field_name="component__type")
    username = django_filters.CharFilter(field_name="user__username")

    class Meta:
        model = models.ComponentUserUsageLimit
        fields = []


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
        return config.ANONYMOUS_USER_CAN_VIEW_OFFERINGS

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
    provider_uuid = django_filters.UUIDFilter(field_name="offering__customer__uuid")
    is_restricted = django_filters.BooleanFilter(field_name="is_restricted")
    state = core_filters.MappedMultipleChoiceFilter(OfferingUserStates.CHOICES)
    has_consent = django_filters.BooleanFilter(
        method="filter_has_consent",
        label="User Has Consent",
        widget=BooleanWidget,
    )

    o = django_filters.OrderingFilter(fields=("created", "modified", "username"))
    query = django_filters.CharFilter(
        method="filter_query", label="Search by offering name, username or user name"
    )

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

    def filter_has_consent(self, queryset, name, value):
        if value is None:
            return queryset
        if value:
            return queryset.filter(
                user__offering_consents__offering=F("offering"),
                user__offering_consents__revocation_date__isnull=True,
            ).distinct()
        else:
            return queryset.exclude(
                user__offering_consents__offering=F("offering"),
                user__offering_consents__revocation_date__isnull=True,
            ).distinct()


class OfferingUserChecklistCompletionsFilter(core_filters.CreatedModifiedFilter):
    """Filter for checklist completions related to offering users."""

    user_uuid = django_filters.UUIDFilter(
        field_name="scope_object_id",
        method="filter_user_uuid",
        label="Filter by user UUID",
    )
    offering_uuid = django_filters.UUIDFilter(
        method="filter_offering_uuid", label="Filter by offering UUID"
    )
    is_completed = django_filters.BooleanFilter(field_name="is_completed")
    o = django_filters.OrderingFilter(fields=("modified", "is_completed"))

    class Meta:
        model = checklist_models.ChecklistCompletion
        fields = []

    def filter_user_uuid(self, queryset, name, value):
        """Filter completions by the UUID of the OfferingUser's user."""
        if not value:
            return queryset

        # Get content type for OfferingUser
        content_type = ContentType.objects.get_for_model(models.OfferingUser)

        # Get OfferingUser IDs that belong to the specified user
        offering_user_ids = models.OfferingUser.objects.filter(
            user__uuid=value
        ).values_list("id", flat=True)

        return queryset.filter(
            scope_content_type=content_type, scope_object_id__in=offering_user_ids
        )

    def filter_offering_uuid(self, queryset, name, value):
        """Filter completions by offering UUID."""
        if not value:
            return queryset

        # Get content type for OfferingUser
        content_type = ContentType.objects.get_for_model(models.OfferingUser)

        # Get OfferingUser IDs that belong to the specified offering
        offering_user_ids = models.OfferingUser.objects.filter(
            offering__uuid=value
        ).values_list("id", flat=True)

        return queryset.filter(
            scope_content_type=content_type, scope_object_id__in=offering_user_ids
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
        choices=OfferingStates.CHOICES,
        label="Customers offerings state",
        method="filter_customers_offerings_state",
    )

    has_shared = django_filters.BooleanFilter(
        method="filter_has_shared", label="Has shared"
    )

    offering_name = django_filters.CharFilter(
        field_name="offerings__name", lookup_expr="icontains"
    )

    resource_customer_uuid = django_filters.UUIDFilter(
        method="filter_resource_customer_uuid"
    )
    resource_project_uuid = django_filters.UUIDFilter(
        method="filter_resource_project_uuid"
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

    def filter_resource_customer_uuid(self, queryset, name, value):
        valid_ids = (
            models.Resource.objects.filter(project__customer__uuid=value)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("offering__category_id", flat=True)
            .distinct()
        )
        return queryset.filter(id__in=valid_ids)

    def filter_resource_project_uuid(self, queryset, name, value):
        valid_ids = (
            models.Resource.objects.filter(project__uuid=value)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("offering__category_id", flat=True)
            .distinct()
        )
        return queryset.filter(id__in=valid_ids)


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


class IntegrationStatusFilter(OfferingFilterMixin, django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=["last_request_timestamp"])
    agent_type = django_filters.CharFilter(field_name="agent_type")
    status = core_filters.MappedMultipleChoiceFilter(
        models.IntegrationStatus.States.CHOICES
    )
    customer_uuid = django_filters.CharFilter(field_name="offering__customer__uuid")

    class Meta:
        model = models.IntegrationStatus
        fields = []


class ProviderPlanFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user

        if user.is_staff:
            return queryset

        customer_ids = get_connected_customers(user)
        return queryset.filter(offering__customer_id__in=customer_ids)


class BackendResourceFilter(
    core_filters.CreatedModifiedFilter,
    structure_filters.NameFilterSet,
    django_filters.FilterSet,
):
    o = django_filters.OrderingFilter(fields=("created",))
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    backend_id = django_filters.CharFilter(
        field_name="backend_id", lookup_expr="exact", label="Backend ID"
    )

    class Meta:
        model = models.BackendResource
        fields = []


class BackendResourceRequestFilter(
    core_filters.CreatedModifiedFilter,
    django_filters.FilterSet,
):
    o = django_filters.OrderingFilter(fields=("created",))
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    started = django_filters.DateTimeFilter(lookup_expr="gte", label="Created after")
    finished = django_filters.DateTimeFilter(lookup_expr="gte", label="Modified after")
    state = core_filters.MappedMultipleChoiceFilter(
        models.BackendResourceRequest.States.CHOICES
    )

    class Meta:
        models = models.BackendResourceRequest
        fields = []


class MaintenanceAnnouncementTemplateFilter(django_filters.FilterSet):
    service_provider_uuid = django_filters.UUIDFilter(
        field_name="service_provider__uuid"
    )
    maintenance_type = django_filters.NumberFilter(field_name="maintenance_type")
    o = django_filters.OrderingFilter(fields=("created", "name"))

    class Meta:
        model = models.MaintenanceAnnouncementTemplate
        fields = []


class MaintenanceAnnouncementFilter(django_filters.FilterSet):
    service_provider_uuid = django_filters.UUIDFilter(
        field_name="service_provider__uuid"
    )
    maintenance_type = django_filters.NumberFilter(field_name="maintenance_type")
    state = core_filters.MappedMultipleChoiceFilter(models.MaintenanceState.CHOICES)
    scheduled_start_after = django_filters.DateTimeFilter(
        field_name="scheduled_start", lookup_expr="gte"
    )
    scheduled_start_before = django_filters.DateTimeFilter(
        field_name="scheduled_start", lookup_expr="lte"
    )
    scheduled_end_after = django_filters.DateTimeFilter(
        field_name="scheduled_end", lookup_expr="gte"
    )
    scheduled_end_before = django_filters.DateTimeFilter(
        field_name="scheduled_end", lookup_expr="lte"
    )
    o = django_filters.OrderingFilter(
        fields=("created", "name", "scheduled_start", "scheduled_end")
    )

    class Meta:
        model = models.MaintenanceAnnouncement
        fields = []


class MaintenanceAnnouncementOfferingTemplateFilter(django_filters.FilterSet):
    maintenance_template_uuid = django_filters.UUIDFilter(
        field_name="maintenance_template__uuid"
    )
    service_provider_uuid = django_filters.UUIDFilter(
        field_name="maintenance_template__service_provider__uuid"
    )
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    impact_level = django_filters.NumberFilter(field_name="impact_level")
    o = django_filters.OrderingFilter(fields=("created",))

    class Meta:
        model = models.MaintenanceAnnouncementOfferingTemplate
        fields = []


def user_extra_query(user):
    customer_ids = get_connected_customers(
        user, (RoleEnum.CUSTOMER_OWNER, RoleEnum.CUSTOMER_MANAGER)
    )
    offering_ids = models.Offering.objects.filter(
        shared=True, customer_id__in=customer_ids
    ).values_list("id", flat=True)

    project_ids = (
        models.Resource.objects.filter(offering_id__in=offering_ids)
        .exclude(state=ResourceStates.TERMINATED)
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


class UserOfferingConsentFilter(django_filters.FilterSet):
    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    version = django_filters.CharFilter(field_name="version")
    has_consent = django_filters.BooleanFilter(method="filter_has_consent")
    requires_reconsent = django_filters.BooleanFilter(
        method="filter_requires_reconsent"
    )

    def filter_has_consent(self, queryset, name, value):
        if value:
            return queryset.filter(revocation_date__isnull=True)
        else:
            return queryset.exclude(revocation_date__isnull=True)

    def filter_requires_reconsent(self, queryset, name, value):
        if value:
            return queryset.filter(
                revocation_date__isnull=True,
                offering__terms_of_service_configs__is_active=True,
                offering__terms_of_service_configs__requires_reconsent=True,
            )
        else:
            return queryset.exclude(
                revocation_date__isnull=True,
                offering__terms_of_service_configs__is_active=True,
                offering__terms_of_service_configs__requires_reconsent=True,
            )

    o = django_filters.OrderingFilter(
        fields=(
            "agreement_date",
            "revocation_date",
            "created",
            "modified",
        )
    )

    class Meta:
        model = models.UserOfferingConsent
        fields = []


class OfferingTermsOfServiceFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    is_active = django_filters.BooleanFilter(field_name="is_active")
    version = django_filters.CharFilter(field_name="version")
    requires_reconsent = django_filters.BooleanFilter(field_name="requires_reconsent")

    o = django_filters.OrderingFilter(
        fields=(
            "created",
            "modified",
            "version",
        )
    )

    class Meta:
        model = models.OfferingTermsOfService
        fields = [
            "offering",
            "offering_uuid",
            "is_active",
            "version",
            "requires_reconsent",
        ]


class CourseAccountFilter(django_filters.FilterSet):
    username = django_filters.CharFilter(field_name="user__username")
    email = django_filters.CharFilter(lookup_expr="icontains")
    state = core_filters.MappedMultipleChoiceFilter(CourseAccountState.choices)
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    project_start_date = DateFromToRangeFilter(field_name="project__start_date")
    project_end_date = DateFromToRangeFilter(field_name="project__end_date")
    o = django_filters.OrderingFilter(
        fields=(
            "created",
            "modified",
            "state",
            "email",
            ("user__username", "username"),
            ("project__name", "project_name"),
            ("project__start_date", "project_start_date"),
            ("project__end_date", "project_end_date"),
        )
    )

    class Meta:
        model = models.CourseAccount
        fields = [
            "username",
            "email",
            "state",
            "project_uuid",
            "project_start_date",
            "project_end_date",
            "o",
        ]


class OfferingPartitionFilter(django_filters.FilterSet):
    """Filter for OfferingPartition model."""

    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    offering_name = django_filters.CharFilter(
        field_name="offering__name", lookup_expr="icontains"
    )
    partition_name = django_filters.CharFilter(lookup_expr="icontains")
    qos = django_filters.CharFilter(lookup_expr="icontains")
    priority_tier = django_filters.NumberFilter()
    exclusive_user = django_filters.BooleanFilter()
    exclusive_topo = django_filters.BooleanFilter()
    req_resv = django_filters.BooleanFilter()

    # Resource limit filters
    max_cpus_per_node = django_filters.NumberFilter()
    max_nodes = django_filters.NumberFilter()
    min_nodes = django_filters.NumberFilter()
    max_time = django_filters.NumberFilter()
    default_time = django_filters.NumberFilter()

    o = django_filters.OrderingFilter(
        fields=(
            ("partition_name", "partition_name"),
            ("offering__name", "offering_name"),
            ("priority_tier", "priority_tier"),
            ("max_nodes", "max_nodes"),
            ("max_time", "max_time"),
            ("created", "created"),
            ("modified", "modified"),
        ),
        field_labels={
            "partition_name": "Partition Name",
            "offering_name": "Offering Name",
            "priority_tier": "Priority Tier",
            "max_nodes": "Max Nodes",
            "max_time": "Max Time",
            "created": "Created Date",
            "modified": "Modified Date",
        },
    )

    class Meta:
        model = models.OfferingPartition
        fields = [
            "offering_uuid",
            "offering_name",
            "partition_name",
            "qos",
            "priority_tier",
            "exclusive_user",
            "exclusive_topo",
            "req_resv",
        ]
