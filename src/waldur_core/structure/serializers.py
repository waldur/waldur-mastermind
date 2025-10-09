import logging
from datetime import datetime

from constance import config
from dbtemplates import models as dbtemplate_models
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.db import models as django_models
from django.db import transaction
from django.db.models import Q
from django.template import Template, TemplateSyntaxError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers
from rest_framework.authtoken import models as authtoken_models

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.models import Checklist
from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core.enums import CoreStates, CoreStateType
from waldur_core.core.fields import MappedChoiceField
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.serializers import PermissionSerializer
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import models, utils
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.filters import filter_visible_users
from waldur_core.structure.managers import (
    count_customer_users,
    filter_queryset_for_user,
)
from waldur_core.structure.models import CUSTOMER_DETAILS_FIELDS
from waldur_core.structure.notifications import NOTIFICATIONS
from waldur_core.structure.registry import get_resource_type, get_service_type
from waldur_mastermind.marketplace.enums import ResourceStates

logger = logging.getLogger(__name__)


def get_options_serializer_class(service_type):
    return next(
        cls
        for cls in ServiceOptionsSerializer.get_subclasses()
        if get_service_type(cls) == service_type
    )


class PermissionFieldFilteringMixin:
    """
    Mixin allowing to filter related fields.

    In order to constrain the list of entities that can be used
    as a value for the field:

    1. Make sure that the entity in question has corresponding
       Permission class defined.

    2. Implement `get_filtered_field_names()` method
       in the class that this mixin is mixed into and return
       the field in question from that method.
    """

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        for field_name in self.get_filtered_field_names():
            if field_name not in fields:  # field could be not required by user
                continue
            field = fields[field_name]
            field.queryset = filter_queryset_for_user(field.queryset, user)

        return fields

    def get_filtered_field_names(self):
        raise NotImplementedError(
            "Implement get_filtered_field_names() to return list of filtered fields"
        )


class FieldFilteringMixin:
    """
    Mixin allowing to filter fields by user.

    In order to constrain the list of fields implement
    `get_filtered_field()` method returning list of tuples
    (field name, func for check access).
    """

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        # Skip field filtering during schema generation
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        for field_name, check_access in self.get_filtered_field():
            if field_name not in fields:
                continue

            if not check_access(user):
                del fields[field_name]

        return fields

    def get_filtered_field(self):
        raise NotImplementedError(
            "Implement get_filtered_field() to return list of tuples "
        )


class PermissionListSerializer(serializers.ListSerializer):
    """
    Allows to filter related queryset by user.
    Counterpart of PermissionFieldFilteringMixin.

    In order to use it set Meta.list_serializer_class. Example:

    >>> class PermissionProjectSerializer(BasicProjectSerializer):
    >>>     class Meta(BasicProjectSerializer.Meta):
    >>>         list_serializer_class = PermissionListSerializer
    >>>
    >>> class CustomerSerializer(serializers.HyperlinkedModelSerializer):
    >>>     projects = PermissionProjectSerializer(many=True, read_only=True)
    """

    def to_representation(self, data):
        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            pass
        else:
            if isinstance(data, django_models.Manager | django_models.query.QuerySet):
                data = filter_queryset_for_user(data.all(), user)

        return super().to_representation(data)


class BasicUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = core_models.User
        fields = (
            "url",
            "uuid",
            "username",
            "full_name",
            "native_name",
            "email",
            "image",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }


class BasicProjectSerializer(core_serializers.BasicInfoSerializer):
    class Meta(core_serializers.BasicInfoSerializer.Meta):
        model = models.Project


class PermissionProjectSerializer(BasicProjectSerializer):
    resource_count = serializers.SerializerMethodField()

    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer
        fields = BasicProjectSerializer.Meta.fields + (
            "image",
            "resource_count",
            "end_date",
        )

    def get_resource_count(self, project) -> int:
        from waldur_mastermind.marketplace import models as marketplace_models

        return (
            marketplace_models.Resource.objects.filter(
                project=project,
            )
            .exclude(state=ResourceStates.TERMINATED)
            .count()
        )


class ProjectTypeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ProjectType
        fields = ("uuid", "url", "name", "description")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "project_type-detail"},
        }


class ProjectSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    resources_count = serializers.SerializerMethodField()
    oecd_fos_2007_label = serializers.ReadOnlyField(
        source="get_oecd_fos_2007_code_display"
    )
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = models.Project
        fields = (
            "url",
            "uuid",
            "name",
            "slug",
            "customer",
            "customer_uuid",
            "customer_name",
            "customer_slug",
            "customer_native_name",
            "customer_abbreviation",
            "description",
            "customer_display_billing_info_in_projects",
            "created",
            "type",
            "type_name",
            "type_uuid",
            "backend_id",
            "start_date",
            "end_date",
            "end_date_requested_by",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "is_industry",
            "image",
            "resources_count",
            "max_service_accounts",
            "kind",
        )
        read_only_fields = ("end_date_requested_by",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "customer": {"lookup_field": "uuid"},
            "type": {"lookup_field": "uuid", "view_name": "project_type-detail"},
            "end_date_requested_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }
        related_paths = {
            "customer": (
                "uuid",
                "name",
                "native_name",
                "abbreviation",
                "slug",
                "display_billing_info_in_projects",
            ),
            "type": ("name", "uuid"),
        }

    def get_fields(self):
        fields = super().get_fields()

        # Should not be possible to edit the field after the start date has arrived.
        if (
            "start_date" in fields
            and isinstance(self.instance, models.Project)
            and self.instance.start_date
            and self.instance.start_date <= timezone.now().date()
        ):
            fields["start_date"].read_only = True

        if (
            "max_service_accounts" in fields
            and not self.context["request"].user.is_staff
        ):
            fields["max_service_accounts"].read_only = True

        return fields

    def validate_start_date(self, start_date):
        # Allow None to clear the field
        if start_date is None:
            return start_date

        # Only validate non-None values
        if start_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {"start_date": _("Cannot be earlier than the current date.")}
            )
        return start_date

    def validate_end_date(self, end_date):
        # Allow None to clear the field
        if end_date is None:
            return end_date

        # Only validate non-None values
        if end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {"end_date": _("Cannot be earlier than the current date.")}
            )
        return end_date

    @staticmethod
    def eager_load(queryset, request=None):
        related_fields = (
            "uuid",
            "name",
            "created",
            "description",
            "customer__uuid",
            "customer__name",
            "customer__slug",
            "customer__native_name",
            "customer__abbreviation",
            "customer__display_billing_info_in_projects",
        )
        return queryset.select_related("customer").only(*related_fields)

    def get_filtered_field_names(self):
        return ("customer",)

    def validate(self, attrs):
        customer = (
            attrs.get("customer") if not self.instance else self.instance.customer
        )
        end_date = attrs.get("end_date")

        if end_date:
            if not has_permission(
                self.context["request"], PermissionEnum.DELETE_PROJECT, customer
            ):
                raise exceptions.PermissionDenied()
            attrs["end_date_requested_by"] = self.context["request"].user

        if settings.WALDUR_CORE.get("OECD_FOS_2007_CODE_MANDATORY"):
            if (not self.instance and not attrs.get("oecd_fos_2007_code")) or (
                self.instance
                and not self.instance.oecd_fos_2007_code
                and not attrs.get("oecd_fos_2007_code")
            ):
                raise serializers.ValidationError(
                    {"oecd_fos_2007_code": _("This field is required.")}
                )

        if attrs.get("kind") == ProjectKind.COURSE.value:
            if not settings.WALDUR_CORE.get("ENABLE_PROJECT_KIND_COURSE", False):
                raise serializers.ValidationError(
                    'Unable to set project kind to "COURSE": ENABLE_PROJECT_KIND_COURSE feature is disabled.'
                )

            if isinstance(self.instance, models.Project):
                # Check the existing end date
                if self.instance.end_date is None:
                    raise serializers.ValidationError(
                        'Unable to set project kind to "COURSE": end_date is not set.'
                    )
            # Check the end date from attrs
            elif attrs.get("end_date") is None:
                raise serializers.ValidationError(
                    "Unable to create a course project kind: end_date is required."
                )

        return attrs

    def get_resources_count(self, project) -> int:
        from waldur_mastermind.marketplace import models as marketplace_models

        return marketplace_models.Resource.objects.filter(
            state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            project=project,
        ).count()


