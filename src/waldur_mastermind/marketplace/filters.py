import json

import django_filters
from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    QuerySet,
)
from django.utils.translation import gettext_lazy as _
from django_filters import DateFromToRangeFilter
from django_filters.widgets import BooleanWidget
from drf_spectacular.plumbing import build_parameter_type
from drf_spectacular.utils import OpenApiParameter
from rest_framework import exceptions as rf_exceptions
from rest_framework.filters import BaseFilterBackend

from waldur_core.checklist import models as checklist_models
from waldur_core.core import filters as core_filters
from waldur_core.core.enums import CoreStates
from waldur_core.core.filters import (
    CharInFilter,
    LooseMultipleChoiceFilter,
    ReviewStateFilter,
    get_generic_field_filter,
)
from waldur_core.core.models import User
from waldur_core.core.utils import get_ip_address, is_uuid_like
from waldur_core.permissions import models as permission_models
from waldur_core.permissions.enums import TYPE_MAP, PermissionEnum, RoleEnum
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
    OfferingUserRuntimeStates,
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
from waldur_openstack import models as openstack_models
from waldur_pid import models as pid_models

from . import models, utils


class ServiceProviderFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        label="Customer URL",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )
    customer_keyword = django_filters.CharFilter(
        method="filter_customer_keyword",
        label="Customer keyword (name, abbreviation or native name)",
    )
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


class TagFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    created_by = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="created_by__uuid"
    )

    class Meta:
        model = models.Tag
        fields = ["name"]


class OfferingFilter(
    core_filters.CreatedModifiedFilter,
    structure_filters.NameFilterSet,
    django_filters.FilterSet,
):
    class Meta:
        model = models.Offering
        fields = []

    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        label="Customer URL",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )
    allowed_customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_allowed_customer",
        label="Allowed customer UUID",
    )
    service_manager_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        method="filter_service_manager",
        label="Service manager UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", method="filter_project", label="Project UUID"
    )
    parent_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="parent__uuid",
        label="Parent offering UUID",
    )
    attributes = django_filters.CharFilter(
        method="filter_attributes", label="Offering attributes (JSON)"
    )
    state = core_filters.MappedMultipleChoiceFilter(
        OfferingStates.CHOICES, label="Offering state"
    )
    organization_group_uuid = core_filters.RelatedUUIDFilter(
        view_name="organization-group-detail",
        field_name="organization_groups__uuid",
        label="Organization group UUID",
    )
    tag = LooseMultipleChoiceFilter(
        field_name="tags__uuid",
        label="Tag UUID (OR logic)",
    )
    tags_and = django_filters.CharFilter(
        method="filter_tags_and",
        label="Tag UUIDs with AND logic (comma-separated)",
    )
    tag_name = LooseMultipleChoiceFilter(
        field_name="tags__name",
        label="Tag name (OR logic)",
    )
    tag_names_and = django_filters.CharFilter(
        method="filter_tag_names_and",
        label="Tag names with AND logic (comma-separated)",
    )
    category_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-detail",
        field_name="category__uuid",
        label="Category UUID",
    )
    category_group_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-group-detail",
        field_name="category__group__uuid",
        label="Category group UUID",
    )
    offering_group_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-offering-group-detail",
        field_name="offering_group__uuid",
        label="Offering group UUID",
    )
    billable = django_filters.BooleanFilter(widget=BooleanWidget, label="Billable")
    shared = django_filters.BooleanFilter(widget=BooleanWidget, label="Shared")
    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description contains"
    )
    keyword = django_filters.CharFilter(method="filter_keyword", label="Keyword")
    scope_uuid = core_filters.RelatedUUIDFilter(
        view_name="servicesettings-detail",
        method=get_generic_field_filter(
            models_to_search=[structure_models.ServiceSettings]
        ),
        label="Scope UUID",
    )
    accessible_via_calls = django_filters.BooleanFilter(
        label="Accessible via calls", method="filter_accessible_via_calls"
    )
    accessible = django_filters.BooleanFilter(
        label="Only offerings the current user can order",
        method="filter_accessible",
    )
    resource_customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_resource_customer_uuid",
        label="Resource customer UUID",
    )
    resource_project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        method="filter_resource_project_uuid",
        label="Resource project UUID",
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
    can_create_offering_user = django_filters.BooleanFilter(
        field_name="plugin_options__service_provider_can_create_offering_user"
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
    type = LooseMultipleChoiceFilter(label="Offering type")

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

    def filter_accessible(self, queryset, name, value):
        # When True, hide restricted offerings the current user cannot order
        # (e.g. plugin_options.restricted_to_roles the user does not hold), even
        # if their project already consumes a resource from one. The catalog
        # passes this so non-orderable offerings do not clutter the marketplace,
        # while detail/retrieve endpoints keep resolving them.
        if not value:
            return queryset
        return queryset.filter_accessible_for_user(self.request.user)

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
        active_consent = models.UserOfferingConsent.objects.filter(
            offering=OuterRef("pk"),
            user=user,
            revocation_date__isnull=True,
        )
        if value:
            return queryset.filter(Exists(active_consent)).distinct()
        else:
            return queryset.exclude(Exists(active_consent)).distinct()

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

    def filter_tags_and(self, queryset, name, value):
        """
        Filter offerings that have ALL specified tags (AND logic).
        Accepts comma-separated tag UUIDs.
        """
        if not value:
            return queryset

        uuids = [u.strip() for u in value.split(",") if u.strip()]

        if not uuids:
            return queryset

        # Filter offerings that have all specified tags
        for tag_uuid in uuids:
            queryset = queryset.filter(tags__uuid=tag_uuid)

        return queryset.distinct()

    def filter_tag_names_and(self, queryset, name, value):
        """
        Filter offerings that have ALL specified tag names (AND logic).
        Accepts comma-separated tag names (exact match).
        """
        if not value:
            return queryset

        names = [n.strip() for n in value.split(",") if n.strip()]

        if not names:
            return queryset

        # Filter offerings that have all specified tags by name
        for tag_name in names:
            queryset = queryset.filter(tags__name__iexact=tag_name)

        return queryset.distinct()


class OfferingCustomersFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        return queryset.filter_for_user(request.user)


class ResourceAccessSubnetConcealmentFilterBackend(BaseFilterBackend):
    """Hide resources whose offering opted into subnet-based concealment when the
    caller's IP is not covered by the resource's access subnets.

    Mirrors the organization-level ``filter_queryset_by_user_ip`` semantics:
    staff/support and requests without a resolvable IP bypass the check. A
    resource is hidden only when its offering enabled
    ``conceal_subnet_restricted_resources`` AND it is restricted (it has at least
    one own subnet, or its offering has at least one provider-default subnet) AND
    the caller's IP is in none of the resource's own subnets nor the offering's
    default subnets. The provider defaults widen the allow-list.
    """

    FLAG = "conceal_subnet_restricted_resources"

    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user is None or not user.is_authenticated:
            return queryset
        user_ip = get_ip_address(request)
        if user.is_staff or user.is_support or not user_ip:
            return queryset

        concealing = {
            "offering__plugin_options__has_key": self.FLAG,
            f"offering__plugin_options__{self.FLAG}": True,
        }
        # Resources restricted because they have their own subnet(s).
        restricted_own = models.ResourceAccessSubnet.objects.filter(
            **{f"resource__{k}": v for k, v in concealing.items()},
            inet__isnull=False,
        ).values_list("resource_id", flat=True)
        # Concealing offerings that carry provider-default subnets: every resource
        # of such an offering is restricted (checked against the defaults).
        offerings_with_defaults = models.OfferingAccessSubnet.objects.filter(
            **concealing,
            inet__isnull=False,
        ).values_list("offering_id", flat=True)

        # Resources allowed because one of their own subnets covers the IP.
        allowed_own = models.ResourceAccessSubnet.objects.filter(
            inet__net_contains_or_equals=user_ip,
        ).values_list("resource_id", flat=True)
        # Offerings whose provider-default subnets cover the IP.
        offerings_allowing_ip = models.OfferingAccessSubnet.objects.filter(
            inet__net_contains_or_equals=user_ip,
        ).values_list("offering_id", flat=True)

        restricted = Q(pk__in=restricted_own) | Q(
            offering_id__in=offerings_with_defaults
        )
        allowed = Q(pk__in=allowed_own) | Q(offering_id__in=offerings_allowing_ip)
        return queryset.exclude(restricted & ~allowed)


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

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="importable",
                schema={"type": "string"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by importable offerings.",
            )
        ]


