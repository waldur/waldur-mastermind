import django_filters
from dbtemplates.models import Template
from django import forms
from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Concat
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.widgets import BooleanWidget
from drf_spectacular.plumbing import build_parameter_type
from drf_spectacular.utils import OpenApiParameter
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.core import models as core_models
from waldur_core.core.enums import CoreStates
from waldur_core.core.filters import (
    ExternalFilterBackend,
    get_generic_field_filter,
)
from waldur_core.core.utils import get_ordering, is_uuid_like, order_with_nulls
from waldur_core.permissions.enums import (
    SYSTEM_CUSTOMER_ROLES,
    SYSTEM_PROJECT_ROLES,
    PermissionEnum,
    RoleEnum,
)
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models
from waldur_core.structure.managers import (
    filter_queryset_by_user_ip,
    filter_queryset_for_user,
    get_connected_customers,
    get_connected_projects,
    get_customer_users,
    get_nested_customer_users,
    get_project_users,
    get_visible_users,
)
from waldur_core.structure.registry import SupportedServices
from waldur_mastermind.billing import models as billing_models


class NameFilterSet(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains", label="Name")
    name_exact = django_filters.CharFilter(
        field_name="name", lookup_expr="exact", label="Name (exact)"
    )


class GenericRoleFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        # Use optimized customer filtering for customer queries when needed
        if (
            hasattr(queryset, "model")
            and queryset.model.__name__ == "Customer"
            and request.user.is_authenticated
            and not request.user.is_staff
            and not request.user.is_support
        ):
            queryset = self._filter_customers_optimized(queryset, request.user)
        else:
            queryset = filter_queryset_for_user(queryset, request.user)
        return filter_queryset_by_user_ip(queryset, request)

    def _filter_customers_optimized(self, queryset, user):
        """Optimized customer filtering to avoid complex permission queries."""
        # Use a single query to get all customer IDs this user has access to
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Q

        accessible_customer_ids = set()

        # Get all content types we need
        customer_ct = ContentType.objects.get_for_model(queryset.model)
        project_ct = ContentType.objects.get_for_model(models.Project)

        # Single query to get all user roles for this user across all relevant content types
        content_type_conditions = Q(content_type=customer_ct) | Q(
            content_type=project_ct
        )

        # Check for call management content types (import locally to avoid module dependency)
        try:
            from waldur_mastermind.proposal.models import Call, CallManagingOrganisation

            call_manager_ct = ContentType.objects.get_for_model(
                CallManagingOrganisation
            )
            call_ct = ContentType.objects.get_for_model(Call)
            content_type_conditions |= Q(content_type=call_manager_ct) | Q(
                content_type=call_ct
            )
        except ImportError:
            call_manager_ct = None
            call_ct = None

        # Single query to get all relevant user roles
        user_roles = (
            UserRole.objects.filter(user=user, is_active=True)
            .filter(content_type_conditions)
            .select_related("content_type")
            .values("content_type__model", "object_id")
        )

        # Process results to get customer IDs
        project_ids = []
        call_manager_ids = []
        call_ids = []

        for role in user_roles:
            model_name = role["content_type__model"]
            object_id = role["object_id"]

            if model_name == "customer":
                accessible_customer_ids.add(object_id)
            elif model_name == "project":
                project_ids.append(object_id)
            elif model_name == "callmanagingorganisation":
                call_manager_ids.append(object_id)
            elif model_name == "call":
                call_ids.append(object_id)

        # Handle project-level access (customers via projects)
        if project_ids:
            project_customer_ids = models.Project.available_objects.filter(
                id__in=project_ids
            ).values_list("customer_id", flat=True)
            accessible_customer_ids.update(project_customer_ids)

        # Handle call management permissions using the data we already collected
        if call_manager_ids and call_manager_ct:
            from waldur_mastermind.proposal.models import CallManagingOrganisation

            call_manager_customer_ids = CallManagingOrganisation.objects.filter(
                id__in=call_manager_ids
            ).values_list("customer_id", flat=True)
            accessible_customer_ids.update(call_manager_customer_ids)

        if call_ids and call_ct:
            from waldur_mastermind.proposal.models import Call

            call_customer_ids = Call.objects.filter(id__in=call_ids).values_list(
                "manager__customer_id", flat=True
            )
            accessible_customer_ids.update(call_customer_ids)
            pass

        if accessible_customer_ids:
            return queryset.filter(id__in=accessible_customer_ids)
        else:
            return queryset.none()


class GenericUserFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user_uuid = request.query_params.get("user_uuid")
        if not user_uuid:
            return queryset

        if not is_uuid_like(user_uuid):
            return queryset.none()

        try:
            user = core_models.User.objects.get(uuid=user_uuid)
        except core_models.User.DoesNotExist:
            return queryset.none()

        return filter_queryset_for_user(queryset, user)

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="user_uuid",
                schema={"type": "string", "format": "uuid"},
                location=OpenApiParameter.QUERY,
                description="Filter by user UUID.",
                extensions={"x-waldur-operation-id": "users_retrieve"},
            )
        ]