class CountrySerializerMixin(serializers.Serializer):
    @staticmethod
    def get_country_choices():
        try:
            if config.COUNTRIES:
                if isinstance(config.COUNTRIES, list):
                    if "," in config.COUNTRIES[0]:
                        country_codes = config.COUNTRIES[0].split(",")
                    else:
                        country_codes = config.COUNTRIES
                else:
                    country_codes = config.COUNTRIES.split(",")
                return [
                    item for item in core_fields.COUNTRIES if item[0] in country_codes
                ]
        except Exception:
            logger.exception(
                "Failed to get country choices, using complete list of countries as fallback."
            )
            return core_fields.COUNTRIES

    country = serializers.ChoiceField(
        required=False, choices=core_fields.COUNTRIES, allow_blank=True
    )
    country_name = serializers.CharField(read_only=True, source="get_country_display")

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields
        if "country" in fields:
            fields["country"].choices = self.get_country_choices()
        return fields


class OrganizationGroupSerializer(serializers.HyperlinkedModelSerializer):
    parent_uuid = serializers.UUIDField(read_only=True, source="parent.uuid")
    parent_name = serializers.CharField(read_only=True, source="parent.name")
    customers_count = serializers.SerializerMethodField()

    class Meta:
        model = models.OrganizationGroup
        fields = (
            "uuid",
            "url",
            "name",
            "parent_uuid",
            "parent_name",
            "parent",
            "customers_count",
        )
        extra_kwargs = {
            "url": {"view_name": "organization-group-detail", "lookup_field": "uuid"},
            "parent": {
                "lookup_field": "uuid",
                "view_name": "organization-group-detail",
            },
        }

    def validate_parent(self, parent):
        if parent and parent == self.instance:
            raise serializers.ValidationError(
                {"parent": _("Organization group cannot be parent of itself.")}
            )
        return parent

    def get_customers_count(self, group: models.OrganizationGroup) -> int:
        # Injected to queryset via annotate in view
        try:
            return group.customers_count
        except AttributeError:
            return 0


class CustomerSerializer(
    core_serializers.SlugSerializerMixin,
    CountrySerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    projects = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    organization_groups = OrganizationGroupSerializer(many=True, read_only=True)
    projects_count = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    project_metadata_checklist = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Checklist.objects.filter(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        ),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.Customer
        fields = (
            "url",
            "uuid",
            "created",
            "organization_groups",
            "display_name",
            "projects",
            "backend_id",
            "image",
            "blocked",
            "archived",
            "display_billing_info_in_projects",
            "default_tax_percent",
            "accounting_start_date",
            "projects_count",
            "users_count",
            "sponsor_number",
            "country_name",
            "max_service_accounts",
            "project_metadata_checklist",
        ) + CUSTOMER_DETAILS_FIELDS
        staff_only_fields = (
            "access_subnets",
            "accounting_start_date",
            "default_tax_percent",
            "agreement_number",
            "domain",
            "organization_groups",
            "blocked",
            "archived",
            "display_billing_info_in_projects",
            "sponsor_number",
            "max_service_accounts",
            "project_metadata_checklist",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_optional_fields(self):
        # Make expensive fields optional, only rendered if requested via ?field=
        return super().get_optional_fields() + [
            "projects",
            "users_count",
            "organization_groups",
        ]

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff:
            for field_name in set(CustomerSerializer.Meta.staff_only_fields) & set(
                fields.keys()
            ):
                fields[field_name].read_only = True

        return fields

    def create(self, validated_data):
        user = self.context["request"].user
        if "domain" not in validated_data:
            # Staff can specify domain name on organization creation
            validated_data["domain"] = user.organization
        return super().create(validated_data)

    @staticmethod
    def eager_load(queryset, request=None):
        # Only prefetch fields that are actually requested
        prefetch_relations = []

        if request:
            requested_fields = request.query_params.getlist("field")
            # If no field parameter specified, prefetch all (default behavior)
            # Otherwise only prefetch fields that are explicitly requested
            if not requested_fields or "projects" in requested_fields:
                prefetch_relations.append("projects")
            if not requested_fields or "organization_groups" in requested_fields:
                prefetch_relations.append("organization_groups")
        else:
            # No request context, prefetch all (fallback)
            prefetch_relations = ["projects", "organization_groups"]

        if prefetch_relations:
            queryset = queryset.prefetch_related(*prefetch_relations)
        return queryset

    def validate(self, attrs):
        country = attrs.get("country")
        vat_code = attrs.get("vat_code")

        if vat_code:
            # Check VAT format using the validate_vat_format method from VATMixin
            from waldur_core.structure.models import VATMixin

            if not VATMixin.validate_vat_format(vat_code, country):
                raise serializers.ValidationError(
                    {"vat_code": _("VAT number has invalid format.")}
                )

            # Note: VIES validation (EU VAT Information Exchange System) has been removed.
            # If needed, it can be implemented separately using external services.
            # The vat_name and vat_address fields can now be manually entered if required.
            logger.debug(
                "VAT number %s format validated for country %s. "
                "VIES validation not performed - manual verification may be required.",
                vat_code,
                country,
            )
        return attrs

    def validate_project_metadata_checklist(self, checklist):
        """Validate that the checklist is of PROJECT_METADATA type."""
        if checklist and checklist.checklist_type != ChecklistTypes.PROJECT_METADATA:
            raise serializers.ValidationError(
                _("Checklist must be of type PROJECT_METADATA")
            )
        return checklist

    def get_display_name(self, customer) -> str:
        return customer.get_display_name()

    def get_projects_count(self, customer) -> int:
        # Use annotated value from queryset if available, otherwise fallback to query
        if hasattr(customer, "projects_count"):
            return customer.projects_count
        return models.Project.available_objects.filter(customer=customer).count()

    @extend_schema_field(PermissionProjectSerializer(many=True))
    def get_projects(self, customer):
        # Use prefetched projects if available to avoid N+1 queries
        if hasattr(customer, "_prefetched_projects"):
            projects = customer._prefetched_projects
        else:
            projects = models.Project.available_objects.filter(customer=customer)

        show_all_projects = self.context["request"].query_params.get(
            "show_all_projects"
        )
        if show_all_projects not in ["true", "True"]:
            query = self.context["request"].query_params.get("query")
            if query:
                # If we have prefetched data, filter in Python; otherwise use DB filter
                if hasattr(customer, "_prefetched_projects"):
                    projects = [p for p in projects if query.lower() in p.name.lower()]
                else:
                    projects = projects.filter(name__icontains=query)

        return PermissionProjectSerializer(
            projects, many=True, context=self.context
        ).data

    def get_users_count(self, customer) -> int:
        # Use cached/optimized calculation if available
        if hasattr(customer, "_cached_users_count"):
            return customer._cached_users_count
        # Fallback to the original calculation
        return count_customer_users(customer)


class AccessSubnetSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.AccessSubnet
        fields = (
            "uuid",
            "inet",
            "description",
            "customer",
        )
        extra_kwargs = {
            "customer": {"lookup_field": "uuid"},
        }
        protected_fields = ["customer"]

    inet = serializers.CharField()

    def validate(self, validated_data):
        if not self.instance:
            customer = validated_data["customer"]
            permission = PermissionEnum.CREATE_ACCESS_SUBNET

            if not has_permission(self.context["request"], permission, customer):
                raise exceptions.PermissionDenied()

        return validated_data


class BasicCustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Customer
        fields = (
            "uuid",
            "name",
        )


class NestedProjectSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Project
        fields = ("uuid", "url")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "project-detail"},
        }


class NestedProjectPermissionSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedRelatedField(
        source="scope", lookup_field="uuid", view_name="project-detail", read_only=True
    )
    uuid = serializers.CharField(read_only=True, source="scope.uuid")
    name = serializers.CharField(read_only=True, source="scope.name")
    role_name = serializers.CharField(read_only=True, source="role.name")

    class Meta:
        model = UserRole
        fields = [
            "url",
            "uuid",
            "name",
            "role_name",
            "expiration_time",
        ]


class CustomerUserSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    expiration_time = serializers.SerializerMethodField()
    projects = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()

    class Meta:
        model = core_models.User
        fields = [
            "url",
            "uuid",
            "username",
            "full_name",
            "email",
            "role_name",
            "projects",
            "expiration_time",
            "image",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_customer_permission(self, user):
        customer = self.context["customer"]
        return UserRole.objects.filter(
            scope=customer,
            user=user,
            is_active=True,
        ).first()

    def get_role_name(self, user) -> str | None:
        permission = self.get_customer_permission(user)
        return permission and permission.role.name

    def get_expiration_time(self, user) -> datetime | None:
        permission = self.get_customer_permission(user)
        return permission and permission.expiration_time

    @extend_schema_field(NestedProjectPermissionSerializer(many=True))
    def get_projects(self, user):
        customer = self.context["customer"]
        project_ids = models.Project.available_objects.filter(
            customer=customer
        ).values_list("id", flat=True)
        projects = UserRole.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Project),
            object_id__in=project_ids,
            user=user,
            is_active=True,
        )
        return NestedProjectPermissionSerializer(
            projects, many=True, context=self.context
        ).data


class BasePermissionSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    role_name = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        fields = (
            "role",
            "role_name",
            "user",
            "user_full_name",
            "user_native_name",
            "user_username",
            "user_uuid",
            "user_email",
        )
        related_paths = {
            "user": ("username", "full_name", "native_name", "uuid", "email"),
        }


class BasePermissionReviewSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Common base serializer for permission review models."""

    class Meta:
        fields_common = (
            "url",
            "uuid",
            "reviewer_full_name",
            "reviewer_uuid",
            "is_pending",
            "created",
            "closed",
        )
        read_only_fields = (
            "is_pending",
            "closed",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }
        related_paths_common = {
            "reviewer": ("full_name", "uuid"),
        }


class CustomerPermissionReviewSerializer(BasePermissionReviewSerializer):
    customer_uuid = serializers.UUIDField(read_only=True, source="customer.uuid")
    customer_name = serializers.CharField(read_only=True, source="customer.name")

    class Meta(BasePermissionReviewSerializer.Meta):
        model = models.CustomerPermissionReview
        view_name = "customer_permission_review-detail"
        fields = BasePermissionReviewSerializer.Meta.fields_common + (
            "customer_uuid",
            "customer_name",
        )
        related_paths = dict(
            BasePermissionReviewSerializer.Meta.related_paths_common,
            customer=("name", "uuid"),
        )
        read_only_fields = BasePermissionReviewSerializer.Meta.read_only_fields
        extra_kwargs = BasePermissionReviewSerializer.Meta.extra_kwargs


class ProjectPermissionReviewSerializer(BasePermissionReviewSerializer):
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.CharField(read_only=True, source="project.name")

    class Meta(BasePermissionReviewSerializer.Meta):
        model = models.ProjectPermissionReview
        view_name = "project-permissions-review-detail"
        fields = BasePermissionReviewSerializer.Meta.fields_common + (
            "project_uuid",
            "project_name",
        )
        related_paths = dict(
            BasePermissionReviewSerializer.Meta.related_paths_common,
            project=("name", "uuid"),
        )
        read_only_fields = BasePermissionReviewSerializer.Meta.read_only_fields
        extra_kwargs = BasePermissionReviewSerializer.Meta.extra_kwargs


class ProjectPermissionLogSerializer(
    core_serializers.RestrictedSerializerMixin, BasePermissionSerializer
):
    customer_uuid = serializers.UUIDField(read_only=True, source="scope.customer.uuid")
    customer_name = serializers.CharField(read_only=True, source="scope.customer.name")
    project_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    project_name = serializers.CharField(read_only=True, source="scope.name")
    project_created = serializers.DateTimeField(read_only=True, source="scope.created")
    project_end_date = serializers.DateTimeField(
        read_only=True, source="scope.end_date"
    )
    project = serializers.HyperlinkedRelatedField(
        source="scope",
        view_name="project-detail",
        read_only=True,
        lookup_field="uuid",
    )
    role = serializers.ReadOnlyField(source="role.name")

    class Meta(BasePermissionSerializer.Meta):
        model = UserRole
        fields = (
            "created",
            "expiration_time",
            "created_by",
            "created_by_full_name",
            "created_by_username",
            "project",
            "project_uuid",
            "project_name",
            "project_created",
            "project_end_date",
            "customer_uuid",
            "customer_name",
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            created_by=("full_name", "username"),
            **BasePermissionSerializer.Meta.related_paths,
        )
        view_name = "project_permission_log-detail"
        extra_kwargs = {
            "user": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "queryset": core_models.User.objects.all(),
            },
            "created_by": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
            "role": {
                "view_name": "role-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
        }


class UserSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    email = serializers.EmailField()
    agree_with_policy = serializers.BooleanField(
        write_only=True,
        required=False,
        help_text=_("User must agree with the policy to register."),
    )
    token = serializers.ReadOnlyField(source="auth_token.key")
    token_expires_at = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    requested_email = serializers.SerializerMethodField()
    full_name = serializers.CharField(max_length=200, required=False, read_only=True)
    identity_provider_name = serializers.SerializerMethodField()
    identity_provider_label = serializers.SerializerMethodField()
    identity_provider_management_url = serializers.SerializerMethodField()
    identity_provider_fields = serializers.SerializerMethodField()
    has_active_session = serializers.SerializerMethodField()

    @extend_schema_field(PermissionSerializer(many=True))
    def get_permissions(self, user: core_models.User):
        perms = UserRole.objects.filter(user=user, is_active=True)
        perms = [perm for perm in perms if perm.scope]
        serializer = PermissionSerializer(instance=perms, many=True)
        return serializer.data

    def get_requested_email(self, user: core_models.User) -> str | None:
        try:
            requested_email = core_models.ChangeEmailRequest.objects.get(user=user)
            return requested_email.email
        except core_models.ChangeEmailRequest.DoesNotExist:
            pass

    def get_identity_provider_name(self, user: core_models.User) -> str:
        return utils.get_identity_provider_name(user.registration_method)

    def get_identity_provider_label(self, user: core_models.User) -> str:
        return utils.get_identity_provider_label(user.registration_method)

    def get_identity_provider_management_url(self, user: core_models.User) -> str:
        return utils.get_identity_provider_management_url(user.registration_method)

    def get_identity_provider_fields(self, user: core_models.User) -> list[str]:
        return utils.get_identity_provider_fields(user.registration_method)

    def get_has_active_session(self, user: core_models.User) -> bool:
        return hasattr(user, "auth_token") and user.auth_token is not None

    def get_token_expires_at(self, user: core_models.User) -> None | datetime:
        if hasattr(user, "auth_token") and user.auth_token and user.token_lifetime:
            return user.auth_token.created + timezone.timedelta(
                seconds=user.token_lifetime
            )

    class Meta:
        model = core_models.User
        fields = (
            "url",
            "uuid",
            "username",
            "slug",
            "full_name",
            "native_name",
            "job_title",
            "email",
            "phone_number",
            "organization",
            "civil_number",
            "description",
            "is_staff",
            "is_active",
            "is_support",
            "token",
            "token_lifetime",
            "token_expires_at",
            "registration_method",
            "date_joined",
            "agree_with_policy",
            "agreement_date",
            "notifications_enabled",
            "preferred_language",
            "permissions",
            "requested_email",
            "affiliations",
            "first_name",
            "last_name",
            "identity_provider_name",
            "identity_provider_label",
            "identity_provider_management_url",
            "identity_provider_fields",
            "image",
            "identity_source",
            "has_active_session",
        )
        read_only_fields = (
            "uuid",
            "civil_number",
            "registration_method",
            "date_joined",
            "agreement_date",
            "affiliations",
            "identity_source",
            "has_active_session",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }
        protected_fields = ("email",)

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if user.is_anonymous:
            return fields

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if not user.is_staff:
            protected_fields = (
                "is_active",
                "is_staff",
                "is_support",
                "description",
                "has_active_session",
            )
            if user.is_support:
                for field in protected_fields:
                    if field in fields:
                        fields[field].read_only = True
            else:
                for field in protected_fields:
                    if field in fields:
                        del fields[field]
            if "notifications_enabled" in fields:
                fields["notifications_enabled"].read_only = True

        if not self._can_see_token(user):
            if "token" in fields:
                del fields["token"]
            if "token_lifetime" in fields:
                del fields["token_lifetime"]

        if request.method in ("PUT", "PATCH"):
            fields["username"].read_only = True
            protected_methods = settings.WALDUR_CORE[
                "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS"
            ]
            if (
                user.registration_method
                and user.registration_method in protected_methods
            ):
                detail_fields = (
                    "full_name",
                    "native_name",
                    "job_title",
                    "email",
                    "phone_number",
                    "organization",
                )
                for field in detail_fields:
                    fields[field].read_only = True

        return fields

    def _can_see_token(self, user):
        # Nobody apart from the user herself can see her token.
        # User can see the token either via details view or /api/users/me

        if isinstance(self.instance, list) and len(self.instance) == 1:
            return self.instance[0] == user
        else:
            return self.instance == user

    def validate(self, attrs):
        agree_with_policy = attrs.pop("agree_with_policy", False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                if (
                    self.instance.is_active
                    and "is_active" in attrs.keys()
                    and not attrs["is_active"]
                    and len(attrs) == 1
                ) or self.instance.is_staff:
                    # Deactivation of user.
                    pass
                else:
                    raise serializers.ValidationError(
                        {"agree_with_policy": _("User must agree with the policy.")}
                    )
            else:
                attrs["agreement_date"] = timezone.now()

        if self.instance:
            idp_fields = self.get_identity_provider_fields(self.instance)
            allowed_fields = set(attrs.keys()) - set(idp_fields)
            attrs = {k: v for k, v in attrs.items() if k in allowed_fields}

        if "full_name" in attrs and "first_name" in attrs:
            raise serializers.ValidationError(
                {"first_name": _("Cannot specify first name with full name")}
            )
        elif "full_name" in attrs and "last_name" in attrs:
            raise serializers.ValidationError(
                {"last_name": _("Cannot specify last name with full name")}
            )

        # Convert validation error from Django to DRF
        # https://github.com/tomchristie/django-rest-framework/issues/2145
        try:
            user = core_models.User(id=getattr(self.instance, "id", None), **attrs)
            user.clean()

        except django_exceptions.ValidationError as error:
            raise exceptions.ValidationError(error.message_dict)
        return attrs


class UserEmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class SshKeySerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")

    class Meta:
        model = core_models.SshPublicKey
        fields = (
            "url",
            "uuid",
            "name",
            "public_key",
            "fingerprint_md5",
            "fingerprint_sha256",
            "fingerprint_sha512",
            "user_uuid",
            "is_shared",
            "type",
        )
        read_only_fields = (
            "fingerprint_md5",
            "fingerprint_sha256",
            "fingerprint_sha512",
            "is_shared",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def validate_name(self, value):
        return value.strip()

    def validate_public_key(self, value):
        value = value.strip()
        if len(value.splitlines()) > 1:
            raise serializers.ValidationError(
                _("Key is not valid: it should be single line.")
            )

        try:
            core_models.get_ssh_key_fingerprints(value)
        except (IndexError, TypeError):
            raise serializers.ValidationError(
                _("Key is not valid: cannot generate fingerprint_md5 from it.")
            )
        return value


class MoveProjectSerializer(serializers.Serializer):
    customer = serializers.HyperlinkedRelatedField(
        queryset=models.Customer.objects.all(),
        view_name="customer-detail",
        lookup_field="uuid",
    )
    preserve_permissions = serializers.BooleanField(required=True)


class ServiceOptionsSerializer(serializers.Serializer):
    class Meta:
        secret_fields = ()

    @classmethod
    def get_subclasses(cls):
        for subclass in cls.__subclasses__():
            yield from subclass.get_subclasses()
            yield subclass


class ServiceSettingsSerializer(
    PermissionFieldFilteringMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    customer_native_name = serializers.CharField(
        read_only=True, source="customer.native_name"
    )
    state = MappedChoiceField(
        choices=[(v, k) for k, v in CoreStates.CHOICES],
        choice_mappings={v: k for k, v in CoreStates.CHOICES},
        read_only=True,
    )
    scope = core_serializers.GenericRelatedField(
        related_models=models.BaseResource.get_all_models(),
        required=False,
        allow_null=True,
    )
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    options = serializers.DictField()

    class Meta:
        model = models.ServiceSettings
        fields = (
            "url",
            "uuid",
            "name",
            "type",
            "state",
            "error_message",
            "shared",
            "customer",
            "customer_name",
            "customer_native_name",
            "terms_of_services",
            "scope",
            "scope_uuid",
            "options",
        )
        read_only_fields = ("state", "error_message")
        related_paths = ("customer",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "customer": {"lookup_field": "uuid"},
        }

    def get_filtered_field_names(self):
        return ("customer",)

    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.select_related("customer")

    def get_fields(self):
        fields = super().get_fields()
        method = self.context["view"].request.method
        if method == "GET" and "options" in fields:
            fields["options"] = serializers.SerializerMethodField("get_options")
        return fields

    def get_options(self, service: models.ServiceSettings) -> dict:
        options = {
            "backend_url": service.backend_url,
            "username": service.username,
            "password": service.password,
            "domain": service.domain,
            "token": service.token,
            **service.options,
        }
        request = self.context["request"]

        if request.user.is_staff:
            return options

        if service.customer and service.customer.has_user(
            request.user, CustomerRole.OWNER
        ):
            return options

        options_serializer_class = get_options_serializer_class(service.type)
        secret_fields = options_serializer_class.Meta.secret_fields
        return {k: v for (k, v) in options.items() if k not in secret_fields}


class BasicResourceSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()
    resource_type = serializers.SerializerMethodField()

    def get_resource_type(self, resource) -> str:
        return get_resource_type(resource)


class BaseResourceSerializer(
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.SerializerMethodField()

    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name="project-detail",
        lookup_field="uuid",
    )

    project_name = serializers.CharField(read_only=True, source="project.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")

    service_name = serializers.CharField(read_only=True, source="service_settings.name")

    service_settings = serializers.HyperlinkedRelatedField(
        queryset=models.ServiceSettings.objects.all(),
        view_name="servicesettings-detail",
        lookup_field="uuid",
    )
    service_settings_uuid = serializers.UUIDField(
        read_only=True, source="service_settings.uuid"
    )
    service_settings_state = serializers.CharField(
        read_only=True, source="service_settings.get_state_display"
    )
    service_settings_error_message = serializers.CharField(
        read_only=True, source="service_settings.error_message"
    )

    customer = serializers.HyperlinkedRelatedField(
        source="project.customer",
        view_name="customer-detail",
        read_only=True,
        lookup_field="uuid",
    )

    customer_name = serializers.CharField(
        read_only=True, source="project.customer.name"
    )
    customer_abbreviation = serializers.CharField(
        read_only=True, source="project.customer.abbreviation"
    )
    customer_native_name = serializers.CharField(
        read_only=True, source="project.customer.native_name"
    )

    created = serializers.DateTimeField(read_only=True)
    resource_type = serializers.SerializerMethodField()

    access_url = serializers.SerializerMethodField()

    class Meta:
        model = NotImplemented
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "service_name",
            "service_settings",
            "service_settings_uuid",
            "service_settings_state",
            "service_settings_error_message",
            "project",
            "project_name",
            "project_uuid",
            "customer",
            "customer_name",
            "customer_native_name",
            "customer_abbreviation",
            "error_message",
            "error_traceback",
            "resource_type",
            "state",
            "created",
            "modified",
            "backend_id",
            "access_url",
        )
        protected_fields = (
            "project",
            "service_settings",
        )
        read_only_fields = ("error_message", "error_traceback", "backend_id")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_state(self, obj) -> CoreStateType:
        return obj.get_state_display()

    def get_filtered_field_names(self):
        return ("project", "service_settings")

    def get_resource_type(self, obj) -> str:
        return get_resource_type(obj)

    def get_resource_fields(self) -> list[str]:
        return [f.name for f in self.Meta.model._meta.get_fields()]

    # an optional generic URL for accessing a resource
    def get_access_url(self, obj) -> str | None:
        return obj.get_access_url()

    def get_fields(self):
        fields = super().get_fields()
        # skip validation on object update
        if not self.instance:
            service_type = get_service_type(self.Meta.model)
            if (
                "service_settings" in fields
                and not fields["service_settings"].read_only
            ):
                queryset = fields["service_settings"].queryset.filter(type=service_type)
                fields["service_settings"].queryset = queryset
        return fields

    @transaction.atomic
    def create(self, validated_data):
        data = validated_data.copy()
        fields = self.get_resource_fields()

        # Remove `virtual` properties which ain't actually belong to the model
        data = {key: value for key, value in data.items() if key in fields}

        resource = super().create(data)
        resource.increase_backend_quotas_usage(validate=True)
        return resource

    @classmethod
    def get_subclasses(cls):
        for subclass in cls.__subclasses__():
            yield from subclass.get_subclasses()
            if subclass.Meta.model != NotImplemented:
                yield subclass


class BaseResourceActionSerializer(BaseResourceSerializer):
    project = serializers.HyperlinkedRelatedField(
        view_name="project-detail",
        lookup_field="uuid",
        read_only=True,
    )
    service_settings = serializers.HyperlinkedRelatedField(
        view_name="servicesettings-detail",
        lookup_field="uuid",
        read_only=True,
    )

    class Meta(BaseResourceSerializer.Meta):
        pass


class SshPublicKeySerializerMixin(serializers.HyperlinkedModelSerializer):
    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name="sshpublickey-detail",
        lookup_field="uuid",
        queryset=core_models.SshPublicKey.objects.all(),
        required=False,
        write_only=True,
    )

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if not request or request.user.is_anonymous or request.user.is_staff:
            return fields
        ssh_public_key = fields.get("ssh_public_key")
        if ssh_public_key:
            visible_users = list(
                filter_visible_users(core_models.User.objects.all(), request.user)
            )
            ssh_public_key.queryset = ssh_public_key.queryset.filter(
                Q(user__in=visible_users) | Q(is_shared=True)
            )
        return fields


class VirtualMachineSerializer(SshPublicKeySerializerMixin, BaseResourceSerializer):
    external_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol="ipv4"),
        read_only=True,
    )
    internal_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol="ipv4"),
        read_only=True,
    )

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + (
            "start_time",
            "cores",
            "ram",
            "disk",
            "min_ram",
            "min_disk",
            "ssh_public_key",
            "user_data",
            "external_ips",
            "internal_ips",
            "latitude",
            "longitude",
            "key_name",
            "key_fingerprint",
            "image_name",
        )
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + (
            "start_time",
            "cores",
            "ram",
            "disk",
            "min_ram",
            "min_disk",
            "external_ips",
            "internal_ips",
            "latitude",
            "longitude",
            "key_name",
            "key_fingerprint",
            "image_name",
        )
        protected_fields = BaseResourceSerializer.Meta.protected_fields + (
            "user_data",
            "ssh_public_key",
        )

    def create(self, validated_data):
        if "image" in validated_data:
            validated_data["image_name"] = validated_data["image"].name
        return super().create(validated_data)


class BasePropertySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = NotImplemented


class UserAgreementSerializer(
    serializers.HyperlinkedModelSerializer,
):
    content = core_serializers.HTMLCleanField()

    class Meta:
        model = models.UserAgreement
        fields = ("url", "uuid", "content", "agreement_type", "created", "modified")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "user-agreements-detail"}
        }


class NotificationTemplateDetailSerializers(serializers.ModelSerializer):
    content = serializers.SerializerMethodField()
    original_content = serializers.SerializerMethodField()
    is_content_overridden = serializers.SerializerMethodField()

    class Meta:
        model = core_models.NotificationTemplate
        fields = (
            "uuid",
            "url",
            "path",
            "name",
            "content",
            "original_content",
            "is_content_overridden",
        )
        extra_kwargs = {
            "url": {
                "view_name": "notification-messages-templates-detail",
                "lookup_field": "uuid",
            },
        }

    def get_content(self, obj: core_models.NotificationTemplate) -> str | None:
        try:
            return dbtemplate_models.Template.objects.get(name=obj.path).content
        except dbtemplate_models.Template.DoesNotExist:
            return None

    def get_original_content(self, obj) -> str | None:
        from django.template.engine import Engine
        from django.template.loaders.app_directories import Loader

        loader = Loader(Engine())
        for origin in loader.get_template_sources(obj.path):
            try:
                source = loader.get_contents(origin)
            except Exception:
                continue
            if source:
                return source

    def get_is_content_overridden(self, obj) -> bool:
        return self.get_content(obj) != self.get_original_content(obj)


class NotificationSerializer(serializers.HyperlinkedModelSerializer):
    templates = NotificationTemplateDetailSerializers(many=True, read_only=True)
    context_schema = serializers.SerializerMethodField()

    class Meta:
        model = core_models.Notification
        fields = (
            "uuid",
            "url",
            "key",
            "description",
            "enabled",
            "created",
            "templates",
            "context_schema",
        )
        read_only_fields = ("created", "enabled")
        extra_kwargs = {
            "url": {
                "view_name": "notification-messages-detail",
                "lookup_field": "uuid",
            },
        }

    def get_context_schema(self, obj) -> dict:
        """
        Finds the notification definition in the global NOTIFICATIONS
        dictionary and returns its 'context' schema.
        """
        try:
            section_key, notification_key = obj.key.split(".", 1)
        except ValueError:
            # Handle cases where the key might not have a dot
            return {}

        # Safely get the list of notifications for the section
        notification_definitions = NOTIFICATIONS.get(section_key, [])

        # Find the specific notification by its full key ('path')
        for definition in notification_definitions:
            if definition.get("path") == notification_key:
                # Return the context schema if it exists, otherwise an empty dict
                return definition.get("context_schema", {})

        # Return an empty dict if no matching notification was found
        return {}


class NotificationTemplateUpdateSerializers(serializers.Serializer):
    content = serializers.CharField()

    def validate_content(self, content):
        try:
            Template(content)
        except TemplateSyntaxError as e:
            raise serializers.ValidationError(f"Invalid template syntax: {str(e)}")
        return content


class AuthTokenSerializer(serializers.HyperlinkedModelSerializer):
    user_first_name = serializers.ReadOnlyField(source="user.first_name")
    user_last_name = serializers.ReadOnlyField(source="user.last_name")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_is_active = serializers.ReadOnlyField(source="user.is_active")
    user_token_lifetime = serializers.ReadOnlyField(source="user.token_lifetime")

    class Meta:
        model = authtoken_models.Token
        fields = (
            "url",
            "created",
            "user",
            "user_first_name",
            "user_last_name",
            "user_username",
            "user_is_active",
            "user_token_lifetime",
        )
        extra_kwargs = {
            "url": {
                "view_name": "auth-tokens-detail",
                "lookup_field": "user_id",
            },
            "user": {"lookup_field": "uuid", "view_name": "user-detail"},
        }


class UserAuthTokenSerializer(AuthTokenSerializer):
    token = serializers.ReadOnlyField(source="key")

    class Meta:
        model = authtoken_models.Token
        fields = (
            "created",
            "user_first_name",
            "user_last_name",
            "user_username",
            "user_is_active",
            "user_token_lifetime",
            "token",
        )


class PasswordChangeSerializer(serializers.Serializer):
    new_password = serializers.CharField(write_only=True, required=True)


class ComponentStatsSerializer(serializers.Serializer):
    type = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    measured_unit = serializers.CharField(read_only=True)
    billing_type = serializers.CharField(read_only=True)
    usage = serializers.IntegerField(read_only=True)
    limit_usage = serializers.IntegerField(read_only=True)
    limit = serializers.IntegerField(read_only=True)
    offering_name = serializers.CharField(read_only=True)
    offering_uuid = serializers.UUIDField(read_only=True)


class ComponentsUsageStatsSerializer(serializers.Serializer):
    components = ComponentStatsSerializer(many=True, read_only=True)


class ConfirmEmailRequestSerializer(serializers.Serializer):
    code = serializers.CharField()


class CountrySerializer(serializers.Serializer):
    label = serializers.CharField(read_only=True)
    value = serializers.CharField(read_only=True)


class ConsoleUrlSerializer(serializers.Serializer):
    url = serializers.URLField(read_only=True)


class ExternalLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ExternalLink
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "link",
            "image",
            "created",
            "modified",
        )

        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "external-links-detail"},
        }


class ChecklistInfoSerializer(serializers.Serializer):
    """Serializer for checklist basic information."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    checklist_type = serializers.CharField(read_only=True)