class OfferingFilterMixin(django_filters.FilterSet):
    """Mixin to provide common offering-related filters."""

    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
    )
    offering_uuid = core_filters.RelatedUUIDInFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    offering_slug = CharInFilter(field_name="offering__slug")
    parent_offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__parent__uuid",
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


class OfferingRoleFilter(django_filters.FilterSet):
    offering_uuid = django_filters.CharFilter(method="filter_by_offering")
    content_type = django_filters.CharFilter(method="filter_by_content_type")
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = permission_models.Role
        fields = []

    def filter_by_offering(self, queryset, name, value):
        try:
            offering = models.Offering.objects.get(uuid=value)
        except models.Offering.DoesNotExist:
            return queryset.none()
        if offering.profile_id:
            # Profile-bound offerings own their catalog through the profile;
            # any direct RoleAvailability rows are ignored to avoid stale
            # bindings from leaking into the per-offering Roles tab.
            return queryset.filter(
                id__in=offering.profile.roles.values_list("id", flat=True)
            )
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        return queryset.filter(
            availability__content_type=offering_ct,
            availability__object_id=offering.id,
        )

    def filter_by_content_type(self, queryset, name, value):
        if value in TYPE_MAP:
            app_label, model_name = TYPE_MAP[value]
            ct = ContentType.objects.get_by_natural_key(app_label, model_name)
            return queryset.filter(content_type=ct)
        return queryset.none()


class OfferingPermissionFilter(UserPermissionFilter):
    class Meta:
        fields = []
        model = UserRole

    offering = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", method="filter_by_offering"
    )
    customer = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_by_customer"
    )

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
    catalog_type = django_filters.ChoiceFilter(
        choices=models.SoftwareCatalog.CATALOG_TYPE_CHOICES,
        label="Catalog type",
        help_text="Filter by catalog type (binary_runtime, source_package, package_manager)",
    )
    description = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Description",
        help_text="Filter catalogs by description (case-insensitive partial match)",
    )
    auto_update_enabled = django_filters.BooleanFilter(
        widget=BooleanWidget,
        label="Auto-update enabled",
        help_text="Filter catalogs by auto-update status",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("version", "version"),
            ("catalog_type", "catalog_type"),
            ("created", "created"),
            ("modified", "modified"),
        ),
        field_labels={
            "name": "Catalog name",
            "version": "Version",
            "catalog_type": "Catalog type",
            "created": "Created date",
            "modified": "Modified date",
        },
    )

    class Meta:
        model = models.SoftwareCatalog
        fields = ["name", "version", "catalog_type"]