class CustomerFilter(NameFilterSet):
    query = django_filters.CharFilter(
        method="filter_query",
        label="Filter by name, native name, abbreviation, domain, UUID, registration code or agreement number",
    )
    native_name = django_filters.CharFilter(
        lookup_expr="icontains", label="Native name"
    )
    abbreviation = django_filters.CharFilter(
        lookup_expr="icontains", label="Abbreviation"
    )
    contact_details = django_filters.CharFilter(
        lookup_expr="icontains", label="Contact details"
    )
    organization_group_uuid = core_filters.ModelMultipleChoiceFilter(
        field_name="organization_groups__uuid",
        label="Organization group UUID",
        to_field_name="uuid",
        queryset=models.OrganizationGroup.objects.all(),
        view_name="organization-group-detail",
    )
    organization_group_name = django_filters.CharFilter(
        field_name="organization_groups__name",
        lookup_expr="icontains",
        label="Organization group name",
    )
    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )
    owned_by_current_user = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_owned_by_current_user",
        label="Return a list of customers where current user is owner.",
    )
    current_user_has_project_create_permission = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_current_user_has_project_create_permission",
        label="Return a list of customers where current user has project create permission.",
    )

    class Meta:
        model = models.Customer
        fields = [
            "name",
            "abbreviation",
            "contact_details",
            "native_name",
            "registration_code",
            "agreement_number",
            "backend_id",
            "archived",
        ]

    def filter_query(self, queryset, name, value):
        if value:
            return queryset.filter(
                Q(name__icontains=value)
                | Q(native_name__icontains=value)
                | Q(abbreviation__icontains=value)
                | Q(domain__icontains=value)
                | Q(uuid=value)
                | Q(registration_code__icontains=value)
                | Q(agreement_number__contains=value)
            ).distinct()
        return queryset

    def filter_owned_by_current_user(self, queryset, name, value):
        if value:
            ids = get_connected_customers(self.request.user, RoleEnum.CUSTOMER_OWNER)
            return queryset.filter(id__in=ids)
        return queryset

    def filter_current_user_has_project_create_permission(self, queryset, name, value):
        user = self.request.user

        if user.is_staff:
            return queryset

        customer_ids_with_permission = (
            UserRole.objects.filter(
                user=user,
                is_active=True,
            )
            .filter(
                role__permissions__permission=PermissionEnum.CREATE_PROJECT,
                content_type=ContentType.objects.get_for_model(models.Customer),
                object_id__in=queryset.values_list("id", flat=True),
            )
            .values_list("object_id", flat=True)
            .distinct()
        )

        if value:
            return queryset.filter(id__in=customer_ids_with_permission)

        return queryset


class ExternalCustomerFilterBackend(ExternalFilterBackend):
    pass


class AccountingStartDateFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        query = Q(accounting_start_date__gt=timezone.now())
        return filter_by_accounting_is_running(request, queryset, query)

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="accounting_is_running",
                schema={"type": "boolean"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by whether accounting is running.",
            )
        ]


class CustomerAccountingStartDateFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if queryset.model == models.Customer:
            query = Q(accounting_start_date__gt=timezone.now())
        else:
            query = Q(customer__accounting_start_date__gt=timezone.now())
        return filter_by_accounting_is_running(request, queryset, query)

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="accounting_is_running",
                schema={"type": "boolean"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by whether accounting is running.",
            )
        ]