class ComplianceOverviewSerializer(serializers.Serializer):
    """Serializer for project metadata compliance overview response."""

    total_projects = serializers.IntegerField(read_only=True)
    projects_with_completions = serializers.IntegerField(read_only=True)
    fully_completed_projects = serializers.IntegerField(read_only=True)
    projects_requiring_review = serializers.IntegerField(read_only=True)
    average_completion_percentage = serializers.FloatField(read_only=True)


class ProjectDetailSerializer(serializers.Serializer):
    """Serializer for individual project compliance details."""

    project_uuid = serializers.UUIDField(read_only=True)
    project_name = serializers.CharField(read_only=True)
    completion_uuid = serializers.UUIDField(
        read_only=True, required=False, allow_null=True
    )
    completion_percentage = serializers.FloatField(read_only=True)
    is_completed = serializers.BooleanField(read_only=True)
    requires_review = serializers.BooleanField(read_only=True)
    answers = serializers.ListField(read_only=True, required=False)
    unanswered_required_questions = serializers.ListField(
        read_only=True, required=False
    )


class ProjectDetailsResponseSerializer(serializers.Serializer):
    """Serializer for project details response."""

    checklist = ChecklistInfoSerializer(read_only=True)
    total_projects = serializers.IntegerField(read_only=True)
    projects_with_completions = serializers.IntegerField(read_only=True)
    fully_completed_projects = serializers.IntegerField(read_only=True)
    projects_requiring_review = serializers.IntegerField(read_only=True)
    project_details = ProjectDetailSerializer(many=True, read_only=True)