class SoftwarePackageFilter(django_filters.FilterSet):
    """Filter for SoftwarePackage model."""

    query = django_filters.CharFilter(
        method="filter_query",
        label="query",
        help_text="Query packages by name, description, or version (case-insensitive partial match)",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        method="filter_offering_uuid",
        label="Offering UUID",
        help_text="Filter packages available for a specific offering",
    )
    catalog_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-group-detail",
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
    name_exact = django_filters.CharFilter(
        field_name="name",
        lookup_expr="iexact",
        label="Package name (exact)",
        help_text="Filter packages by exact name (case-insensitive)",
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
    extension_type = django_filters.CharFilter(
        method="filter_by_extension_type",
        label="Extension type",
        help_text="Filter packages having extensions of a specific type (e.g., 'python')",
    )
    extension_name = django_filters.CharFilter(
        method="filter_by_extension_name",
        label="Extension name",
        help_text="Filter packages having extensions with a specific name",
    )
    is_extension = django_filters.BooleanFilter(
        widget=BooleanWidget,
        label="Is extension",
        help_text="Filter packages that are extensions of other packages",
    )
    parent_software_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-software-package-detail",
        field_name="parent_softwares__uuid",
        label="Parent software UUID",
        help_text="Filter extension packages belonging to a specific parent package",
    )
    category = django_filters.CharFilter(
        method="filter_category",
        label="Category",
        help_text="Filter packages by category (e.g., bio, hpc, chemistry)",
    )
    license = django_filters.CharFilter(
        method="filter_license",
        label="License",
        help_text="Filter packages by license (e.g., GPL-3.0, MIT)",
    )
    catalog_type = django_filters.ChoiceFilter(
        choices=models.SoftwareCatalog.CATALOG_TYPE_CHOICES,
        field_name="catalog__catalog_type",
        label="Catalog type",
        help_text="Filter packages by catalog type (binary_runtime, source_package, package_manager)",
    )
    toolchain_families_compatibility = django_filters.CharFilter(
        method="filter_toolchain_families_compatibility",
        label="Toolchain families compatibility",
        help_text="Filter packages compatible with a specific toolchain family (e.g., foss_2022b)",
    )
    toolchain_name = django_filters.CharFilter(
        method="filter_toolchain_name",
        label="Toolchain name",
        help_text="Filter packages by toolchain name (e.g., foss, gfbf)",
    )
    has_gpu = django_filters.BooleanFilter(
        method="filter_has_gpu",
        widget=BooleanWidget,
        label="Has GPU support",
        help_text="Filter packages that have GPU-enabled builds",
    )
    gpu_arch = django_filters.CharFilter(
        method="filter_gpu_arch",
        label="GPU architecture",
        help_text="Filter packages by GPU architecture (e.g., nvidia/cc90)",
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
        return queryset.filter(
            versions__targets__target_type="cpu_architecture",
            versions__targets__target_name=value,
        ).distinct()

    def filter_cpu_microarchitecture(self, queryset, name, value):
        """Filter packages with versions available for CPU microarchitecture."""
        return queryset.filter(
            versions__targets__target_type="cpu_architecture",
            versions__targets__target_subtype=value,
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

    def filter_by_extension_type(self, queryset, name, value):
        """Filter packages having extensions of a specific type (e.g., 'python')."""
        return queryset.filter(
            versions__metadata__extensions__contains=[{"type": value}]
        ).distinct()

    def filter_by_extension_name(self, queryset, name, value):
        """Filter packages having extensions with a specific name."""
        return queryset.filter(
            versions__metadata__extensions__contains=[{"name": value}]
        ).distinct()

    def filter_toolchain_families_compatibility(self, queryset, name, value):
        """Filter packages with versions compatible with a specific toolchain family."""
        return queryset.filter(
            versions__metadata__toolchain_families_compatibility__contains=[value]
        ).distinct()

    def filter_category(self, queryset, name, value):
        """Filter packages by category."""
        return queryset.filter(categories__contains=[value]).distinct()

    def filter_license(self, queryset, name, value):
        """Filter packages by license."""
        return queryset.filter(licenses__contains=[value]).distinct()

    def filter_toolchain_name(self, queryset, name, value):
        """Filter packages by toolchain name (via version metadata)."""
        return queryset.filter(versions__metadata__toolchain__name=value).distinct()

    def filter_has_gpu(self, queryset, name, value):
        """Filter packages that have at least one GPU-enabled target."""
        # Exists: at least one non-empty gpu_architectures target (not "all targets").
        has_gpu_target = models.SoftwareTarget.objects.filter(
            version__package_id=OuterRef("pk"),
        ).exclude(gpu_architectures=[])
        if value:
            return queryset.filter(Exists(has_gpu_target)).distinct()
        return queryset.exclude(Exists(has_gpu_target)).distinct()

    def filter_gpu_arch(self, queryset, name, value):
        """Filter packages by specific GPU architecture (e.g., nvidia/cc90)."""
        return queryset.filter(
            versions__targets__gpu_architectures__contains=[value]
        ).distinct()


class SoftwareVersionFilter(django_filters.FilterSet):
    """Filter for SoftwareVersion model."""

    package_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-software-package-detail", field_name="package__uuid"
    )
    catalog_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-group-detail",
        field_name="package__catalog__uuid",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", method="filter_offering_uuid"
    )
    package_name = django_filters.CharFilter(
        field_name="package__name", lookup_expr="icontains"
    )
    version = django_filters.CharFilter(lookup_expr="icontains")
    version_exact = django_filters.CharFilter(
        field_name="version",
        lookup_expr="exact",
        label="Version (exact)",
        help_text="Filter versions by exact version string",
    )
    cpu_family = django_filters.CharFilter(method="filter_cpu_family")
    cpu_microarchitecture = django_filters.CharFilter(
        method="filter_cpu_microarchitecture"
    )
    toolchain_families_compatibility = django_filters.CharFilter(
        method="filter_toolchain_families_compatibility",
        label="Toolchain families compatibility",
        help_text="Filter versions compatible with a specific toolchain family (e.g., foss_2022b)",
    )
    toolchain_name = django_filters.CharFilter(
        method="filter_toolchain_name",
        label="Toolchain name",
        help_text="Filter versions by toolchain name (e.g., foss, gfbf)",
    )
    toolchain_version = django_filters.CharFilter(
        method="filter_toolchain_version",
        label="Toolchain version",
        help_text="Filter versions by toolchain version (e.g., 2023b)",
    )
    release_date = DateFromToRangeFilter(
        label="Release date range",
        help_text="Filter versions by release date range (release_date_after, release_date_before)",
    )
    catalog_type = django_filters.ChoiceFilter(
        choices=models.SoftwareCatalog.CATALOG_TYPE_CHOICES,
        field_name="package__catalog__catalog_type",
        label="Catalog type",
        help_text="Filter versions by catalog type (binary_runtime, source_package, package_manager)",
    )
    has_gpu = django_filters.BooleanFilter(
        method="filter_has_gpu",
        widget=BooleanWidget,
        label="Has GPU support",
        help_text="Filter versions that have GPU-enabled builds",
    )
    gpu_arch = django_filters.CharFilter(
        method="filter_gpu_arch",
        label="GPU architecture",
        help_text="Filter versions by GPU architecture (e.g., nvidia/cc90)",
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

    def filter_cpu_family(self, queryset, name, value):
        return queryset.filter(
            targets__target_type="cpu_architecture",
            targets__target_name=value,
        ).distinct()

    def filter_cpu_microarchitecture(self, queryset, name, value):
        return queryset.filter(
            targets__target_type="cpu_architecture",
            targets__target_subtype=value,
        ).distinct()

    def filter_toolchain_families_compatibility(self, queryset, name, value):
        """Filter versions compatible with a specific toolchain family."""
        return queryset.filter(
            metadata__toolchain_families_compatibility__contains=[value]
        )

    def filter_toolchain_name(self, queryset, name, value):
        """Filter versions by toolchain name."""
        return queryset.filter(metadata__toolchain__name=value)

    def filter_toolchain_version(self, queryset, name, value):
        """Filter versions by toolchain version."""
        return queryset.filter(metadata__toolchain__version=value)

    def filter_has_gpu(self, queryset, name, value):
        """Filter versions that have at least one GPU-enabled target."""
        has_gpu_target = models.SoftwareTarget.objects.filter(
            version_id=OuterRef("pk"),
        ).exclude(gpu_architectures=[])
        if value:
            return queryset.filter(Exists(has_gpu_target)).distinct()
        return queryset.exclude(Exists(has_gpu_target)).distinct()

    def filter_gpu_arch(self, queryset, name, value):
        """Filter versions by specific GPU architecture (e.g., nvidia/cc90)."""
        return queryset.filter(targets__gpu_architectures__contains=[value]).distinct()


class SoftwareTargetFilter(django_filters.FilterSet):
    """Filter for SoftwareTarget model."""

    version_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-software-version-detail", field_name="version__uuid"
    )
    package_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-software-package-detail",
        field_name="version__package__uuid",
    )
    catalog_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-group-detail",
        field_name="version__package__catalog__uuid",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", method="filter_offering_uuid"
    )
    cpu_family = django_filters.CharFilter(method="filter_cpu_family")
    cpu_microarchitecture = django_filters.CharFilter(
        method="filter_cpu_microarchitecture"
    )
    path = django_filters.CharFilter(
        field_name="location",
        lookup_expr="icontains",
        label="Path",
        help_text="Filter targets by location/path (case-insensitive partial match)",
    )
    target_type = django_filters.CharFilter(
        lookup_expr="iexact",
        label="Target type",
        help_text="Filter targets by type (e.g., architecture, platform, variant)",
    )
    target_name = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Target name",
        help_text="Filter targets by name (e.g., x86_64, aarch64)",
    )
    target_subtype = django_filters.CharFilter(
        lookup_expr="icontains",
        label="Target subtype",
        help_text="Filter targets by subtype (e.g., microarchitecture, distribution)",
    )
    has_gpu = django_filters.BooleanFilter(
        method="filter_has_gpu",
        widget=BooleanWidget,
        label="Has GPU support",
        help_text="Filter targets that have GPU architectures",
    )
    gpu_arch = django_filters.CharFilter(
        method="filter_gpu_arch",
        label="GPU architecture",
        help_text="Filter targets by GPU architecture (e.g., nvidia/cc90)",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("target_name", "cpu_family"),
            ("target_subtype", "cpu_microarchitecture"),
            ("version__package__name", "package_name"),
            ("target_type", "target_type"),
            ("target_name", "target_name"),
            ("created", "created"),
        ),
        field_labels={
            "cpu_family": "CPU Family",
            "cpu_microarchitecture": "CPU Microarchitecture",
            "package_name": "Package name",
            "target_type": "Target type",
            "target_name": "Target name",
            "created": "Created date",
        },
    )

    class Meta:
        model = models.SoftwareTarget
        fields = ["cpu_family", "cpu_microarchitecture", "target_type", "target_name"]

    def filter_offering_uuid(self, queryset, name, value):
        return queryset.filter(
            version__package__catalog__offerings__offering__uuid=value
        ).distinct()

    def filter_cpu_family(self, queryset, name, value):
        return queryset.filter(
            target_type="cpu_architecture",
            target_name__iexact=value,
        )

    def filter_cpu_microarchitecture(self, queryset, name, value):
        return queryset.filter(
            target_type="cpu_architecture",
            target_subtype__iexact=value,
        )

    def filter_has_gpu(self, queryset, name, value):
        """Filter targets that have GPU architectures."""
        if value:
            return queryset.exclude(gpu_architectures=[])
        return queryset.filter(gpu_architectures=[])

    def filter_gpu_arch(self, queryset, name, value):
        """Filter targets by specific GPU architecture (e.g., nvidia/cc90)."""
        return queryset.filter(gpu_architectures__contains=[value])