def filter_by_accounting_is_running(request, queryset, query):
    if not django_settings.WALDUR_CORE["ENABLE_ACCOUNTING_START_DATE"]:
        return queryset

    value = request.query_params.get("accounting_is_running")
    boolean_field = forms.NullBooleanField()

    try:
        value = boolean_field.to_python(value)
    except exceptions.ValidationError:
        value = None

    if value is None:
        return queryset

    if value:
        return queryset.exclude(query)
    else:
        return queryset.filter(query)


class ProjectTypeFilter(NameFilterSet):
    class Meta:
        model = models.ProjectType
        fields = ["name"]


class ProjectFilter(core_filters.CreatedModifiedFilter, NameFilterSet):
    customer = core_filters.RelatedUUIDInFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        distinct=True,
        label="Customer UUID",
    )

    customer_name = django_filters.CharFilter(
        field_name="customer__name",
        distinct=True,
        lookup_expr="icontains",
        label="Customer name",
    )

    customer_native_name = django_filters.CharFilter(
        field_name="customer__native_name",
        distinct=True,
        lookup_expr="icontains",
        label="Customer native name",
    )

    customer_abbreviation = django_filters.CharFilter(
        field_name="customer__abbreviation",
        distinct=True,
        lookup_expr="icontains",
        label="Customer abbreviation",
    )

    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description"
    )
    conceal_finished_projects = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_conceal_finished_projects",
        label="Conceal finished projects",
    )

    query = django_filters.CharFilter(
        method="filter_query",
        label="Filter by name, slug, UUID, backend ID or resource effective ID",
    )

    can_manage = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_can_manage",
        label="Return a list of projects where current user is manager or a customer owner.",
    )

    slug = django_filters.CharFilter(
        field_name="slug", lookup_expr="exact", label="Slug"
    )

    can_admin = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_can_admin",
        label="Return a list of projects where current user is admin.",
    )

    is_removed = django_filters.BooleanFilter(widget=BooleanWidget, label="Is removed")

    user_uuid_with_active_role = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        method="filter_by_user_uuid_with_active_role",
        label="Filter projects where the given user has a role.",
    )

    affiliated_organization_uuid = core_filters.ModelMultipleChoiceFilter(
        field_name="affiliated_organizations__uuid",
        label="Affiliated organization UUID",
        to_field_name="uuid",
        queryset=models.AffiliatedOrganization.objects.all(),
        view_name="affiliated-organization-detail",
    )

    affiliated_organization_name = django_filters.CharFilter(
        field_name="affiliated_organizations__name",
        lookup_expr="icontains",
        label="Affiliated organization name",
    )

    has_affiliated_organization = django_filters.BooleanFilter(
        widget=BooleanWidget,
        method="filter_has_affiliated_organization",
        label="Filter projects that have at least one affiliated organization.",
    )

    science_domain_uuid = core_filters.RelatedUUIDFilter(
        view_name="science-domain-detail",
        field_name="science_sub_domain__domain__uuid",
        label="Science domain UUID",
    )

    science_sub_domain_uuid = core_filters.RelatedUUIDFilter(
        view_name="science-sub-domain-detail",
        field_name="science_sub_domain__uuid",
        label="Science sub-domain UUID",
    )

    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("created", "created"),
            ("customer__name", "customer_name"),
            ("customer__native_name", "customer_native_name"),
            ("customer__abbreviation", "customer_abbreviation"),
            ("estimated_cost", "estimated_cost"),
            ("end_date", "end_date"),
            ("start_date", "start_date"),
        ),
        label="Ordering",
    )

    class Meta:
        model = models.Project
        fields = [
            "name",
            "customer",
            "customer_name",
            "customer_native_name",
            "customer_abbreviation",
            "description",
            "created",
            "modified",
            "query",
            "backend_id",
            "user_uuid_with_active_role",
        ]

    def filter_can_manage(self, queryset, name, value):
        user = self.request.user

        if value is not None:
            connected_customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)
            connected_projects = get_connected_projects(user, RoleEnum.PROJECT_MANAGER)
            queryset = queryset.filter(
                Q(customer__in=connected_customers) | Q(id__in=connected_projects)
            ).distinct()
        return queryset

    def filter_can_admin(self, queryset, name, value):
        user = self.request.user

        if value is not None:
            connected_projects = get_connected_projects(user, RoleEnum.PROJECT_ADMIN)
            queryset = queryset.filter(id__in=connected_projects)
        return queryset

    def filter_by_user_uuid_with_active_role(self, queryset, name, value):
        try:
            user = core_models.User.objects.get(uuid=value)
        except core_models.User.DoesNotExist:
            return queryset.none()
        project_ct = ContentType.objects.get_for_model(models.Project)
        project_ids = UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type=project_ct,
        ).values_list("object_id", flat=True)
        return queryset.filter(id__in=project_ids)

    def filter_query(self, queryset, name, value):
        if is_uuid_like(value):
            queryset = queryset.filter(
                Q(uuid=value)
                | Q(resource__backend_id=value)
                | Q(resource__effective_id=value)
            )
        else:
            queryset = queryset.filter(
                Q(resource__name=value)
                | Q(name__icontains=value)
                | Q(slug__icontains=value)
                | Q(resource__backend_id__iexact=value)
                | Q(resource__effective_id__iexact=value)
            )
        return queryset.distinct()

    def filter_conceal_finished_projects(self, queryset, name, value):
        if value:
            return queryset.exclude(end_date__lt=timezone.now())
        return queryset

    def filter_has_affiliated_organization(self, queryset, name, value):
        if value:
            return queryset.filter(affiliated_organizations__isnull=False).distinct()
        return queryset.filter(affiliated_organizations__isnull=True)