class ProjectAnswerSerializer(serializers.ModelSerializer):
    """Serializer for project checklist answer details."""

    project_uuid = serializers.UUIDField(source="uuid", read_only=True)
    project_name = serializers.CharField(source="name", read_only=True)
    completion_uuid = serializers.SerializerMethodField()
    completion_percentage = serializers.SerializerMethodField()
    is_completed = serializers.SerializerMethodField()
    requires_review = serializers.SerializerMethodField()
    answers_count = serializers.SerializerMethodField()
    unanswered_required_count = serializers.SerializerMethodField()

    class Meta:
        model = models.Project
        fields = [
            "project_uuid",
            "project_name",
            "completion_uuid",
            "completion_percentage",
            "is_completed",
            "requires_review",
            "answers_count",
            "unanswered_required_count",
        ]

    def _get_completion_data(self, project):
        """Get or calculate completion data for project."""
        # Use bulk-loaded completion data if available (most efficient)
        serializer_class = self.__class__
        if hasattr(serializer_class, "_bulk_completion_data"):
            return serializer_class._bulk_completion_data.get(project.id)

        # Fallback to individual queries (less efficient)
        if not hasattr(self, "_completion_cache"):
            self._completion_cache = {}

        if project.id not in self._completion_cache:
            checklist = self.context.get("checklist")
            if not checklist:
                self._completion_cache[project.id] = None
                return None

            # Import here to avoid circular imports
            from django.contrib.contenttypes.models import ContentType

            from waldur_core.checklist import models as checklist_models

            content_type = ContentType.objects.get_for_model(models.Project)
            try:
                completion = checklist_models.ChecklistCompletion.objects.get(
                    checklist=checklist,
                    scope_content_type=content_type,
                    scope_object_id=project.id,
                )
                self._completion_cache[project.id] = completion
            except checklist_models.ChecklistCompletion.DoesNotExist:
                self._completion_cache[project.id] = None

        return self._completion_cache[project.id]

    def get_completion_uuid(self, project) -> str | None:
        """Get completion UUID."""
        completion = self._get_completion_data(project)
        return completion.uuid.hex if completion else None

    def get_completion_percentage(self, project) -> float:
        """Get completion percentage."""
        completion = self._get_completion_data(project)
        return completion.get_completion_percentage() if completion else 0.0

    def get_is_completed(self, project) -> bool:
        """Get completion status."""
        completion = self._get_completion_data(project)
        return completion.is_completed if completion else False

    def get_requires_review(self, project) -> bool:
        """Get review requirement status."""
        completion = self._get_completion_data(project)
        return completion.requires_review if completion else False

    def get_answers_count(self, project) -> int:
        """Get count of answers."""
        completion = self._get_completion_data(project)
        if completion:
            return completion.answers.count()
        return 0

    def get_unanswered_required_count(self, project) -> int:
        """Get count of unanswered required questions."""
        checklist = self.context.get("checklist")
        if not checklist:
            return 0

        completion = self._get_completion_data(project)
        total_required = checklist.questions.filter(required=True).count()

        if completion:
            answered_required = completion.answers.filter(
                question__required=True
            ).count()
            return max(0, total_required - answered_required)
        else:
            return total_required