class OfferingSoftwareCatalogFilter(django_filters.FilterSet):
    """Filter for OfferingSoftwareCatalog model."""

    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    catalog_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-group-detail", field_name="catalog__uuid"
    )
    catalog_name = django_filters.CharFilter(
        field_name="catalog__name", lookup_expr="icontains"
    )
    offering_name = django_filters.CharFilter(
        field_name="offering__name", lookup_expr="icontains"
    )
    partition_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-software-partition-detail", field_name="partition__uuid"
    )
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
    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by order UUID, slug, project name or resource name",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    offering_type = core_filters.LooseMultipleChoiceFilter(
        field_name="offering__type", lookup_expr="exact", label="Offering type"
    )
    category_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-detail",
        field_name="offering__category__uuid",
        label="Category UUID",
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="offering__customer__uuid",
        label="Provider UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    service_manager_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        method="filter_service_manager",
        label="Service manager UUID",
    )
    state = core_filters.MappedMultipleChoiceFilter(
        OrderStates.CHOICES, label="Order state"
    )
    type = core_filters.MappedMultipleChoiceFilter(
        OrderTypes.CHOICES, label="Order type"
    )
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    resource_name = django_filters.CharFilter(
        field_name="resource__name", lookup_expr="exact", label="Resource name"
    )
    can_approve_as_consumer = django_filters.BooleanFilter(
        method="filter_can_approve_as_consumer",
        label="Can approve as consumer",
    )
    can_approve_as_provider = django_filters.BooleanFilter(
        method="filter_can_approve_as_provider",
        label="Can approve as provider",
    )
    was_auto_approved = django_filters.BooleanFilter(
        method="filter_was_auto_approved",
        label="Auto-approved",
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

    def filter_was_auto_approved(self, queryset, name, value):
        if value:
            return queryset.filter(auto_approved_by_rule__isnull=False)
        return queryset.filter(auto_approved_by_rule__isnull=True)


class ProjectOrderAutoApprovalFilter(django_filters.FilterSet):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="project__uuid",
        label="Project UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    enabled = django_filters.BooleanFilter()

    class Meta:
        model = models.ProjectOrderAutoApproval
        fields = []


class ResourceFilter(
    OfferingFilterMixin,
    structure_filters.NameFilterSet,
    core_filters.CreatedModifiedFilter,
):
    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by resource UUID, name, slug, backend ID, effective ID, IPs or hypervisor",
    )

    offering_type = django_filters.CharFilter(
        field_name="offering__type", label="Offering type"
    )
    offering_billable = django_filters.BooleanFilter(
        field_name="offering__billable", label="Offering billable"
    )
    plan_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-plan-detail", field_name="plan__uuid", label="Plan UUID"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    project_name = django_filters.CharFilter(
        field_name="project__name", label="Project name"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer URL",
    )
    service_manager_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        method="filter_service_manager",
        label="Service manager UUID",
    )
    category_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-detail",
        field_name="offering__category__uuid",
        label="Category UUID",
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="offering__customer__uuid",
        label="Provider UUID",
    )
    backend_id = django_filters.CharFilter(label="Backend ID")
    state = core_filters.MappedMultipleChoiceFilter(
        ResourceStates.CHOICES, label="Resource state"
    )
    runtime_state = django_filters.CharFilter(
        field_name="backend_metadata__runtime_state", label="Runtime state"
    )
    flavor_name = django_filters.CharFilter(
        field_name="backend_metadata__flavor_name",
        lookup_expr="icontains",
        label="Flavor name",
    )
    image_name = django_filters.CharFilter(
        field_name="backend_metadata__image_name",
        lookup_expr="icontains",
        label="Image name",
    )
    downscaled = django_filters.BooleanFilter(
        field_name="downscaled", label="Downscaled"
    )
    restrict_member_access = django_filters.BooleanFilter(
        field_name="restrict_member_access", label="Restrict member access"
    )
    paused = django_filters.BooleanFilter(field_name="paused", label="Paused")
    order_state = core_filters.MappedMultipleChoiceFilter(
        choices=OrderStates.CHOICES,
        field_name="order__state",
        label="Order state",
    )
    visible_to_providers = django_filters.BooleanFilter(
        method="filter_visible_to_providers",
        label="Include only resources visible to service providers",
    )
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
    is_attached = django_filters.BooleanFilter(
        method="filter_is_attached",
        label="Filter by attached state",
    )
    resource_attributes = django_filters.CharFilter(
        method="filter_resource_attributes",
        label="Resource attributes (JSON)",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("created", "created"),
            ("project__name", "project_name"),
            ("state", "state"),
            ("end_date", "end_date"),
        )
    )

    class Meta:
        model = models.Resource
        fields = []

    def filter_has_termination_date(self, queryset: ResourceQuerySet, name, value):
        return queryset.exclude(end_date__isnull=value)

    def filter_resource_attributes(self, queryset, name, value):
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
                queryset = queryset.filter(**{f"attributes__{k}__in": v})
            else:
                queryset = queryset.filter(attributes__contains={k: v})
        return queryset

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

    def filter_is_attached(self, queryset: ResourceQuerySet, name, value):
        if value:
            return queryset.filter(backend_metadata__has_key="instance_name")
        return queryset.exclude(backend_metadata__has_key="instance_name")

    def filter_visible_to_providers(self, queryset, name, value):
        if value:
            return queryset.exclude(
                Q(state=ResourceStates.CREATING)
                & Q(
                    order__state__in=[
                        OrderStates.PENDING_CONSUMER,
                        OrderStates.PENDING_PROJECT,
                        OrderStates.PENDING_START_DATE,
                    ]
                )
            )
        return queryset


class ResourceAccessSubnetFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="resource__offering__uuid",
        label="Offering UUID",
    )
    inet = django_filters.CharFilter(lookup_expr="icontains", label="Inet")
    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description"
    )

    class Meta:
        model = models.ResourceAccessSubnet
        fields = [
            "resource",
            "resource_uuid",
            "offering_uuid",
            "inet",
            "description",
        ]


class OfferingAccessSubnetFilter(django_filters.FilterSet):
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering URL",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    inet = django_filters.CharFilter(lookup_expr="icontains", label="Inet")
    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description"
    )

    class Meta:
        model = models.OfferingAccessSubnet
        fields = [
            "offering",
            "offering_uuid",
            "inet",
            "description",
        ]


class ResourceScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return []

    def get_field_name(self):
        return "scope"


class BaseScopedServiceAccountFilter(django_filters.FilterSet):
    username = django_filters.CharFilter(field_name="username", label="Username")
    email = django_filters.CharFilter(lookup_expr="icontains", label="Email contains")
    state = core_filters.MappedMultipleChoiceFilter(
        ServiceAccountState.CHOICES, label="Service account state"
    )

    class Meta:
        model = models.ScopedServiceAccount
        fields = ["username", "email"]


class CustomerServiceAccountFilter(BaseScopedServiceAccountFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        label="Customer URL",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )

    class Meta(BaseScopedServiceAccountFilter.Meta):
        model = models.CustomerServiceAccount
        fields = BaseScopedServiceAccountFilter.Meta.fields


class ProjectServiceAccountFilter(BaseScopedServiceAccountFilter):
    project = core_filters.URLFilter(
        view_name="project-detail",
        field_name="project__uuid",
        label="Project URL",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )

    class Meta(BaseScopedServiceAccountFilter.Meta):
        model = models.ProjectServiceAccount
        fields = BaseScopedServiceAccountFilter.Meta.fields


class RobotAccountFilter(core_filters.CreatedModifiedFilter, django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="resource__project__uuid",
        label="Project UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="resource__project__customer__uuid",
        label="Customer UUID",
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="resource__offering__customer__uuid",
        label="Provider UUID",
    )
    state = django_filters.ChoiceFilter(
        choices=RobotAccountStates.CHOICES, label="Robot account state"
    )
    username = django_filters.CharFilter(
        lookup_expr="icontains", label="Username contains"
    )
    user_email = django_filters.CharFilter(
        field_name="users__email",
        lookup_expr="icontains",
        label="Connected user email contains",
        distinct=True,
    )
    responsible_user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        field_name="responsible_user__uuid",
        label="Responsible user UUID",
    )

    class Meta:
        model = models.RobotAccount
        fields = ["type", "state", "username"]


class ResourceProjectFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.ResourceProject
        fields = []


class PlanFilter(OfferingFilterMixin, django_filters.FilterSet):
    class Meta:
        model = models.Plan
        fields = []

    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )


class ComponentUsageMonthlyFilter(django_filters.FilterSet):
    billing_period = django_filters.DateFilter(
        field_name="billing_period", input_formats=["%Y-%m"]
    )
    start = django_filters.DateFilter(
        field_name="billing_period", lookup_expr="gte", input_formats=["%Y-%m"]
    )
    end = django_filters.DateFilter(
        field_name="billing_period", lookup_expr="lte", input_formats=["%Y-%m"]
    )
    component_type = django_filters.CharFilter(field_name="component__type")
    billing_type = django_filters.CharFilter(field_name="component__billing_type")
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="component__offering__uuid",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="component__offering__customer__uuid",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="component__offering__project__uuid",
    )
    offering_type = django_filters.CharFilter(field_name="component__offering__type")

    class Meta:
        model = models.ComponentUsageMonthly
        fields = []


class CategoryComponentUsageScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return [structure_models.Project, structure_models.Customer]

    def get_field_name(self):
        return "scope"


class CategoryComponentUsageFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryComponentUsage
        fields = []

    date_before = django_filters.DateFilter(
        field_name="date", lookup_expr="lte", label="Date before or equal to"
    )
    date_after = django_filters.DateFilter(
        field_name="date", lookup_expr="gte", label="Date after or equal to"
    )