def filter_visible_users(queryset, user, extra=None):
    if user is None or not user.is_authenticated or user.is_staff or user.is_support:
        return queryset
    return (
        queryset.filter(is_staff=False)
        .filter(Q(id__in=get_visible_users(user)) | Q(id=user.id) | (extra or Q()))
        .distinct()
    )


class UserFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user

        queryset = filter_visible_users(queryset, user, self.get_extra_q(user))
        return queryset.order_by("username")

    _extra_query = []

    @classmethod
    def register_extra_query(cls, func_get_query):
        """
        Add extra Q for user list queryset
        :param func_get_query: a function that takes User object and returns Q object
        :return: None
        """
        cls._extra_query.append(func_get_query)

    @classmethod
    def get_extra_q(cls, user):
        result = Q()
        for q in cls._extra_query:
            result = result | q(user)
        return result


class BaseUserFilter(django_filters.FilterSet):
    full_name = django_filters.CharFilter(
        method="filter_by_full_name", label="Full name"
    )
    user_keyword = django_filters.CharFilter(
        method="filter_by_user_keyword", label="User keyword"
    )
    username = django_filters.CharFilter(label="Username")
    native_name = django_filters.CharFilter(
        lookup_expr="icontains", label="Native name"
    )
    organization = django_filters.CharFilter(
        lookup_expr="icontains", label="Organization"
    )
    job_title = django_filters.CharFilter(lookup_expr="icontains", label="Job title")
    email = django_filters.CharFilter(lookup_expr="icontains", label="Email")
    is_active = django_filters.BooleanFilter(widget=BooleanWidget, label="Is active")
    modified = django_filters.DateTimeFilter(
        lookup_expr="gte", label="Date modified after"
    )
    date_joined = django_filters.DateTimeFilter(
        lookup_expr="gte", label="Date joined after"
    )
    agreement_date = django_filters.DateTimeFilter(
        lookup_expr="gte", label="Agreement date after"
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value)

    def filter_by_user_keyword(self, queryset, name, value):
        return core_filters.filter_by_user_keyword(queryset, value)

    class Meta:
        model = core_models.User
        fields = [
            "full_name",
            "user_keyword",
            "native_name",
            "organization",
            "email",
            "phone_number",
            "description",
            "job_title",
            "username",
            "civil_number",
            "is_active",
            "registration_method",
        ]