class ProjectAnswerDetailSerializer(serializers.Serializer):
    """Serializer for individual project answers within a question."""

    project_uuid = serializers.UUIDField(read_only=True)
    project_name = serializers.CharField(read_only=True)
    answer_uuid = serializers.UUIDField(read_only=True, allow_null=True)
    answer_data = serializers.JSONField(read_only=True, allow_null=True)
    answered_by = serializers.CharField(read_only=True, allow_null=True)
    answered_at = serializers.DateTimeField(read_only=True, allow_null=True)
    requires_review = serializers.BooleanField(read_only=True)


class QuestionAnswerSerializer(serializers.ModelSerializer):
    """Serializer for question with all project answers."""

    question_uuid = serializers.UUIDField(source="uuid", read_only=True)
    question_description = serializers.CharField(source="description", read_only=True)
    question_type = serializers.CharField(read_only=True)
    required = serializers.BooleanField(read_only=True)
    order = serializers.IntegerField(read_only=True)
    total_projects = serializers.SerializerMethodField()
    answered_projects_count = serializers.SerializerMethodField()
    project_answers = serializers.SerializerMethodField()
    question_options = serializers.SerializerMethodField()
    min_value = serializers.DecimalField(
        max_digits=20, decimal_places=4, read_only=True, allow_null=True
    )
    max_value = serializers.DecimalField(
        max_digits=20, decimal_places=4, read_only=True, allow_null=True
    )

    class Meta:
        # Import here to avoid circular imports
        from waldur_core.checklist.models import Question

        model = Question
        fields = [
            "question_uuid",
            "question_description",
            "question_type",
            "required",
            "order",
            "min_value",
            "max_value",
            "total_projects",
            "answered_projects_count",
            "project_answers",
            "question_options",
        ]

    def _get_projects_and_answers_data(self, question):
        """Get or calculate project and answer data for this question."""
        # Use bulk-loaded data if available (most efficient)
        serializer_class = self.__class__
        if hasattr(serializer_class, "_bulk_question_data"):
            return serializer_class._bulk_question_data.get(
                question.id,
                {
                    "projects": [],
                    "answers_by_project": {},
                    "total_projects": 0,
                    "answered_projects_count": 0,
                },
            )

        # Fallback to individual queries (less efficient)
        customer = self.context.get("customer")
        if not customer:
            return {
                "projects": [],
                "answers_by_project": {},
                "total_projects": 0,
                "answered_projects_count": 0,
            }

        from django.contrib.contenttypes.models import ContentType

        from waldur_core.checklist.models import Answer
        from waldur_core.structure import models

        # Get all projects for the customer
        projects = list(
            models.Project.objects.filter(customer=customer).order_by("name")
        )
        project_ct = ContentType.objects.get_for_model(models.Project)

        # Get answers for this question across all projects
        answers = Answer.objects.filter(
            question=question,
            completion__scope_content_type=project_ct,
            completion__scope_object_id__in=[p.id for p in projects],
        ).select_related("user", "completion")

        # Create mapping of project_id -> answer
        answers_by_project = {
            answer.completion.scope_object_id: answer for answer in answers
        }

        return {
            "projects": projects,
            "answers_by_project": answers_by_project,
            "total_projects": len(projects),
            "answered_projects_count": len(answers_by_project),
        }

    def get_total_projects(self, question) -> int:
        """Get total projects count."""
        data = self._get_projects_and_answers_data(question)
        return data["total_projects"]

    def get_answered_projects_count(self, question) -> int:
        """Get count of projects that answered this question."""
        data = self._get_projects_and_answers_data(question)
        return data["answered_projects_count"]

    def get_project_answers(self, question) -> list[dict]:
        """Get all project answers for this question."""
        data = self._get_projects_and_answers_data(question)
        projects = data["projects"]
        answers_by_project = data["answers_by_project"]

        project_answers = []

        for project in projects:
            answer = answers_by_project.get(project.id)
            if answer:
                project_answers.append(
                    {
                        "project_uuid": project.uuid.hex,
                        "project_name": project.name,
                        "answer_uuid": answer.uuid.hex,
                        "answer_data": answer.answer_data,
                        "answer_labels": self._get_answer_labels(
                            question, answer.answer_data
                        ),
                        "answered_by": answer.user.full_name if answer.user else None,
                        "answered_at": answer.created,
                        "requires_review": answer.requires_review,
                    }
                )
            else:
                # No answer for this project
                project_answers.append(
                    {
                        "project_uuid": project.uuid.hex,
                        "project_name": project.name,
                        "answer_uuid": None,
                        "answer_data": None,
                        "answer_labels": None,
                        "answered_by": None,
                        "answered_at": None,
                        "requires_review": False,
                    }
                )

        return project_answers

    def get_question_options(self, question) -> list[dict]:
        """Get question options for select-type questions."""
        if question.question_type in ["single_select", "multi_select"]:
            # Use prefetched data if available, otherwise fall back to querying
            options = question.question_options.all()
            # Sort in Python to avoid overriding prefetch_related
            sorted_options = sorted(options, key=lambda opt: opt.order)
            return [
                {
                    "uuid": str(option.uuid),
                    "label": option.label,
                    "order": option.order,
                }
                for option in sorted_options
            ]
        return []

    def _get_answer_labels(self, question, answer_data) -> list[str] | str | None:
        """Convert answer data UUIDs to human-readable labels for select-type questions."""
        if not answer_data or question.question_type not in [
            "single_select",
            "multi_select",
        ]:
            return answer_data

        # Use pre-computed options map if available (most efficient)
        serializer_class = self.__class__
        if hasattr(serializer_class, "_bulk_question_data"):
            question_data = serializer_class._bulk_question_data.get(question.id, {})
            options_map = question_data.get("options_map", {})
        else:
            # Fallback to querying (less efficient)
            options_map = {
                str(option.uuid): option.label
                for option in question.question_options.all()
            }

        if question.question_type == "single_select":
            # answer_data is a single UUID string (for single_select questions stored as list of one item)
            if isinstance(answer_data, list) and len(answer_data) == 1:
                uuid_str = str(answer_data[0])
                return options_map.get(uuid_str, answer_data)
            elif isinstance(answer_data, str):
                return options_map.get(answer_data, answer_data)
            return answer_data

        elif question.question_type == "multi_select":
            # answer_data is a list of UUID strings
            if isinstance(answer_data, list):
                return [
                    options_map.get(str(uuid_val), str(uuid_val))
                    for uuid_val in answer_data
                ]
            return answer_data

        return answer_data