class ComponentUsageFilter(django_filters.FilterSet):
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource URL",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="resource__offering__uuid",
        label="Offering UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="resource__project__uuid",
        label="Project UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="resource__project__customer__uuid",
        label="Customer UUID",
    )
    date_before = django_filters.DateFilter(
        field_name="date__date", lookup_expr="lte", label="Date before or equal to"
    )
    date_after = django_filters.DateFilter(
        field_name="date__date", lookup_expr="gte", label="Date after or equal to"
    )
    billing_period_year = django_filters.NumberFilter(
        field_name="billing_period__year", label="Billing period year"
    )
    billing_period_month = django_filters.NumberFilter(
        field_name="billing_period__month", label="Billing period month"
    )
    type = django_filters.CharFilter(
        field_name="component__type", label="Component type"
    )

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
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="component_usage__resource__uuid",
        label="Resource UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="component_usage__resource__offering__uuid",
        label="Offering UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="component_usage__resource__project__uuid",
        label="Project UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="component_usage__resource__project__customer__uuid",
        label="Customer UUID",
    )
    date_before = django_filters.DateFilter(
        field_name="component_usage__date__date",
        lookup_expr="lte",
        label="Date before or equal .google/docsto",
    )
    date_after = django_filters.DateFilter(
        field_name="component_usage__date__date",
        lookup_expr="gte",
        label="Date after or equal to",
    )
    username = django_filters.CharFilter(
        field_name="username", lookup_expr="icontains", label="Username contains"
    )
    billing_period_year = django_filters.NumberFilter(
        field_name="component_usage__billing_period__year", label="Billing period year"
    )
    billing_period_month = django_filters.NumberFilter(
        field_name="component_usage__billing_period__month",
        label="Billing period month",
    )
    type = django_filters.CharFilter(
        field_name="component_usage__component__type", label="Component type"
    )

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
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="resource__offering__uuid",
        label="Offering UUID",
    )
    component_type = django_filters.CharFilter(
        field_name="component__type", label="Component type"
    )
    username = django_filters.CharFilter(field_name="user__username", label="Username")

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

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="has_resources",
                schema={"type": "string"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by customers with resources.",
            )
        ]


class ServiceProviderOfferingFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer_uuid = request.query_params.get("service_provider_uuid")

        if customer_uuid and is_uuid_like(customer_uuid):
            customers = models.Resource.objects.filter(
                offering__customer__uuid=customer_uuid
            ).values_list("project__customer_id", flat=True)
            queryset = queryset.filter(pk__in=customers)
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="service_provider_uuid",
                schema={"type": "string", "format": "uuid"},
                location=OpenApiParameter.QUERY,
                description="Filter by service provider UUID.",
                extensions={
                    "x-waldur-operation-id": "marketplace_service_providers_list"
                },
            )
        ]


class CustomerServiceProviderFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        is_service_provider = request.query_params.get("is_service_provider")
        if is_service_provider in ["true", "True"]:
            customers = models.ServiceProvider.objects.values_list(
                "customer_id", flat=True
            )
            return queryset.filter(pk__in=customers)
        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="is_service_provider",
                schema={"type": "boolean"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by customers that are service providers.",
            )
        ]


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

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="is_call_managing_organization",
                schema={"type": "boolean"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by customers that are call managing organizations.",
            )
        ]


class OfferingUserFilter(OfferingFilterMixin, core_filters.CreatedModifiedFilter):
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid", label="User UUID"
    )
    user_username = django_filters.CharFilter(
        field_name="user__username", lookup_expr="iexact", label="User username"
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="offering__customer__uuid",
        label="Provider UUID",
    )
    is_restricted = django_filters.BooleanFilter(
        field_name="is_restricted", label="Is restricted"
    )
    state = core_filters.MappedMultipleChoiceFilter(
        OfferingUserStates.CHOICES, label="Offering user state"
    )
    runtime_state = core_filters.MappedMultipleChoiceFilter(
        OfferingUserRuntimeStates.CHOICES, label="Offering user runtime state"
    )
    has_consent = django_filters.BooleanFilter(
        method="filter_has_consent",
        label="User Has Consent",
        widget=BooleanWidget,
    )
    has_complete_profile = django_filters.BooleanFilter(
        method="filter_has_complete_profile",
        label="User has complete profile for the offering",
        widget=BooleanWidget,
    )
    offering_has_active_tos = django_filters.BooleanFilter(
        method="filter_offering_has_active_tos",
        label="Offering has active Terms of Service",
        widget=BooleanWidget,
    )

    o = django_filters.OrderingFilter(fields=("created", "modified", "username"))
    query = django_filters.CharFilter(
        method="filter_query",
        label="Search by offering name, username, user name, UID or primary GID",
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
            | Q(user__username__icontains=value)
            | Q(backend_metadata__uidnumber__icontains=value)
            | Q(backend_metadata__primarygroup__icontains=value)
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

    def filter_has_complete_profile(self, queryset, name, value):
        if value is None:
            return queryset
        incomplete_q = utils.build_incomplete_profile_q()
        if value:
            return queryset.exclude(incomplete_q).distinct()
        else:
            return queryset.filter(incomplete_q).distinct()

    def filter_offering_has_active_tos(self, queryset, name, value):
        if value is None:
            return queryset

        if value:
            return queryset.filter(
                offering__terms_of_service_configs__is_active=True
            ).distinct()
        else:
            return queryset.exclude(
                offering__terms_of_service_configs__is_active=True
            ).distinct()


class OfferingUserChecklistCompletionsFilter(core_filters.CreatedModifiedFilter):
    """Filter for checklist completions related to offering users."""

    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        field_name="scope_object_id",
        method="filter_user_uuid",
        label="Filter by user UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        method="filter_offering_uuid",
        label="Filter by offering UUID",
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


class OfferingGroupFilter(django_filters.FilterSet):
    class Meta:
        model = models.OfferingGroup
        fields = []

    title = django_filters.CharFilter(lookup_expr="icontains")
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        label="Customer UUID",
    )


class PosixIdPoolFilter(django_filters.FilterSet):
    class Meta:
        model = models.PosixIdPool
        fields = []

    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_customer_uuid",
        label="Customer UUID",
    )
    service_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="service_provider__uuid",
        label="Service provider UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )

    def filter_customer_uuid(self, queryset, name, value):
        return queryset.filter(
            Q(service_provider__customer__uuid=value)
            | Q(offering__customer__uuid=value)
        )


class PosixIdentityFilter(django_filters.FilterSet):
    class Meta:
        model = models.PosixIdentity
        fields = []

    pool_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-posix-id-pool-detail",
        field_name="pool__uuid",
        label="POSIX ID pool UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    is_released = django_filters.BooleanFilter(
        field_name="released_at", lookup_expr="isnull", exclude=True
    )


class CategoryFilter(django_filters.FilterSet):
    class Meta:
        model = models.Category
        fields = []

    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_customer_uuid",
        label="Customer UUID",
    )

    group_uuid = core_filters.RelatedUUIDFilter(
        view_name="organization-group-detail",
        field_name="group__uuid",
        label="Category group UUID",
    )

    title = django_filters.CharFilter(lookup_expr="icontains", label="Title contains")

    customers_offerings_state = django_filters.MultipleChoiceFilter(
        choices=OfferingStates.CHOICES,
        label="Customers offerings state",
        method="filter_customers_offerings_state",
    )

    has_shared = django_filters.BooleanFilter(
        method="filter_has_shared", label="Has shared"
    )

    offering_name = django_filters.CharFilter(
        field_name="offerings__name",
        lookup_expr="icontains",
        label="Offering name contains",
    )

    resource_customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_resource_customer_uuid",
        label="Resource customer UUID",
    )
    resource_project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        method="filter_resource_project_uuid",
        label="Resource project UUID",
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