class UserFilter(BaseUserFilter):
    is_staff = django_filters.BooleanFilter(
        widget=BooleanWidget, method="filter_is_staff", label="Is staff"
    )
    is_support = django_filters.BooleanFilter(
        widget=BooleanWidget, method="filter_is_support", label="Is support"
    )
    username = django_filters.CharFilter(
        field_name="username", lookup_expr="exact", label="Username (exact)"
    )
    organization_roles = django_filters.CharFilter(
        method="filter_organization_roles", label="Organization roles"
    )
    project_roles = django_filters.CharFilter(
        method="filter_project_roles", label="Project roles"
    )
    query = django_filters.CharFilter(
        method="filter_query",
        label="Filter by first name, last name, civil number, username or email",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", method="filter_by_customer", label="Customer UUID"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", method="filter_by_project", label="Project UUID"
    )
    username_list = django_filters.CharFilter(
        method="filter_username_list", label="Comma-separated usernames"
    )

    o = core_filters.ExtendedOrderingFilter(
        fields=(
            (("first_name", "last_name"), "full_name"),
            "native_name",
            "email",
            "phone_number",
            "description",
            "organization",
            "job_title",
            "username",
            "is_active",
            "registration_method",
            "is_staff",
            "is_support",
        ),
        label="Ordering",
    )

    def filter_is_staff(self, queryset, name, value):
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset.filter(is_staff=value)
        return queryset.none()

    def filter_is_support(self, queryset, name, value):
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset.filter(is_support=value)
        return queryset.none()

    def filter_by_customer(self, queryset, name, value):
        try:
            customer = models.Customer.objects.get(uuid=value)
        except models.Customer.DoesNotExist:
            return queryset.none()

        return queryset.filter(id__in=get_nested_customer_users(customer)).distinct()

    def filter_by_project(self, queryset, name, value):
        try:
            project = models.Project.objects.get(uuid=value)
        except models.Project.DoesNotExist:
            return queryset.none()

        project_users = get_project_users(project.id)
        return queryset.filter(id__in=project_users).distinct()

    def filter_organization_roles(self, queryset, name, value):
        roles = self.request.GET.getlist("organization_roles")
        return queryset.filter(
            userrole__role__name__in=roles, userrole__is_active=True
        ).distinct()

    def filter_project_roles(self, queryset, name, value):
        roles = self.request.GET.getlist("project_roles")
        return queryset.filter(
            userrole__role__name__in=roles, userrole__is_active=True
        ).distinct()

    def filter_query(self, queryset, name, value):
        q = (
            Q(first_name__icontains=value)
            | Q(last_name__icontains=value)
            | Q(civil_number__icontains=value)
            | Q(username__icontains=value)
            | Q(email__icontains=value)
        )

        if len(value.split()) > 1:
            q |= Q(first_name__icontains=value.split()[0])
            q |= Q(last_name__icontains=value.split()[1])

        query = queryset.filter(q)
        return query

    def filter_username_list(self, queryset, name, value):
        if not value:
            return queryset.none()

        usernames = {
            username.strip() for username in value.split(",") if username.strip()
        }
        if not usernames:
            return queryset.none()

        return queryset.filter(username__in=usernames).distinct()


class ConcatenatedNameOrderingBackend(BaseFilterBackend):
    """
    Provides ordering by a concatenated field of first_name, last_name, and username.
    Use the query parameter `o=concatenated_name` or `o=-concatenated_name`.
    """

    def filter_queryset(self, request, queryset, view):
        order_param = request.query_params.get("o")

        if order_param in ("concatenated_name", "-concatenated_name"):
            return queryset.annotate(
                concatenated_name=Concat("first_name", "last_name", "username")
            ).order_by(order_param)

        return queryset

    def get_schema_operation_parameters(self, view):
        """Declare the 'o' parameter for OpenAPI schema."""
        return [
            build_parameter_type(
                name="o",
                schema={
                    "type": "string",
                    "enum": ["concatenated_name", "-concatenated_name"],
                },
                location=OpenApiParameter.QUERY,
                required=False,
                description="Ordering. Sort by a combination of first name, last name, and username.",
            )
        ]


class PermissionReviewFilter(django_filters.FilterSet):
    reviewer_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="reviewer__uuid", label="Reviewer UUID"
    )
    is_pending = django_filters.BooleanFilter(
        field_name="is_pending", label="Is pending"
    )
    o = django_filters.OrderingFilter(fields=("created", "closed"), label="Ordering")

    class Meta:
        fields = [
            "reviewer_uuid",
            "is_pending",
            "closed",
        ]


class CustomerPermissionReviewFilter(PermissionReviewFilter):
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )

    class Meta:
        model = models.CustomerPermissionReview
        fields = PermissionReviewFilter.Meta.fields + [
            "customer_uuid",
        ]