class AttributeFilter(django_filters.FilterSet):
    class Meta:
        model = models.Attribute
        fields = []

    section = core_filters.URLFilter(
        view_name="marketplace-section-detail",
        field_name="section__key",
        label="Section URL",
        lookup_field="key",
    )


class AttributeOptionFilter(django_filters.FilterSet):
    class Meta:
        model = models.AttributeOption
        fields = []

    attribute = core_filters.URLFilter(
        view_name="marketplace-attribute-detail",
        field_name="attribute__uuid",
        label="Attribute URL",
    )


class CategoryColumnFilter(django_filters.FilterSet):
    class Meta:
        model = models.CategoryColumn
        fields = []

    category_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-category-detail",
        field_name="category__uuid",
        label="Category UUID",
    )
    title = django_filters.CharFilter(lookup_expr="icontains", label="Title contains")


class PlanComponentFilter(django_filters.FilterSet):
    class Meta:
        model = models.PlanComponent
        fields = []

    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="plan__offering__uuid",
        label="Offering UUID",
    )

    plan_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-plan-detail", field_name="plan__uuid", label="Plan UUID"
    )

    shared = django_filters.BooleanFilter(
        widget=BooleanWidget, field_name="plan__offering__shared", label="Shared"
    )

    archived = django_filters.BooleanFilter(
        field_name="plan__archived", label="Archived"
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

    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="invoice__customer__uuid",
        label="Customer UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="resource__offering__uuid",
        label="Offering UUID",
    )
    invoice_month = django_filters.NumberFilter(
        field_name="invoice__month", label="Invoice month"
    )
    invoice_year = django_filters.NumberFilter(
        field_name="invoice__year", label="Invoice year"
    )

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
    agent_type = django_filters.CharFilter(field_name="agent_type", label="Agent type")
    status = core_filters.MappedMultipleChoiceFilter(
        models.IntegrationStatus.States.CHOICES, label="Integration status"
    )
    customer_uuid = django_filters.CharFilter(
        field_name="offering__customer__uuid", label="Customer UUID"
    )

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
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
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
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    started = django_filters.DateTimeFilter(lookup_expr="gte", label="Created after")
    finished = django_filters.DateTimeFilter(lookup_expr="gte", label="Modified after")
    state = core_filters.MappedMultipleChoiceFilter(
        models.BackendResourceRequest.States.CHOICES,
        label="Backend resource request state",
    )

    class Meta:
        models = models.BackendResourceRequest
        fields = []


class MaintenanceAnnouncementTemplateFilter(django_filters.FilterSet):
    service_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="service_provider__uuid",
        label="Service provider UUID",
    )
    maintenance_type = django_filters.NumberFilter(
        field_name="maintenance_type", label="Maintenance type"
    )
    o = django_filters.OrderingFilter(fields=("created", "name"))

    class Meta:
        model = models.MaintenanceAnnouncementTemplate
        fields = []


def annotate_timing_deltas(queryset):
    """Annotate the derived overrun/start deltas used by timing ordering and the
    timing_bucket filter. Applied on demand (only when those params are used) so
    the aggregation queries in maintenance_stats keep their intended GROUP BY."""
    return queryset.annotate(
        overrun_delta=ExpressionWrapper(
            F("actual_end") - F("scheduled_end"), output_field=DurationField()
        ),
        start_delta=ExpressionWrapper(
            F("actual_start") - F("scheduled_start"), output_field=DurationField()
        ),
    )


class MaintenanceOrderingFilter(django_filters.OrderingFilter):
    """Ordering filter that sorts derived timing fields by their queryset
    annotations and always sinks NULL rows (not-yet-completed maintenances)."""

    # ?o param name -> queryset annotation.
    NULLS_LAST_ANNOTATIONS = {
        "overrun_minutes": "overrun_delta",
        "start_delta_minutes": "start_delta",
    }

    def filter(self, qs, value):
        if value and {v.lstrip("-") for v in value} & set(self.NULLS_LAST_ANNOTATIONS):
            qs = annotate_timing_deltas(qs)
        return super().filter(qs, value)

    def get_ordering_value(self, param):
        descending = param.startswith("-")
        name = param[1:] if descending else param
        annotation = self.NULLS_LAST_ANNOTATIONS.get(name)
        if annotation:
            expr = F(annotation)
            return (
                expr.desc(nulls_last=True) if descending else expr.asc(nulls_last=True)
            )
        return super().get_ordering_value(param)


class MaintenanceAnnouncementFilter(django_filters.FilterSet):
    service_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="service_provider__uuid",
        label="Service provider UUID",
    )
    maintenance_type = django_filters.NumberFilter(
        field_name="maintenance_type", label="Maintenance type"
    )
    state = core_filters.MappedMultipleChoiceFilter(
        models.MaintenanceState.CHOICES, label="Maintenance state"
    )
    scheduled_start_after = django_filters.DateTimeFilter(
        field_name="scheduled_start", lookup_expr="gte", label="Scheduled start after"
    )
    scheduled_start_before = django_filters.DateTimeFilter(
        field_name="scheduled_start", lookup_expr="lte", label="Scheduled start before"
    )
    scheduled_end_after = django_filters.DateTimeFilter(
        field_name="scheduled_end", lookup_expr="gte", label="Scheduled end after"
    )
    scheduled_end_before = django_filters.DateTimeFilter(
        field_name="scheduled_end", lookup_expr="lte", label="Scheduled end before"
    )
    o = MaintenanceOrderingFilter(
        fields=(
            "created",
            "name",
            "scheduled_start",
            "scheduled_end",
            "overrun_minutes",
            "start_delta_minutes",
        )
    )
    timing_bucket = django_filters.CharFilter(
        method="filter_timing_bucket",
        label="Timing bucket (comma-separated: on_time, late_start, overrun, early, pending)",
    )

    class Meta:
        model = models.MaintenanceAnnouncement
        fields = []

    def filter_timing_bucket(self, queryset, name, value):
        buckets = [b.strip() for b in value.split(",") if b.strip()]
        if not buckets:
            return queryset
        queryset = annotate_timing_deltas(queryset)
        # Q-expressions mirror MaintenanceAnnouncement.timing_bucket precedence
        # (pending > overrun > late_start > early > on_time), operating on the
        # overrun_delta / start_delta annotations added by the viewset.
        tol = models.MaintenanceAnnouncement.TIMING_TOLERANCE
        started = Q(actual_start__isnull=False)
        ended = Q(actual_end__isnull=False)
        q_overrun = ended & Q(overrun_delta__gt=tol)
        q_late = started & Q(start_delta__gt=tol) & ~q_overrun
        q_early = ended & Q(overrun_delta__lt=-tol) & ~q_overrun & ~q_late
        q_on_time = started & ~q_overrun & ~q_late & ~q_early
        bucket_q = {
            models.MaintenanceTimingBucket.OVERRUN: q_overrun,
            models.MaintenanceTimingBucket.LATE_START: q_late,
            models.MaintenanceTimingBucket.EARLY: q_early,
            models.MaintenanceTimingBucket.ON_TIME: q_on_time,
            models.MaintenanceTimingBucket.PENDING: Q(actual_start__isnull=True),
        }
        combined = Q()
        matched = False
        for bucket in buckets:
            if bucket in bucket_q:
                combined |= bucket_q[bucket]
                matched = True
        if not matched:
            return queryset.none()
        return queryset.filter(combined)