class ProjectPermissionReviewFilter(PermissionReviewFilter):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )

    class Meta:
        model = models.ProjectPermissionReview
        fields = PermissionReviewFilter.Meta.fields + [
            "project_uuid",
        ]


class ProjectEndDateChangeRequestFilter(django_filters.FilterSet):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    created_by_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail",
        field_name="created_by__uuid",
        label="Created by UUID",
    )
    state = core_filters.ReviewStateFilter()

    class Meta:
        model = models.ProjectEndDateChangeRequest
        fields = []


class SshKeyFilter(core_filters.CreatedModifiedFilter, NameFilterSet):
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid", label="User UUID"
    )

    o = django_filters.OrderingFilter(fields=("name",), label="Ordering")

    class Meta:
        model = core_models.SshPublicKey
        fields = [
            "name",
            "fingerprint_md5",
            "fingerprint_sha256",
            "fingerprint_sha512",
            "user_uuid",
            "is_shared",
        ]


class ServiceTypeFilter(django_filters.Filter):
    def filter(self, qs, value):
        value = SupportedServices.get_filter_mapping().get(value)
        return super().filter(qs, value)


class ServiceSettingsFilter(NameFilterSet):
    type = ServiceTypeFilter(label="Type")
    state = core_filters.MappedMultipleChoiceFilter(
        CoreStates.choices,
        label="State",
    )
    customer = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )
    scope_uuid = core_filters.RelatedUUIDFilter(
        method=get_generic_field_filter(
            models_to_search=models.BaseResource.get_all_models()
        ),
        label="Scope UUID",
    )

    class Meta:
        model = models.ServiceSettings
        fields = ("name", "type", "state", "shared", "scope_uuid")


class ServiceSettingsScopeFilterBackend(core_filters.GenericKeyFilterBackend):
    def get_related_models(self):
        return models.BaseResource.get_all_models()

    def get_field_name(self):
        return "scope"


class BaseResourceFilter(NameFilterSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filters["o"] = django_filters.OrderingFilter(
            fields=self.ORDERING_FIELDS, label="Ordering"
        )

    # customer
    customer = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="project__customer__uuid",
        label="Customer UUID",
    )
    customer_name = django_filters.CharFilter(
        field_name="project__customer__name",
        lookup_expr="icontains",
        label="Customer name",
    )
    customer_native_name = django_filters.CharFilter(
        field_name="project__customer__native_name",
        lookup_expr="icontains",
        label="Customer native name",
    )
    customer_abbreviation = django_filters.CharFilter(
        field_name="project__customer__abbreviation",
        lookup_expr="icontains",
        label="Customer abbreviation",
    )
    # project
    project = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid", label="Project UUID"
    )
    project_name = django_filters.CharFilter(
        field_name="project__name", lookup_expr="icontains", label="Project name"
    )
    # service settings
    service_settings_uuid = core_filters.RelatedUUIDFilter(
        view_name="servicesettings-detail",
        field_name="service_settings__uuid",
        label="Service settings UUID",
    )
    service_settings_name = django_filters.CharFilter(
        field_name="service_settings__name",
        lookup_expr="icontains",
        label="Service settings name",
    )
    # resource
    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description"
    )
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")
    backend_id = django_filters.CharFilter(
        field_name="backend_id", lookup_expr="exact", label="Backend ID"
    )
    external_ip = core_filters.EmptyFilter(label="External IP")
    can_manage = django_filters.BooleanFilter(
        label="Can manage", method="filter_can_manage"
    )

    def filter_can_manage(self, queryset, name, value):
        user = self.request.user

        if user.is_staff:
            return queryset

        # Get projects where user is admin or manager
        managed_projects = get_connected_projects(
            user, [RoleEnum.PROJECT_ADMIN, RoleEnum.PROJECT_MANAGER]
        )

        # Get customers where user is owner
        owned_customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)

        # Filter resources by project or customer
        return queryset.filter(
            Q(project__in=managed_projects) | Q(project__customer__in=owned_customers)
        ).distinct()

    ORDERING_FIELDS = (
        ("name", "name"),
        ("state", "state"),
        ("project__customer__name", "customer_name"),
        (
            "project__customer__native_name",
            "customer_native_name",
        ),
        (
            "project__customer__abbreviation",
            "customer_abbreviation",
        ),
        ("project__name", "project_name"),
        ("service_settings__name", "service_name"),
        ("created", "created"),
    )

    class Meta:
        model = models.BaseResource
        fields = (
            # customer
            "customer",
            "customer_uuid",
            "customer_name",
            "customer_native_name",
            "customer_abbreviation",
            # project
            "project",
            "project_uuid",
            "project_name",
            # service settings
            "service_settings_name",
            "service_settings_uuid",
            # resource
            "name",
            "name_exact",
            "description",
            "state",
            "backend_id",
        )


class StartTimeFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        order_by = get_ordering(request)
        if order_by not in ("start_time", "-start_time"):
            return queryset
        return order_with_nulls(queryset, order_by)

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="o",
                schema={
                    "type": "string",
                    "enum": ["start_time", "-start_time"],
                },
                location=OpenApiParameter.QUERY,
                required=False,
                description="Ordering. Sort by start time.",
            )
        ]


class BaseServicePropertyFilter(NameFilterSet):
    class Meta:
        fields = ("name",)


class ServicePropertySettingsFilter(BaseServicePropertyFilter):
    settings_uuid = core_filters.RelatedUUIDFilter(
        view_name="servicesettings-detail",
        field_name="settings__uuid",
        label="Settings UUID",
    )
    settings = core_filters.URLFilter(
        view_name="servicesettings-detail",
        field_name="settings__uuid",
        distinct=True,
        label="Settings URL",
    )

    class Meta(BaseServicePropertyFilter.Meta):
        fields = BaseServicePropertyFilter.Meta.fields + ("settings",)


class OrganizationGroupFilter(NameFilterSet):
    parent = core_filters.RelatedUUIDFilter(
        view_name="organization-group-detail",
        field_name="parent__uuid",
        label="Parent UUID",
    )

    class Meta:
        model = models.OrganizationGroup
        fields = [
            "name",
        ]


class AffiliatedOrganizationFilter(NameFilterSet):
    query = django_filters.CharFilter(method="filter_query", label="Search")
    code = django_filters.CharFilter(lookup_expr="iexact", label="Code")
    abbreviation = django_filters.CharFilter(
        lookup_expr="icontains", label="Abbreviation"
    )
    country = django_filters.CharFilter(lookup_expr="exact", label="Country")

    class Meta:
        model = models.AffiliatedOrganization
        fields = [
            "query",
            "name",
            "code",
            "abbreviation",
            "country",
        ]

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(code__icontains=value)
            | Q(abbreviation__icontains=value)
        )


class ScienceDomainFilter(NameFilterSet):
    class Meta:
        model = models.ScienceDomain
        fields = ["name"]


class ScienceSubDomainFilter(NameFilterSet):
    domain_uuid = core_filters.RelatedUUIDFilter(
        view_name="science-domain-detail",
        field_name="domain__uuid",
        label="Domain UUID",
    )
    domain_name = django_filters.CharFilter(
        field_name="domain__name",
        lookup_expr="icontains",
        label="Domain name",
    )

    class Meta:
        model = models.ScienceSubDomain
        fields = ["name", "domain_uuid", "domain_name"]


class UserAgreementsFilter(django_filters.FilterSet):
    # Note: language filtering is handled in the viewset with fallback logic
    class Meta:
        model = models.UserAgreement
        fields = [
            "agreement_type",
        ]


class UserRolesFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        customer_uuid = view.kwargs.get("customer_uuid")
        if not customer_uuid:
            return queryset

        customer = get_object_or_404(models.Customer, uuid=customer_uuid)

        project_roles = request.query_params.getlist("project_role")
        organization_roles = request.query_params.getlist("organization_role")

        query = Q()

        if project_roles:
            # Filter project permissions by current customer
            projects = models.Project.available_objects.filter(
                customer=customer
            ).values_list("id", flat=True)
            project_users = get_project_users(projects, project_roles)
            query = query | Q(id__in=project_users)

        if organization_roles:
            # Filter customer permissions by current customer
            customer_users = get_customer_users(customer.id, organization_roles)
            query = query | Q(id__in=customer_users)

        return queryset.filter(query)

    def get_schema_operation_parameters(self, view):
        """
        Declare role parameters for OpenAPI schema.
        """
        return [
            build_parameter_type(
                name="project_role",
                schema={
                    "type": "array",
                    "items": {
                        # Use anyOf to allow enum OR a generic string
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": [role.value for role in SYSTEM_PROJECT_ROLES],
                            },
                            {
                                # This second schema allows any other string value
                                "type": "string"
                            },
                        ],
                    },
                },
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by one or more project roles. "
                "Select a standard role or provide a custom role string. "
                "Can be specified multiple times.",
            ),
            build_parameter_type(
                name="organization_role",
                schema={
                    "type": "array",
                    "items": {
                        # Use anyOf to allow enum OR a generic string
                        "anyOf": [
                            {
                                "type": "string",
                                "enum": [role.value for role in SYSTEM_CUSTOMER_ROLES],
                            },
                            {
                                # This second schema allows any other string value
                                "type": "string"
                            },
                        ],
                    },
                },
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by one or more organization roles. "
                "Select a standard role or provide a custom role string. "
                "Can be specified multiple times.",
            ),
        ]


class ProjectEstimatedCostFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        order_by = get_ordering(request)
        if order_by not in ("estimated_cost", "-estimated_cost"):
            return queryset

        ct = ContentType.objects.get_for_model(models.Project)
        estimates = billing_models.PriceEstimate.objects.filter(
            content_type=ct, object_id=OuterRef("pk")
        )
        queryset = queryset.annotate(
            estimated_cost=Subquery(estimates.values("total")[:1])
        )
        return order_with_nulls(queryset, order_by)

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="o",
                schema={
                    "type": "string",
                    "enum": ["estimated_cost", "-estimated_cost"],
                },
                location=OpenApiParameter.QUERY,
                required=False,
                description="Ordering. Sort by estimated cost.",
            )
        ]


class NotificationTemplateFilter(NameFilterSet):
    path = django_filters.CharFilter(lookup_expr="icontains", label="Path")
    path_exact = django_filters.CharFilter(
        field_name="path", lookup_expr="exact", label="Path (exact)"
    )
    is_overridden = django_filters.BooleanFilter(
        method="filter_is_overridden", label="Is overridden"
    )

    class Meta:
        model = core_models.NotificationTemplate
        fields = [
            "name",
            "path",
            "is_overridden",
        ]

    def filter_is_overridden(self, queryset, name, value):
        return queryset.filter(path__in=Template.objects.values_list("name"))


class NotificationFilter(NameFilterSet):
    query = django_filters.CharFilter(
        method="filter_query", label="Filter by key or description"
    )
    is_overridden = django_filters.BooleanFilter(
        method="filter_is_overridden", label="Is overridden"
    )

    class Meta:
        model = core_models.Notification
        fields = ["key", "description", "is_overridden"]

    def filter_query(self, queryset, name, value):
        query = queryset.filter(
            Q(key__icontains=value) | Q(description__icontains=value)
        )
        return query

    def filter_is_overridden(self, queryset, name, value):
        template_names = Template.objects.values_list("name", flat=True)
        overridden_notifications = [
            notification.uuid
            for notification in queryset
            if notification.templates.filter(path__in=template_names).exists() == value
        ]
        return queryset.filter(uuid__in=overridden_notifications)


class AccessSubnetFilter(django_filters.FilterSet):
    customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="customer__uuid",
        label="Customer URL",
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid", label="Customer UUID"
    )
    inet = django_filters.CharFilter(lookup_expr="icontains", label="Inet")
    description = django_filters.CharFilter(
        lookup_expr="icontains", label="Description"
    )

    class Meta:
        model = models.AccessSubnet
        fields = [
            "customer",
            "customer_uuid",
            "inet",
            "description",
        ]


class ExternalLinkFilter(django_filters.FilterSet):
    query = django_filters.CharFilter(
        method="filter_query", label="Filter by name, link or description"
    )

    class Meta:
        model = models.ExternalLink
        fields = ()

    def filter_query(self, queryset, name, value):
        if value:
            return queryset.filter(
                Q(name__icontains=value)
                | Q(link__icontains=value)
                | Q(description__icontains=value)
            ).distinct()
        return queryset