class MaintenanceAnnouncementOfferingTemplateFilter(django_filters.FilterSet):
    maintenance_template_uuid = core_filters.RelatedUUIDFilter(
        view_name="maintenance-announcement-template-detail",
        field_name="maintenance_template__uuid",
        label="Maintenance template UUID",
    )
    service_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="maintenance_template__service_provider__uuid",
        label="Service provider UUID",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    impact_level = django_filters.NumberFilter(
        field_name="impact_level", label="Impact level"
    )
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
    user = core_filters.URLFilter(
        view_name="user-detail", field_name="user__uuid", label="User URL"
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid", label="User UUID"
    )
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering URL",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    version = django_filters.CharFilter(field_name="version", label="Version")
    has_consent = django_filters.BooleanFilter(
        method="filter_has_consent", label="Has consent"
    )
    requires_reconsent = django_filters.BooleanFilter(
        method="filter_requires_reconsent", label="Requires reconsent"
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
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering URL",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    is_active = django_filters.BooleanFilter(field_name="is_active", label="Is active")
    version = django_filters.CharFilter(field_name="version", label="Version")
    requires_reconsent = django_filters.BooleanFilter(
        field_name="requires_reconsent", label="Requires reconsent"
    )

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
    username = django_filters.CharFilter(field_name="user__username", label="Username")
    email = django_filters.CharFilter(lookup_expr="icontains", label="Email contains")
    state = core_filters.MappedMultipleChoiceFilter(
        CourseAccountState.choices, label="Course account state"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    project_start_date = DateFromToRangeFilter(
        field_name="project__start_date", label="Project start date range"
    )
    project_end_date = DateFromToRangeFilter(
        field_name="project__end_date", label="Project end date range"
    )
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

    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
        label="Offering UUID",
    )
    offering_name = django_filters.CharFilter(
        field_name="offering__name",
        lookup_expr="icontains",
        label="Offering name contains",
    )
    partition_name = django_filters.CharFilter(
        lookup_expr="icontains", label="Partition name contains"
    )
    qos = django_filters.CharFilter(lookup_expr="icontains", label="QoS contains")
    priority_tier = django_filters.NumberFilter(label="Priority tier")
    exclusive_user = django_filters.BooleanFilter(label="Exclusive user")
    exclusive_topo = django_filters.BooleanFilter(label="Exclusive topology")
    req_resv = django_filters.BooleanFilter(label="Requires reservation")

    # Architecture filters
    cpu_arch = django_filters.CharFilter(
        lookup_expr="icontains", label="CPU architecture"
    )
    gpu_arch = django_filters.CharFilter(
        lookup_expr="icontains", label="GPU architecture"
    )
    has_gpu = django_filters.BooleanFilter(
        method="filter_has_gpu",
        widget=BooleanWidget,
        label="Has GPU",
        help_text="Filter partitions that have GPU architecture",
    )

    # Resource limit filters
    max_cpus_per_node = django_filters.NumberFilter(label="Max CPUs per node")
    max_nodes = django_filters.NumberFilter(label="Max nodes")
    min_nodes = django_filters.NumberFilter(label="Min nodes")
    max_time = django_filters.NumberFilter(label="Max time")
    default_time = django_filters.NumberFilter(label="Default time")

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
            "cpu_arch",
            "gpu_arch",
            "qos",
            "priority_tier",
            "exclusive_user",
            "exclusive_topo",
            "req_resv",
        ]

    def filter_has_gpu(self, queryset, name, value):
        """Filter partitions that have GPU architecture."""
        if value:
            return queryset.exclude(gpu_arch="")
        return queryset.filter(gpu_arch="")


class OpenStackInstanceReportFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    flavor_name = django_filters.CharFilter(lookup_expr="icontains")
    image_name = django_filters.CharFilter(lookup_expr="icontains")
    hypervisor_hostname = django_filters.CharFilter(lookup_expr="icontains")

    runtime_state = django_filters.CharFilter()
    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name",
    )

    cores_min = django_filters.NumberFilter(field_name="cores", lookup_expr="gte")
    cores_max = django_filters.NumberFilter(field_name="cores", lookup_expr="lte")
    ram_min = django_filters.NumberFilter(field_name="ram", lookup_expr="gte")
    ram_max = django_filters.NumberFilter(field_name="ram", lookup_expr="lte")
    disk_min = django_filters.NumberFilter(field_name="disk", lookup_expr="gte")
    disk_max = django_filters.NumberFilter(field_name="disk", lookup_expr="lte")

    service_settings_uuid = django_filters.UUIDFilter(
        field_name="service_settings__uuid",
    )
    customer_uuid = django_filters.UUIDFilter(
        field_name="project__customer__uuid",
    )
    project_uuid = django_filters.UUIDFilter(
        field_name="project__uuid",
    )
    tenant_uuid = django_filters.UUIDFilter(
        field_name="tenant__uuid",
    )

    state = core_filters.MappedMultipleChoiceFilter(
        choices=CoreStates.choices,
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("cores", "cores"),
            ("ram", "ram"),
            ("disk", "disk"),
            ("created", "created"),
            ("runtime_state", "runtime_state"),
            ("flavor_name", "flavor_name"),
            ("hypervisor_hostname", "hypervisor_hostname"),
            ("project__customer__name", "customer_name"),
            ("project__name", "project_name"),
            ("service_settings__name", "cluster_name"),
            ("start_time", "start_time"),
        ),
    )

    class Meta:
        model = openstack_models.Instance
        fields = []


class ResourceLimitChangeRequestFilter(django_filters.FilterSet):
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
        label="Resource UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="resource__project__customer__uuid",
        label="Customer UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail",
        field_name="resource__project__uuid",
        label="Project UUID",
    )
    created_by_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        field_name="created_by__uuid",
        label="Created by UUID",
    )
    state = ReviewStateFilter()

    class Meta:
        model = models.ResourceLimitChangeRequest
        fields = []
