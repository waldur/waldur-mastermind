import logging
import re
from collections import defaultdict
from datetime import datetime

from constance import config
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db import models as django_models
from django.db.models import Count, Q, Sum
from django.template import Template, TemplateSyntaxError
from django.utils import timezone
from django.utils import timezone as django_timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers
from rest_framework.authtoken import models as authtoken_models

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.models import Checklist, ChecklistCompletion
from waldur_core.checklist.utils import serialize_completion_answers
from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import template_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.core.fields import MappedChoiceField
from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.passkeys import policy as passkey_policy
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.serializers import (
    MePermissionSerializer,
    PermissionSerializer,
)
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import managers, models, utils
from waldur_core.structure.enums import ProjectKind
from waldur_core.structure.filters import filter_visible_users
from waldur_core.structure.managers import (
    count_customer_users,
    filter_queryset_for_user,
)
from waldur_core.structure.models import CUSTOMER_DETAILS_FIELDS
from waldur_core.structure.notifications import NOTIFICATIONS
from waldur_core.structure.registry import get_resource_type, get_service_type
from waldur_core.structure.utils_data_access import log_user_data_access_sync
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


class AffiliatedOrganizationSerializer(
    core_serializers.RestrictedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    projects_count = serializers.SerializerMethodField(
        help_text="Number of active projects affiliated with this organization"
    )

    class Meta:
        model = models.AffiliatedOrganization
        fields = (
            "uuid",
            "url",
            "name",
            "code",
            "abbreviation",
            "description",
            "email",
            "homepage",
            "country",
            "address",
            "created",
            "modified",
            "projects_count",
        )
        extra_kwargs = {
            "url": {
                "view_name": "affiliated-organization-detail",
                "lookup_field": "uuid",
            },
        }

    def get_projects_count(self, org: models.AffiliatedOrganization) -> int:
        try:
            return org.projects_count
        except AttributeError:
            return 0


class ProjectAffiliationUpdateSerializer(serializers.Serializer):
    affiliation = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.AffiliatedOrganization.objects.all(),
        allow_null=True,
        required=False,
    )

    def save(self, **kwargs):
        project = self.instance
        project.affiliation = self.validated_data.get("affiliation")
        project.save(update_fields=["affiliation"])


class CustomerDefaultAffiliationsUpdateSerializer(serializers.Serializer):
    default_affiliations = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.AffiliatedOrganization.objects.all(),
        many=True,
        required=False,
    )

    def save(self, **kwargs):
        customer = self.instance
        orgs = self.validated_data.get("default_affiliations", [])
        customer.default_affiliations.set(orgs)


class ScienceDomainSerializer(serializers.HyperlinkedModelSerializer):
    subdomains_count = serializers.SerializerMethodField(
        help_text="Number of sub-domains in this domain"
    )

    class Meta:
        model = models.ScienceDomain
        fields = (
            "uuid",
            "url",
            "code",
            "name",
            "created",
            "modified",
            "subdomains_count",
        )
        extra_kwargs = {
            "url": {
                "view_name": "science-domain-detail",
                "lookup_field": "uuid",
            },
        }

    def get_subdomains_count(self, obj) -> int:
        try:
            return obj.subdomains_count
        except AttributeError:
            return obj.subdomains.count()


class ScienceSubDomainSerializer(serializers.HyperlinkedModelSerializer):
    domain_uuid = serializers.ReadOnlyField(source="domain.uuid")
    domain_name = serializers.ReadOnlyField(source="domain.name")
    domain_code = serializers.ReadOnlyField(source="domain.code")
    projects_count = serializers.SerializerMethodField(
        help_text="Number of active projects using this sub-domain"
    )

    class Meta:
        model = models.ScienceSubDomain
        fields = (
            "uuid",
            "url",
            "code",
            "name",
            "domain",
            "domain_uuid",
            "domain_name",
            "domain_code",
            "created",
            "modified",
            "projects_count",
        )
        extra_kwargs = {
            "url": {
                "view_name": "science-sub-domain-detail",
                "lookup_field": "uuid",
            },
            "domain": {
                "lookup_field": "uuid",
                "view_name": "science-domain-detail",
            },
        }

    def get_projects_count(self, obj) -> int:
        try:
            return obj.projects_count
        except AttributeError:
            return 0


class ScienceDomainPresetSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField()


class LoadScienceDomainPresetSerializer(serializers.Serializer):
    preset = serializers.ChoiceField(choices=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from waldur_core.structure.presets import SCIENCE_DOMAIN_PRESETS

        self.fields["preset"].choices = list(SCIENCE_DOMAIN_PRESETS.keys())


class LoadScienceDomainPresetResponseSerializer(serializers.Serializer):
    created_domains = serializers.IntegerField()
    created_subdomains = serializers.IntegerField()
    skipped_domains = serializers.IntegerField()
    skipped_subdomains = serializers.IntegerField()


class ProjectListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        project_ids = [item.id for item in data]

        if not project_ids:
            return super().to_representation(data)

        request = self.context.get("request")
        requested_fields = self._get_requested_fields(request)

        # Initialize bulk context
        bulk_data = {
            "resources_count": {},
            "marketplace_resource_counts": {},
            "billing_estimates": {},
            "project_credits": {},
            "project_metadata": {},
        }

        # 1. Bulk fetch resource counts
        if not requested_fields or "resources_count" in requested_fields:
            bulk_data["resources_count"] = self._bulk_fetch_resources_count(project_ids)

        # 2. Bulk fetch marketplace resource counts
        if not requested_fields or "marketplace_resource_count" in requested_fields:
            bulk_data["marketplace_resource_counts"] = (
                self._bulk_fetch_marketplace_resource_counts(project_ids)
            )

        # 3. Bulk fetch billing estimates
        if not requested_fields or "billing_price_estimate" in requested_fields:
            bulk_data["billing_estimates"] = self._bulk_fetch_billing_estimates(
                request, project_ids
            )

        # 4. Bulk fetch project credits
        if not requested_fields or "project_credit" in requested_fields:
            bulk_data["project_credits"] = self._bulk_fetch_project_credits(project_ids)

        # 5. Bulk fetch project metadata checklist answers
        if not requested_fields or "project_metadata" in requested_fields:
            bulk_data["project_metadata"] = self._bulk_fetch_project_metadata(
                project_ids
            )

        self.context["bulk_data"] = bulk_data
        return super().to_representation(data)

    def _bulk_fetch_project_metadata(self, project_ids):
        completions = fetch_project_metadata_completions(project_ids)
        return {
            project_id: serialize_completion_answers(completion)
            for project_id, completion in completions.items()
        }

    def _bulk_fetch_resources_count(self, project_ids):
        from waldur_mastermind.marketplace import models as marketplace_models

        resources_counts = (
            marketplace_models.Resource.objects.filter(
                project_id__in=project_ids,
                state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            )
            .values("project_id")
            .annotate(count=Count("*"))
        )
        return {item["project_id"]: item["count"] for item in resources_counts}

    def _bulk_fetch_marketplace_resource_counts(self, project_ids):
        from waldur_mastermind.marketplace import models as marketplace_models

        category_counts = (
            marketplace_models.Resource.objects.order_by()
            .exclude(state=ResourceStates.TERMINATED)
            .filter(project_id__in=project_ids)
            .values("project_id", "offering__category__uuid")
            .annotate(count=Count("*"))
        )
        category_counts_map = {}
        for item in category_counts:
            project_id = item["project_id"]
            category_uuid = str(item["offering__category__uuid"])
            count = item["count"]
            if project_id not in category_counts_map:
                category_counts_map[project_id] = {}
            category_counts_map[project_id][category_uuid] = count
        return category_counts_map

    def _bulk_fetch_billing_estimates(self, request, project_ids):
        if not request:
            return {}
        from waldur_mastermind.billing import serializers as billing_serializers

        year, month = billing_serializers._parse_period_from_request(request)
        return billing_serializers._bulk_compute_project_estimates(
            project_ids, year, month
        )

    def _bulk_fetch_project_credits(self, project_ids):
        from waldur_mastermind.invoices import models as invoice_models

        return dict(
            invoice_models.ProjectCredit.objects.filter(
                project_id__in=project_ids
            ).values_list("project_id", "value")
        )

    def _get_requested_fields(self, request):
        if not request:
            return []
        return request.query_params.getlist(
            "field", getattr(request, "GET", {}).getlist("field")
        )


@extend_schema_field(OpenApiTypes.ANY)
class ProjectMetadataAnswerValueField(serializers.JSONField):
    """A checklist answer's value: shape depends on the question's type
    (bool for boolean questions, str for text/date/etc., list[str] for
    single/multi-select once option UUIDs are resolved to labels, ...).
    Declared as a plain JSONField, drf-spectacular renders it as a generic
    `{type: object, additionalProperties: true}`, which strictly-typed SDK
    clients can't deserialize non-object answers (a bool, a string) into.
    OpenApiTypes.ANY documents it honestly as "could be anything" instead.
    """


class ProjectMetadataAnswerSerializer(serializers.Serializer):
    """Shape of a single project-metadata checklist answer (read-only)."""

    question_uuid = serializers.CharField()
    question = serializers.CharField(help_text="Question description.")
    question_type = serializers.CharField()
    answer = ProjectMetadataAnswerValueField(
        help_text=(
            "Human-readable answer value; select-type option UUIDs are resolved "
            "to their labels."
        ),
    )


def fetch_project_metadata_completions(project_ids):
    """Map project id -> its PROJECT_METADATA ChecklistCompletion (answers prefetched).

    A project has at most one metadata completion (its customer's metadata
    checklist); stale completions are removed when the checklist changes.
    """
    if not project_ids:
        return {}
    content_type = ContentType.objects.get_for_model(models.Project)
    completions = ChecklistCompletion.objects.filter(
        scope_content_type=content_type,
        scope_object_id__in=project_ids,
        checklist__checklist_type=ChecklistTypes.PROJECT_METADATA,
    ).prefetch_related("answers__question__question_options")
    return {completion.scope_object_id: completion for completion in completions}


class ProjectSerializer(
    core_serializers.UserEmailPatternsValidatorMixin,
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    resources_count = serializers.SerializerMethodField(
        help_text="Number of active resources in this project"
    )
    project_metadata = serializers.SerializerMethodField(
        help_text="Answers to the customer's project-metadata checklist (read-only)."
    )
    oecd_fos_2007_label = serializers.CharField(
        read_only=True,
        source="get_oecd_fos_2007_code_display",
        help_text="Human-readable label for the OECD FOS 2007 classification code",
    )
    description = core_serializers.HTMLCleanField(
        required=False,
        allow_blank=True,
        max_length=DESCRIPTION_LENGTH,
        help_text="Project description (HTML content will be sanitized)",
    )
    start_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project start date. Cannot be edited after the start date has arrived.",
    )
    end_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project end date. Setting this field requires DELETE_PROJECT permission.",
    )
    termination_metadata = serializers.JSONField(
        read_only=True,
        allow_null=True,
        help_text="Metadata about project termination (read-only)",
    )
    staff_notes = core_serializers.HTMLCleanField(
        required=False,
        allow_blank=True,
        max_length=DESCRIPTION_LENGTH,
        help_text="Internal notes visible only to staff and support users (HTML content will be sanitized)",
    )
    effective_end_date = serializers.DateField(
        read_only=True,
        allow_null=True,
        source="end_date_with_grace",
        help_text="Effective end date including grace period. After this date, project resources will be terminated.",
    )
    is_in_grace_period = serializers.BooleanField(
        read_only=True,
        help_text="True if the project is past its end date but still within the grace period.",
    )
    customer_grace_period_days = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        source="customer.grace_period_days",
        help_text="Grace period days set at the customer (organization) level. Used as default when project-level is not set.",
    )
    affiliation = AffiliatedOrganizationSerializer(read_only=True, allow_null=True)
    affiliation_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        source="affiliation",
        queryset=models.AffiliatedOrganization.objects.all(),
        allow_null=True,
        required=False,
    )
    affiliation_name = serializers.ReadOnlyField(source="affiliation.name")
    affiliation_code = serializers.ReadOnlyField(source="affiliation.code")
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )
    science_sub_domain = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ScienceSubDomain.objects.all(),
        allow_null=True,
        required=False,
    )
    science_sub_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.name",
    )
    science_sub_domain_code = serializers.ReadOnlyField(
        source="science_sub_domain.code",
    )
    science_domain_uuid = serializers.ReadOnlyField(
        source="science_sub_domain.domain.uuid",
    )
    science_domain_name = serializers.ReadOnlyField(
        source="science_sub_domain.domain.name",
    )
    science_domain_code = serializers.ReadOnlyField(
        source="science_sub_domain.domain.code",
    )

    class Meta:
        model = models.Project
        list_serializer_class = ProjectListSerializer
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
            "end_date_updated_at",
            "oecd_fos_2007_code",
            "oecd_fos_2007_label",
            "is_industry",
            "image",
            "resources_count",
            "project_metadata",
            "max_service_accounts",
            "kind",
            "is_removed",
            "termination_metadata",
            "staff_notes",
            "grace_period_days",
            "customer_grace_period_days",
            "effective_end_date",
            "is_in_grace_period",
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "affiliation",
            "affiliation_uuid",
            "affiliation_name",
            "affiliation_code",
            "science_sub_domain",
            "science_sub_domain_name",
            "science_sub_domain_code",
            "science_domain_uuid",
            "science_domain_name",
            "science_domain_code",
        )
        read_only_fields = (
            "end_date_requested_by",
            "end_date_updated_at",
            "is_removed",
            "termination_metadata",
            "project_metadata",
            "effective_end_date",
            "is_in_grace_period",
        )
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

        # Handle staff_notes field visibility and permissions
        if "staff_notes" in fields:
            user = self.context["request"].user
            # Check if this is schema generation context (drf-spectacular)
            # When generating schema, we want to include all fields
            is_schema_generation = getattr(
                self.context.get("view"), "swagger_fake_view", False
            )

            if not is_schema_generation:
                if not (user.is_staff or user.is_support):
                    # Remove field entirely for non-staff/non-support users
                    del fields["staff_notes"]
                elif not user.is_staff:
                    # Support users can see but not edit
                    fields["staff_notes"].read_only = True

        # Handle staff-only override fields (visible to all, editable by staff)
        for field_name in ("grace_period_days",):
            if field_name not in fields:
                continue
            user = self.context["request"].user
            # Check if this is schema generation context (drf-spectacular)
            # When generating schema, we want to include all fields
            is_schema_generation = getattr(
                self.context.get("view"), "swagger_fake_view", False
            )

            if not is_schema_generation:
                if not user.is_staff:
                    # Make field read-only for non-staff users
                    fields[field_name].read_only = True

        # Make all fields read-only for terminated (soft-deleted) projects
        if isinstance(self.instance, models.Project) and self.instance.is_removed:
            for field_name, field in fields.items():
                if field_name not in self.Meta.read_only_fields:
                    field.read_only = True

        # Handle user_email_patterns, user_affiliations, and user_identity_sources permissions
        # Only users with CREATE_PROJECT permission on customer can edit these
        restriction_fields = (
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
        )
        for field_name in restriction_fields:
            if field_name in fields:
                user = self.context["request"].user
                is_schema_generation = getattr(
                    self.context.get("view"), "swagger_fake_view", False
                )
                if not is_schema_generation:
                    customer = (
                        self.instance.customer
                        if isinstance(self.instance, models.Project)
                        else None
                    )
                    if customer and not has_permission(
                        self.context["request"],
                        PermissionEnum.CREATE_PROJECT,
                        customer,
                    ):
                        fields[field_name].read_only = True

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
        return queryset.select_related(
            "customer",
            "type",
            "projectcredit",
            "end_date_requested_by",
            "science_sub_domain",
            "science_sub_domain__domain",
            "affiliation",
        )

    def get_filtered_field_names(self):
        return ("customer",)

    def validate(self, attrs):
        customer = (
            attrs.get("customer") if not self.instance else self.instance.customer
        )
        # Guard every actual change to the end date, including a change to null.
        # Testing the value for truthiness let an explicit {"end_date": null}
        # through unchecked, so removing a project's expiry — which keeps it
        # alive indefinitely — was easier than setting one. A write that repeats
        # the current value is left alone, so unrelated partial updates that echo
        # the field are not blocked.
        if "end_date" in attrs:
            current_end_date = self.instance.end_date if self.instance else None
            if attrs["end_date"] != current_end_date:
                if not has_permission(
                    self.context["request"], PermissionEnum.DELETE_PROJECT, customer
                ):
                    raise exceptions.PermissionDenied()
                attrs["end_date_requested_by"] = self.context["request"].user

        # Each "field X is mandatory" flag below enforces the constraint on
        # create, or on an update that explicitly sets the field to null. An
        # update that simply omits the field is allowed even if the project's
        # current value is null -- otherwise every PATCH would have to re-send
        # the mandatory fields, which breaks unrelated partial updates (e.g.
        # bulk-assigning affiliations or sub-domains).
        def _require_on_create(field_name, response_key=None):
            response_key = response_key or field_name
            if not self.instance and not attrs.get(field_name):
                raise serializers.ValidationError(
                    {response_key: _("This field is required.")}
                )
            if self.instance and field_name in attrs and not attrs[field_name]:
                raise serializers.ValidationError(
                    {response_key: _("This field is required.")}
                )

        if settings.WALDUR_CORE.get("OECD_FOS_2007_CODE_MANDATORY"):
            _require_on_create("oecd_fos_2007_code")

        if config.PROJECT_END_DATE_MANDATORY:
            _require_on_create("end_date")

        if config.AFFILIATION_REQUIRED_AT_PROJECT_CREATION:
            _require_on_create("affiliation", response_key="affiliation_uuid")

        # Enforce the configurable project-name pattern only when the name is
        # actually being set (on create, or on an update that changes it), so
        # unrelated PATCHes to projects whose existing name predates the rule
        # are not blocked.
        name = attrs.get("name")
        if name is not None:
            name_error = core_validators.get_project_name_regex_error(name)
            if name_error:
                raise serializers.ValidationError({"name": name_error})

        affiliation = attrs.get("affiliation")
        if affiliation is not None:
            request = self.context.get("request")
            user = request.user if request else None
            if user is not None and not user.is_staff:
                if not customer.default_affiliations.filter(pk=affiliation.pk).exists():
                    raise serializers.ValidationError(
                        {
                            "affiliation_uuid": _(
                                "Selected affiliation is not in this organization's default list."
                            )
                        }
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
        bulk_data = self.context.get("bulk_data", {})
        if "resources_count" in bulk_data:
            return bulk_data["resources_count"].get(project.id, 0)

        # Fallback for cases when eager_load wasn't applied
        if hasattr(project, "_resources_count"):
            return project._resources_count

        from waldur_mastermind.marketplace import models as marketplace_models

        return marketplace_models.Resource.objects.filter(
            state__in=(ResourceStates.OK, ResourceStates.UPDATING),
            project=project,
        ).count()

    @extend_schema_field(ProjectMetadataAnswerSerializer(many=True))
    def get_project_metadata(self, project) -> list[dict]:
        bulk_data = self.context.get("bulk_data", {})
        if "project_metadata" in bulk_data:
            return bulk_data["project_metadata"].get(project.id, [])

        # Fallback for the detail view (no bulk pre-fetch).
        completion = fetch_project_metadata_completions([project.id]).get(project.id)
        return serialize_completion_answers(completion) if completion else []


class CountrySerializerMixin(serializers.Serializer):
    @staticmethod
    def get_country_choices():
        try:
            # Check if database is properly configured to avoid errors during schema generation
            try:
                # Try to check database connection without executing queries
                connection.ensure_connection()
                # If connection is dummy backend, skip database-dependent config
                if connection.vendor == "dummy":
                    return core_fields.COUNTRIES
            except (ImproperlyConfigured, Exception):
                # Database not available or improperly configured, use defaults
                return core_fields.COUNTRIES

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
            # Fallback to default without logging during schema generation
            pass
        return core_fields.COUNTRIES

    country = serializers.ChoiceField(
        required=False,
        choices=core_fields.COUNTRIES,
        allow_blank=True,
        help_text="Country code (ISO 3166-1 alpha-2)",
    )
    country_name = serializers.CharField(
        read_only=True,
        source="get_country_display",
        help_text="Human-readable country name",
    )

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
    parent_uuid = serializers.UUIDField(
        read_only=True,
        source="parent.uuid",
        help_text="UUID of the parent organization group",
    )
    parent_name = serializers.CharField(
        read_only=True,
        source="parent.name",
        help_text="Name of the parent organization group",
    )
    customers_count = serializers.SerializerMethodField(
        help_text="Number of customers in this organization group"
    )

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


class CustomerContactUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Customer
        fields = (
            "contact_details",
            "email",
            "phone_number",
            "homepage",
            "notification_emails",
        )


class CustomerListSerializer(serializers.ListSerializer):
    """
    Handles bulk optimizations for Customer collections to prevent N+1 queries.
    Calculates context data ONCE per page and injects it into child serializers.
    """

    def to_representation(self, data):
        # 1. Extract context and requested fields
        request = self.context.get("request")
        requested_fields = self._get_requested_fields(request)

        # 'data' is the queryset/list of objects for the current page
        customer_ids = [item.id for item in data]

        if not customer_ids:
            return super().to_representation(data)

        # 2. Build the bulk context dictionary
        bulk_context = {
            "visibility": self._get_visibility_context(request, customer_ids),
            "users_count": {},
            "billing_estimates": {},
            "customer_credits": {},
            "project_credits_sums": {},
        }

        # 3. Bulk fetch complex aggregations only if requested
        if not requested_fields or "users_count" in requested_fields:
            bulk_context["users_count"] = self._bulk_calculate_users_count(
                bulk_context["visibility"], customer_ids
            )

        if not requested_fields or "billing_price_estimate" in requested_fields:
            bulk_context["billing_estimates"] = self._bulk_fetch_billing_estimates(
                bulk_context["visibility"], customer_ids
            )

        if (
            not requested_fields
            or "customer_credit" in requested_fields
            or "customer_unallocated_credit" in requested_fields
        ):
            credits_data = self._bulk_fetch_credits(customer_ids)
            bulk_context["customer_credits"] = credits_data["customer_credits"]
            bulk_context["project_credits_sums"] = credits_data["project_credits_sums"]

        # 4. Inject into the serializer context for child serializers to consume
        self.context["bulk_data"] = bulk_context

        # 5. Proceed with standard serialization
        return super().to_representation(data)

    def _get_requested_fields(self, request):
        if not request:
            return []
        return request.query_params.getlist(
            "field", getattr(request, "GET", {}).getlist("field")
        )

    def _get_visibility_context(self, request, customer_ids):
        """Returns a mapping of {customer_id: [visible_project_ids]}"""
        user = getattr(request, "user", None)
        visibility_map = defaultdict(list)

        if not user or not user.is_authenticated:
            return visibility_map

        if user.is_staff or user.is_support:
            # None signifies 'Full Visibility'
            return {cid: None for cid in customer_ids}

        # Query user roles ONCE for the page
        user_projects = managers.get_visible_projects(user)

        # Map customers to their visible projects
        visible_projects = models.Project.available_objects.filter(
            customer_id__in=customer_ids, id__in=user_projects
        ).values_list("customer_id", "id")

        for customer_id, project_id in visible_projects:
            visibility_map[customer_id].append(project_id)

        # Check for direct customer roles (full visibility for those customers)
        customer_ct = ContentType.objects.get_for_model(models.Customer)
        direct_customers = UserRole.objects.filter(
            user=user,
            is_active=True,
            content_type=customer_ct,
            object_id__in=customer_ids,
        ).values_list("object_id", flat=True)

        for cid in direct_customers:
            visibility_map[cid] = None

        return visibility_map

    def _bulk_calculate_users_count(self, visibility_map, customer_ids):
        """Bulk query to count unique users per customer based on visibility."""
        counts = {}
        customer_ct = ContentType.objects.get_for_model(models.Customer)
        project_ct = ContentType.objects.get_for_model(models.Project)

        for customer_id in customer_ids:
            pids = visibility_map.get(customer_id)
            if pids is None:
                # Full visibility: count users across all projects and the customer itself
                project_ids = list(
                    models.Project.available_objects.filter(
                        customer_id=customer_id
                    ).values_list("id", flat=True)
                )

                user_roles_query = Q(
                    content_type=customer_ct,
                    object_id=customer_id,
                    is_active=True,
                )

                if project_ids:
                    user_roles_query |= Q(
                        content_type=project_ct,
                        object_id__in=project_ids,
                        is_active=True,
                    )
            else:
                # Restricted visibility: count users only in visible projects
                if not pids:
                    counts[customer_id] = 0
                    continue

                user_roles_query = Q(
                    content_type=project_ct,
                    object_id__in=pids,
                    is_active=True,
                )

            unique_user_count = (
                UserRole.objects.filter(user_roles_query)
                .values("user_id")
                .distinct()
                .count()
            )
            counts[customer_id] = unique_user_count

        return counts

    def _bulk_fetch_billing_estimates(self, visibility_map, customer_ids):
        """Bulk query for billing estimates."""
        # Note: We import billing models here to avoid circular dependencies
        from waldur_mastermind.billing import models as billing_models

        customer_ct = ContentType.objects.get_for_model(models.Customer)
        project_ct = ContentType.objects.get_for_model(models.Project)

        full_visibility_cids = [
            cid for cid, pids in visibility_map.items() if pids is None
        ]
        restricted_visibility_map = {
            cid: pids for cid, pids in visibility_map.items() if pids is not None
        }

        all_restricted_pids = []
        for pids in restricted_visibility_map.values():
            all_restricted_pids.extend(pids)

        estimates_cache = {}

        # 1. Fetch customer-level estimates for full visibility
        if full_visibility_cids:
            customer_estimates = billing_models.PriceEstimate.objects.filter(
                content_type=customer_ct, object_id__in=full_visibility_cids
            ).select_related("content_type")
            for est in customer_estimates:
                estimates_cache[est.object_id] = est

        # 2. Fetch project-level estimates for restricted visibility
        if all_restricted_pids:
            project_estimates = billing_models.PriceEstimate.objects.filter(
                content_type=project_ct, object_id__in=all_restricted_pids
            ).select_related("content_type")

            # Group project estimates by customer
            # We need to know which project belongs to which customer
            project_to_customer = dict(
                models.Project.available_objects.filter(
                    id__in=all_restricted_pids
                ).values_list("id", "customer_id")
            )

            for est in project_estimates:
                cid = project_to_customer.get(est.object_id)
                if cid:
                    if cid not in estimates_cache:
                        estimates_cache[cid] = []
                    if isinstance(estimates_cache[cid], list):
                        estimates_cache[cid].append(est)

        return estimates_cache

    def _bulk_fetch_credits(self, customer_ids):
        """Bulk query for customer and project credits."""
        from waldur_mastermind.invoices import models as invoice_models

        # Fetch customer credits
        customer_credits = dict(
            invoice_models.CustomerCredit.objects.filter(
                customer_id__in=customer_ids
            ).values_list("customer_id", "value")
        )

        # Fetch project credits sum per customer (for unallocated credit calculation)
        project_credits_sums = dict(
            invoice_models.ProjectCredit.objects.filter(
                project__customer_id__in=customer_ids
            )
            .values("project__customer_id")
            .annotate(sum=Sum("value"))
            .values_list("project__customer_id", "sum")
        )

        return {
            "customer_credits": customer_credits,
            "project_credits_sums": project_credits_sums,
        }


class CustomerSerializer(
    core_serializers.UserEmailPatternsValidatorMixin,
    core_serializers.SlugSerializerMixin,
    CountrySerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    display_name = serializers.SerializerMethodField(
        help_text="Display name of the organization (includes native name if available)"
    )
    organization_groups = OrganizationGroupSerializer(
        many=True,
        read_only=True,
        help_text="Organization groups this customer belongs to",
    )
    projects_count = serializers.SerializerMethodField(
        help_text="Number of projects in this organization"
    )
    users_count = serializers.SerializerMethodField(
        help_text="Number of users with access to this organization"
    )
    project_metadata_checklist = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Checklist.objects.filter(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        ),
        required=False,
        allow_null=True,
        help_text="Checklist to be used for project metadata validation in this organization",
    )
    default_affiliations = AffiliatedOrganizationSerializer(
        many=True,
        read_only=True,
        help_text="Affiliations offered to project creators of this organization.",
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )

    class Meta:
        model = models.Customer
        list_serializer_class = CustomerListSerializer
        fields = (
            "url",
            "uuid",
            "created",
            "organization_groups",
            "display_name",
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
            "grace_period_days",
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "default_affiliations",
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
            "user_email_patterns",
            "user_affiliations",
            "user_identity_sources",
            "project_slug_template",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

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

        if not user.is_staff:
            staff_fields = set(self.Meta.staff_only_fields) | {"grace_period_days"}
            for field_name in staff_fields & set(fields.keys()):
                fields[field_name].read_only = True

        return fields

    def create(self, validated_data):
        user = self.context["request"].user
        if "domain" not in validated_data:
            # Staff can specify domain name on organization creation
            validated_data["domain"] = user.organization
        return super().create(validated_data)

    def validate_project_slug_template(self, value):
        if not value:
            return value

        placeholders = re.findall(r"\{([^}:]+)", value)

        allowed_placeholders = {
            "customer_slug",
            "project_name",
            "year",
            "month",
            "counter",
            "counter_padded",
        }

        invalid_placeholders = set(placeholders) - allowed_placeholders
        if invalid_placeholders:
            raise serializers.ValidationError(
                f"Invalid placeholders: {', '.join(sorted(invalid_placeholders))}. "
                f"Allowed: {', '.join(sorted(allowed_placeholders))}"
            )

        test_context = {
            "customer_slug": "test-org",
            "project_name": "test-project",
            "year": "2026",
            "month": "04",
            "counter": "1",
            "counter_padded": "001",
        }

        try:
            value.format(**test_context)
        except (KeyError, ValueError) as e:
            raise serializers.ValidationError(f"Invalid template format: {e}")

        return value

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
        # Use annotated value if available (from ViewSet.get_queryset)
        if hasattr(customer, "annotated_projects_count"):
            return customer.annotated_projects_count

        # Fallback for cases when annotation wasn't applied (e.g. detail view or nested)
        return models.Project.available_objects.filter(customer=customer).count()

    @extend_schema_field(PermissionProjectSerializer(many=True))
    def get_projects(self, customer):
        # Use prefetched projects if available (via to_attr="visible_projects")
        if hasattr(customer, "visible_projects"):
            projects = customer.visible_projects
        else:
            projects = models.Project.available_objects.filter(customer=customer)

        show_all_projects = self.context.get("request", {}).query_params.get(
            "show_all_projects"
        )
        if show_all_projects not in ["true", "True"]:
            query = self.context.get("request", {}).query_params.get("query")
            if query:
                # If we have prefetched data, filter in Python; otherwise use DB filter
                if isinstance(projects, list):
                    projects = [p for p in projects if query.lower() in p.name.lower()]
                else:
                    projects = projects.filter(name__icontains=query)

        return PermissionProjectSerializer(
            projects, many=True, context=self.context
        ).data

    def get_users_count(self, customer) -> int:
        # Use bulk-loaded data if available
        bulk_data = self.context.get("bulk_data", {})
        if "users_count" in bulk_data:
            return bulk_data["users_count"].get(customer.id, 0)

        # Fallback for single-object view
        return self._calculate_single_user_count(customer)

    def _calculate_single_user_count(self, customer) -> int:
        request = self.context.get("request")
        if request and hasattr(request, "user"):
            user = request.user
            if user.is_staff or user.is_support:
                return count_customer_users(customer)

            customer_ct = ContentType.objects.get_for_model(models.Customer)
            project_ct = ContentType.objects.get_for_model(models.Project)
            if UserRole.objects.filter(
                user=user,
                is_active=True,
                content_type=customer_ct,
                object_id=customer.id,
            ).exists():
                return count_customer_users(customer)

            user_projects = managers.get_visible_projects(user)
            visible_project_ids = list(
                models.Project.available_objects.filter(
                    customer=customer,
                    id__in=user_projects,
                ).values_list("id", flat=True)
            )
            if not visible_project_ids:
                return 0
            return (
                UserRole.objects.filter(
                    content_type=project_ct,
                    object_id__in=visible_project_ids,
                    is_active=True,
                )
                .values("user_id")
                .distinct()
                .count()
            )
        return count_customer_users(customer)


class ScopedOfferingSerializer(serializers.Serializer):
    """An offering an access subnet applies to, with enough to label it."""

    uuid = serializers.CharField()
    name = serializers.CharField()
    # False once the organization has terminated its last resource of the
    # offering. The scope is kept so re-provisioning restores protection, but
    # the portal has to show it as stale — it can be removed, not re-added, and
    # is no longer exported.
    has_live_resources = serializers.BooleanField()


class AccessSubnetSerializer(
    core_serializers.AccessSubnetMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.AccessSubnet
        fields = (
            "uuid",
            "inet",
            "description",
            "customer",
            "applies_to_portal",
            "offerings",
            "scoped_offerings",
            "is_staff_managed",
        )
        extra_kwargs = {
            "customer": {"lookup_field": "uuid"},
        }
        protected_fields = ["customer"]
        read_only_fields = ["is_staff_managed", "scoped_offerings"]

    inet = serializers.CharField()
    scoped_offerings = ScopedOfferingSerializer(many=True, read_only=True)
    offerings = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        help_text="UUIDs of offerings this network may reach. Only offerings "
        "the organization consumes and that enable access subnets are accepted.",
    )

    def to_representation(self, instance):
        from waldur_mastermind.marketplace import models as marketplace_models

        data = super().to_representation(instance)
        scopes = marketplace_models.AccessSubnetOfferingScope.objects.filter(
            access_subnet=instance
        ).select_related("offering")
        data["offerings"] = [scope.offering.uuid.hex for scope in scopes]
        # Names travel with the entry because there is no way to look an
        # offering up by uuid from the portal, and a dormant one is absent from
        # the list of offerings the organization consumes.
        data["scoped_offerings"] = [
            {
                "uuid": scope.offering.uuid.hex,
                "name": scope.offering.name,
                "has_live_resources": marketplace_models.Resource.objects.filter(
                    project__customer_id=instance.customer_id,
                    offering_id=scope.offering_id,
                )
                .exclude(state=marketplace_models.Resource.States.TERMINATED)
                .exists(),
            }
            for scope in scopes
        ]
        return data

    def _already_scoped(self):
        """Offering ids this entry is already scoped to."""
        from waldur_mastermind.marketplace import models as marketplace_models

        if self.instance is None:
            return set()
        return set(
            marketplace_models.AccessSubnetOfferingScope.objects.filter(
                access_subnet=self.instance
            ).values_list("offering_id", flat=True)
        )

    def _resolve_offerings(self, customer, uuids):
        """Offerings the customer may scope this subnet to.

        Rejects anything the customer does not consume or that has not opted
        into access subnets — the two conditions that would otherwise surface
        as a confusing failure after the fact.

        Offerings already scoped are exempt from those checks. An organization
        that terminates its last resource of an offering keeps the scope, and
        re-validating it would reject every subsequent edit of the entry —
        including the one removing that very scope, leaving no way out.
        """
        from waldur_mastermind.marketplace import models as marketplace_models

        if not uuids:
            return []
        offerings = list(
            marketplace_models.Offering.objects.filter(uuid__in=uuids).distinct()
        )
        found = {offering.uuid.hex for offering in offerings}
        missing = {str(value).replace("-", "") for value in uuids} - found
        if missing:
            raise exceptions.ValidationError(
                {"offerings": _("Offerings not found: %s") % ", ".join(sorted(missing))}
            )
        already_scoped = self._already_scoped()
        for offering in offerings:
            if offering.id in already_scoped:
                continue
            if not (offering.plugin_options or {}).get(
                "enable_resource_access_subnets"
            ):
                raise exceptions.ValidationError(
                    {
                        "offerings": _(
                            "Access subnets are not enabled for offering %s."
                        )
                        % offering.name
                    }
                )
            if (
                not marketplace_models.Resource.objects.filter(
                    project__customer=customer, offering=offering
                )
                .exclude(state=marketplace_models.Resource.States.TERMINATED)
                .exists()
            ):
                raise exceptions.ValidationError(
                    {
                        "offerings": _(
                            "This organization has no resources of offering %s."
                        )
                        % offering.name
                    }
                )
        return offerings

    def _sync_offerings(self, instance, offerings):
        from waldur_mastermind.marketplace import models as marketplace_models

        scope_model = marketplace_models.AccessSubnetOfferingScope
        wanted = {offering.id for offering in offerings}
        existing = scope_model.objects.filter(access_subnet=instance)
        # Delete individually rather than with a bulk queryset delete so the
        # post_delete audit handler fires for each removed scope.
        for scope in existing.exclude(offering_id__in=wanted):
            scope.delete()
        present = set(existing.values_list("offering_id", flat=True))
        for offering in offerings:
            if offering.id not in present:
                scope_model.objects.create(access_subnet=instance, offering=offering)

    def validate(self, validated_data):
        if not self.instance:
            customer = validated_data["customer"]
            permission = PermissionEnum.CREATE_ACCESS_SUBNET

            if not has_permission(self.context["request"], permission, customer):
                raise exceptions.PermissionDenied()
        else:
            self.validate_staff_managed()
            customer = self.instance.customer

        if "offerings" in validated_data:
            self._resolve_offerings(customer, validated_data["offerings"])
        return validated_data

    def create(self, validated_data):
        offerings = validated_data.pop("offerings", None)
        instance = super().create(validated_data)
        if offerings is not None:
            self._sync_offerings(
                instance, self._resolve_offerings(instance.customer, offerings)
            )
        return instance

    def update(self, instance, validated_data):
        offerings = validated_data.pop("offerings", None)
        instance = super().update(instance, validated_data)
        if offerings is not None:
            self._sync_offerings(
                instance, self._resolve_offerings(instance.customer, offerings)
            )
        return instance


class AccessSubnetImpactAddressSerializer(serializers.Serializer):
    """One address that can reach a resource, and where it came from."""

    inet = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    # Provider defaults are published on the offering and are not editable by
    # the consumer, so an address it never added still needs explaining.
    source = serializers.ChoiceField(choices=["organization", "provider_default"])
    is_staff_managed = serializers.BooleanField()


class AccessSubnetImpactResourceSerializer(serializers.Serializer):
    """A resource and the addresses that may reach it.

    Only resources of offerings that opted into access subnets appear, so every
    row here has an allow-list that means something.
    """

    resource_uuid = serializers.CharField()
    resource_name = serializers.CharField()
    project_name = serializers.CharField()
    offering_uuid = serializers.CharField()
    offering_name = serializers.CharField()
    # False means the list is advisory: exported for an external firewall, but
    # Waldur itself does not act on it.
    concealment_enabled = serializers.BooleanField()
    # True when nothing restricts this resource, so it is reachable from
    # anywhere. This is the case the redesign exists to expose.
    unrestricted = serializers.BooleanField()
    addresses = AccessSubnetImpactAddressSerializer(many=True)
    packed = serializers.ListField(child=serializers.CharField())


class AccessSubnetImpactSerializer(serializers.Serializer):
    """Which of an organization's resources each access subnet reaches."""

    resources = AccessSubnetImpactResourceSerializer(many=True)


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


class ProjectEndDateChangeRequestCreateSerializer(serializers.ModelSerializer):
    project = serializers.HyperlinkedRelatedField(
        view_name="project-detail",
        lookup_field="uuid",
        queryset=models.Project.available_objects.all(),
    )
    state = serializers.CharField(read_only=True, source="get_state_display")
    uuid = serializers.UUIDField(read_only=True)

    class Meta:
        model = models.ProjectEndDateChangeRequest
        fields = ("project", "requested_end_date", "comment", "uuid", "state")

    def validate_project(self, project):
        user = self.context["request"].user
        if user.is_staff:
            raise serializers.ValidationError(
                _("Staff users should use project edit instead of creating a request.")
            )
        accessible = filter_queryset_for_user(
            models.Project.available_objects.filter(pk=project.pk),
            user,
        )
        if not accessible.exists():
            raise serializers.ValidationError(
                _("You don't have access to this project.")
            )
        if has_permission(
            user, PermissionEnum.UPDATE_PROJECT, project
        ) or has_permission(user, PermissionEnum.UPDATE_PROJECT, project.customer):
            raise serializers.ValidationError(
                _(
                    "You have permission to change project end date directly. "
                    "Use project edit instead of creating a request."
                )
            )
        return project

    def validate_requested_end_date(self, value):
        if value and value <= django_timezone.now().date():
            raise serializers.ValidationError(
                _("Requested end date must be in the future.")
            )
        return value

    def validate(self, attrs):
        project = attrs.get("project")
        requested_end_date = attrs.get("requested_end_date")
        if project and requested_end_date:
            if models.ProjectEndDateChangeRequest.objects.filter(
                project=project,
                requested_end_date=requested_end_date,
                state=ReviewStates.PENDING,
            ).exists():
                raise serializers.ValidationError(
                    _("A pending request for this project and end date already exists.")
                )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        validated_data["created_by"] = request.user
        validated_data["state"] = ReviewStates.PENDING
        return super().create(validated_data)


class ProjectEndDateChangeRequestSerializer(serializers.HyperlinkedModelSerializer):
    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.select_related("project__customer", "created_by", "reviewed_by")

    state = serializers.CharField(read_only=True, source="get_state_display")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.CharField(read_only=True, source="project.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="project.customer.name"
    )
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name", allow_null=True
    )
    created_by_uuid = serializers.UUIDField(
        read_only=True, source="created_by.uuid", allow_null=True
    )
    reviewed_by_full_name = serializers.CharField(
        read_only=True, source="reviewed_by.full_name", allow_null=True
    )
    reviewed_by_uuid = serializers.UUIDField(
        read_only=True, source="reviewed_by.uuid", allow_null=True
    )

    class Meta:
        model = models.ProjectEndDateChangeRequest
        fields = (
            "url",
            "uuid",
            "state",
            "project",
            "project_uuid",
            "project_name",
            "customer_uuid",
            "customer_name",
            "requested_end_date",
            "comment",
            "created",
            "created_by_uuid",
            "created_by_full_name",
            "reviewed_at",
            "reviewed_by_uuid",
            "reviewed_by_full_name",
            "review_comment",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "project-end-date-change-request-detail",
            },
            "project": {"lookup_field": "uuid", "view_name": "project-detail"},
        }


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


class UserListSerializer(serializers.ListSerializer):
    """Resolves permission scopes for the whole page before serializing it.

    See UserSerializer.prime_permission_scopes.
    """

    def to_representation(self, data):
        users = list(data)
        self.child.prime_permission_scopes(users)
        return super().to_representation(users)


class UserSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    nationalities = serializers.ListField(child=serializers.CharField(), required=False)
    managed_isds = serializers.ListField(child=serializers.CharField(), required=False)
    active_isds = serializers.ListField(child=serializers.CharField(), required=False)
    eduperson_assurance = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    affiliations = serializers.ListField(child=serializers.CharField(), required=False)
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
    has_usable_password = serializers.SerializerMethodField()
    has_passkey = serializers.SerializerMethodField()
    passkey_count = serializers.SerializerMethodField()
    ip_address = serializers.CharField(read_only=True, required=False, allow_null=True)
    birth_date = serializers.DateField(required=False, allow_null=True)
    should_protect_user_details = serializers.BooleanField(read_only=True)

    # Populated per page by UserListSerializer; None for a single instance.
    _page_scope_cache = None
    _page_perms_cache = None

    @extend_schema_field(PermissionSerializer(many=True))
    def get_permissions(self, user: core_models.User):
        return self._serialize_permissions(user, PermissionSerializer)

    def _get_user_permissions(self, user: core_models.User):
        # Use prefetched permissions if available (from UserViewSet.get_queryset)
        # to avoid N+1 queries. Fall back to query for backwards compatibility.
        if hasattr(user, "prefetched_permissions"):
            return list(user.prefetched_permissions)
        return list(
            UserRole.objects.filter(user=user, is_active=True).select_related(
                "user", "role", "created_by", "content_type"
            )
        )

    @staticmethod
    def _resolve_scopes(scope_ids_by_ct):
        """Load the permission scopes (Project, Customer) in one query per type."""
        scope_objects = {}
        for ct_id, obj_ids in scope_ids_by_ct.items():
            ct = ContentType.objects.get_for_id(ct_id)
            model_class = ct.model_class()
            if model_class is None:
                continue
            qs = model_class.objects.filter(id__in=obj_ids)
            if hasattr(model_class, "customer_id"):
                qs = qs.select_related("customer")
            for obj in qs:
                scope_objects[(ct_id, obj.id)] = obj
        return scope_objects

    def prime_permission_scopes(self, users):
        """Resolve the scopes of every user on this page in one pass.

        Scope loading was already batched, but only within a single user, so a
        page of 100 users cost 100 x (one query per scope type). Collecting the
        whole page first collapses that to one query per scope type.
        """
        perms_by_user = {}
        scope_ids_by_ct = {}
        for user in users:
            perms = self._get_user_permissions(user)
            perms_by_user[user.pk] = perms
            for perm in perms:
                if perm.content_type_id and perm.object_id:
                    scope_ids_by_ct.setdefault(perm.content_type_id, set()).add(
                        perm.object_id
                    )
        self._page_perms_cache = perms_by_user
        self._page_scope_cache = self._resolve_scopes(scope_ids_by_ct)

    def _serialize_permissions(self, user: core_models.User, serializer_class):
        if self._page_perms_cache is not None and user.pk in self._page_perms_cache:
            perms = self._page_perms_cache[user.pk]
            scope_objects = self._page_scope_cache
        else:
            perms = self._get_user_permissions(user)
            scope_ids_by_ct = {}
            for perm in perms:
                if perm.content_type_id and perm.object_id:
                    scope_ids_by_ct.setdefault(perm.content_type_id, []).append(
                        perm.object_id
                    )
            scope_objects = self._resolve_scopes(scope_ids_by_ct)

        valid_perms = []
        for perm in perms:
            scope = scope_objects.get((perm.content_type_id, perm.object_id))
            if scope is not None:
                perm.scope = scope
                valid_perms.append(perm)

        serializer = serializer_class(instance=valid_perms, many=True)
        return serializer.data

    def get_requested_email(self, user: core_models.User) -> str | None:
        try:
            return user.changeemailrequest.email
        except core_models.ChangeEmailRequest.DoesNotExist:
            return None

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

    def get_has_usable_password(self, user: core_models.User) -> bool:
        return user.has_usable_password()

    def get_has_passkey(self, user: core_models.User) -> bool:
        return self.get_passkey_count(user) > 0

    def get_passkey_count(self, user: core_models.User) -> int:
        # Reported as 0 rather than hidden when passkeys are disabled, so the
        # frontend has one shape to render regardless of deployment config.
        if not passkey_policy.is_enabled():
            return 0
        # UserViewSet annotates this for the list, where counting per row would
        # be an N+1. Single-instance use (e.g. /api/users/me) falls through to
        # one query, which is what it would have cost anyway.
        annotated = getattr(user, "active_passkey_count", None)
        if annotated is not None:
            return annotated
        return user.passkey_credentials.filter(is_active=True).count()

    def get_token_expires_at(self, user: core_models.User) -> None | datetime:
        if hasattr(user, "auth_token") and user.auth_token and user.token_lifetime:
            return user.auth_token.created + timezone.timedelta(
                seconds=user.token_lifetime
            )

    class Meta:
        model = core_models.User
        list_serializer_class = UserListSerializer
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
            "birth_date",
            "identity_provider_name",
            "identity_provider_label",
            "identity_provider_management_url",
            "identity_provider_fields",
            "image",
            "identity_source",
            "should_protect_user_details",
            "has_active_session",
            "has_usable_password",
            "has_passkey",
            "passkey_count",
            "ip_address",
            # User profile attributes
            "gender",
            "personal_title",
            "place_of_birth",
            "address",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "organization_registry_code",
            "organization_vat_code",
            "organization_address",
            "eduperson_assurance",
            # POSIX identity from the identity provider (read-only)
            "uid_number",
            "primary_gid",
            # Identity Bridge fields (staff-only, see get_fields)
            "is_identity_manager",
            "can_use_personal_access_tokens",
            "attribute_sources",
            "managed_isds",
            "active_isds",
            "deactivation_reason",
            "is_admin_deactivated",
        )
        read_only_fields = (
            "uuid",
            "civil_number",
            "registration_method",
            "date_joined",
            "agreement_date",
            "affiliations",
            "identity_source",
            "should_protect_user_details",
            "has_active_session",
            "has_usable_password",
            "has_passkey",
            "passkey_count",
            "attribute_sources",
            "active_isds",
            "is_admin_deactivated",
            "uid_number",
            "primary_gid",
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
                "deactivation_reason",
                "is_admin_deactivated",
            )
            # Identity Bridge fields visible to staff and to the user themselves
            self_visible_fields = (
                "managed_isds",
                "active_isds",
                "is_identity_manager",
                "can_use_personal_access_tokens",
            )
            staff_only_fields = (
                "attribute_sources",
                "has_usable_password",
            )
            is_own_profile = self._can_see_token(user)
            for field in staff_only_fields:
                if field not in fields:
                    continue
                if is_own_profile and field == "has_usable_password":
                    fields[field].read_only = True
                else:
                    del fields[field]
            if is_own_profile:
                for field in self_visible_fields:
                    if field in fields:
                        fields[field].read_only = True
            else:
                for field in self_visible_fields:
                    if field in fields:
                        del fields[field]
            if user.is_support:
                for field in protected_fields:
                    if field in fields:
                        fields[field].read_only = True
            else:
                for field in protected_fields:
                    if field not in fields:
                        continue
                    if is_own_profile and field == "has_active_session":
                        fields[field].read_only = True
                    else:
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

            if user.should_protect_user_details:
                detail_fields = (
                    "full_name",
                    "native_name",
                    "job_title",
                    "email",
                    "phone_number",
                    "organization",
                    # User profile attributes
                    "gender",
                    "personal_title",
                    "place_of_birth",
                    "address",
                    "country_of_residence",
                    "nationality",
                    "nationalities",
                    "organization_country",
                    "organization_type",
                    "organization_registry_code",
                    "eduperson_assurance",
                )
                for field in detail_fields:
                    if field in fields:
                        fields[field].read_only = True

        return fields

    def _can_see_token(self, user):
        # Nobody apart from the user herself can see her token.
        # User can see the token either via details view or /api/users/me

        if isinstance(self.instance, list) and len(self.instance) == 1:
            is_self = self.instance[0] == user
        else:
            is_self = self.instance == user

        if not is_self:
            return False

        # `user` here is request.user, which impersonation has already
        # replaced with the impersonated account — so "her own token" reads as
        # true for an impersonator viewing /api/users/me, and hands them a
        # durable credential for somebody else. Impersonation is meant to let
        # staff see what a user sees, not to walk away with their token.
        request = self.context.get("request")
        if request is not None and getattr(request.user, "impersonator", None):
            return False

        return True

    def _is_staff_editing_other_user(self):
        try:
            request = self.context["request"]
            return (
                request.user.is_staff
                and self.instance
                and request.user != self.instance
            )
        except (KeyError, AttributeError):
            return False

    def validate(self, attrs):
        agree_with_policy = attrs.pop("agree_with_policy", False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                if (
                    (
                        self.instance.is_active
                        and "is_active" in attrs.keys()
                        and not attrs["is_active"]
                        and len(set(attrs.keys()) - {"deactivation_reason"}) == 1
                    )
                    or self.instance.is_staff
                    or self._is_staff_editing_other_user()
                ):
                    # Deactivation of user, staff editing own profile,
                    # or staff editing another user.
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

        # Auto-populate deactivation_reason on manual deactivation,
        # and clear it on reactivation
        if self.instance and "is_active" in attrs:
            request = self.context.get("request")
            request_user = request.user if request else None
            if not attrs["is_active"] and self.instance.is_active:
                if not attrs.get("deactivation_reason"):
                    actor = request_user.username if request_user else "unknown"
                    attrs["deactivation_reason"] = f"Manually deactivated by {actor}"
                # Mark as an administrative override so the role-sync task does
                # not automatically revive the user.
                attrs["is_admin_deactivated"] = True
            elif attrs["is_active"] and not self.instance.is_active:
                attrs["deactivation_reason"] = ""
                attrs["is_admin_deactivated"] = False

        if "full_name" in attrs and "first_name" in attrs:
            raise serializers.ValidationError(
                {"first_name": _("Cannot specify first name with full name")}
            )
        elif "full_name" in attrs and "last_name" in attrs:
            raise serializers.ValidationError(
                {"last_name": _("Cannot specify last name with full name")}
            )

        organization_vat_code = attrs.get("organization_vat_code")
        if organization_vat_code:
            from waldur_core.structure.models import VATMixin

            if not VATMixin.validate_vat_format(organization_vat_code):
                raise serializers.ValidationError(
                    {"organization_vat_code": _("VAT number has invalid format.")}
                )

        # Convert validation error from Django to DRF
        # https://github.com/tomchristie/django-rest-framework/issues/2145
        try:
            user = core_models.User(id=getattr(self.instance, "id", None), **attrs)
            user.clean()

        except django_exceptions.ValidationError as error:
            raise exceptions.ValidationError(error.message_dict)
        return attrs

    # Personal data fields to track for GDPR data access logging.
    # Technical fields (url, uuid, token, is_staff, image, etc.) are excluded.
    PERSONAL_DATA_FIELDS = frozenset(
        [
            "username",
            "full_name",
            "native_name",
            "first_name",
            "last_name",
            "job_title",
            "email",
            "phone_number",
            "organization",
            "civil_number",
            "affiliations",
            "birth_date",
            "gender",
            "personal_title",
            "place_of_birth",
            "address",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "organization_registry_code",
            "organization_vat_code",
            "organization_address",
            "eduperson_assurance",
        ]
    )

    def to_representation(self, instance):
        """
        Override to log user data access for GDPR compliance.
        """
        data = super().to_representation(instance)

        request = self.context.get("request")
        if request and hasattr(request, "user") and request.user.is_authenticated:
            view = self.context.get("view")
            # Skip logging during schema generation
            if not getattr(view, "swagger_fake_view", False):
                # Only log access to personal data fields, not technical fields
                personal_fields_accessed = [
                    field for field in data.keys() if field in self.PERSONAL_DATA_FIELDS
                ]
                if personal_fields_accessed:
                    # Buffer entries for bulk insert when serializing multiple users
                    is_list_context = isinstance(
                        self.parent, serializers.ListSerializer
                    )
                    if is_list_context and view is not None:
                        if not hasattr(view, "_data_access_log_entries"):
                            view._data_access_log_entries = []
                        view._data_access_log_entries.append(
                            {
                                "target_user": instance,
                                "accessed_fields": personal_fields_accessed,
                            }
                        )
                    elif not is_list_context:
                        log_user_data_access_sync(
                            target_user=instance,
                            accessor=request.user,
                            request=request,
                            accessed_fields=personal_fields_accessed,
                        )

        return data


class UserEmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ScimSyncAllResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class ScimPullAttributesResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    changed_fields = serializers.ListField(
        child=serializers.CharField(), required=False
    )


class ProfileCompletenessSerializer(serializers.Serializer):
    """Serializer for profile completeness check response."""

    is_complete = serializers.BooleanField(
        help_text="Whether all mandatory profile fields are filled."
    )
    missing_fields = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of mandatory fields that are missing.",
    )
    mandatory_fields = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of all mandatory fields.",
    )
    enforcement_enabled = serializers.BooleanField(
        help_text="Whether enforcement of mandatory attributes is enabled.",
    )


class UserMeSerializer(UserSerializer):
    ip_address = serializers.CharField(read_only=True)
    profile_completeness = ProfileCompletenessSerializer(read_only=True)

    @extend_schema_field(MePermissionSerializer(many=True))
    def get_permissions(self, user: core_models.User):
        # The me endpoint describes the current user, so it only needs the
        # trimmed permission projection (see MePermissionSerializer).
        return self._serialize_permissions(user, MePermissionSerializer)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + (
            "ip_address",
            "profile_completeness",
        )


class UserActiveStatusCountSerializer(serializers.Serializer):
    """Serializer for user active status counts."""

    status = serializers.CharField()
    count = serializers.IntegerField()


class UserLanguageCountSerializer(serializers.Serializer):
    """Serializer for user language distribution counts."""

    language = serializers.CharField()
    count = serializers.IntegerField()


class UserRegistrationTrendSerializer(serializers.Serializer):
    """Serializer for user registration trends by month."""

    month = serializers.CharField()
    count = serializers.IntegerField()


class AttributeSourceDetailSerializer(serializers.Serializer):
    """Serializer for a single attribute source entry with staleness info."""

    source = serializers.CharField()
    timestamp = serializers.CharField()
    age_days = serializers.FloatField()
    is_stale = serializers.BooleanField()


class IdentityBridgeUserStatusSerializer(serializers.Serializer):
    """Serializer for per-user identity bridge diagnostic info."""

    active_isds = serializers.ListField(child=serializers.CharField())
    managed_isds = serializers.ListField(child=serializers.CharField())
    attribute_sources = serializers.DictField(
        child=AttributeSourceDetailSerializer(),
    )
    stale_attributes = serializers.ListField(child=serializers.CharField())
    effective_bridge_fields = serializers.ListField(child=serializers.CharField())
    is_federated = serializers.BooleanField()


class ISDUserCountSerializer(serializers.Serializer):
    """Serializer for per-ISD user count."""

    isd = serializers.CharField()
    user_count = serializers.IntegerField()
    stale_user_count = serializers.IntegerField()
    oldest_sync = serializers.CharField(allow_null=True)


class IdentityManagerSerializer(serializers.Serializer):
    """Serializer for identity manager info in bridge stats."""

    uuid = serializers.UUIDField()
    full_name = serializers.CharField()
    managed_isds = serializers.ListField(child=serializers.CharField())


class IdentityBridgeStatsSerializer(serializers.Serializer):
    """Serializer for system-wide identity bridge statistics."""

    enabled = serializers.BooleanField()
    deactivation_policy = serializers.CharField()
    allowed_attributes = serializers.ListField(child=serializers.CharField())
    total_federated_users = serializers.IntegerField()
    total_active_federated_users = serializers.IntegerField()
    users_per_isd = ISDUserCountSerializer(many=True)
    stale_threshold_days = serializers.IntegerField()
    identity_managers = IdentityManagerSerializer(many=True)


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

        # Validate key type against allowlist
        key_type = value.split()[0]
        allowed_types = config.SSH_KEY_ALLOWED_TYPES
        if allowed_types and key_type not in allowed_types:
            raise serializers.ValidationError(
                _(
                    "Key type '%(key_type)s' is not allowed. "
                    "Allowed types: %(allowed_types)s."
                )
                % {
                    "key_type": key_type,
                    "allowed_types": ", ".join(allowed_types),
                }
            )

        # Validate RSA key size
        min_rsa_size = config.SSH_KEY_MIN_RSA_KEY_SIZE
        if key_type == "ssh-rsa" and min_rsa_size > 0:
            try:
                parsed_key = load_ssh_public_key(value.encode("utf-8"))
                if parsed_key.key_size < min_rsa_size:
                    raise serializers.ValidationError(
                        _(
                            "RSA key size %(key_size)s bits is too small. "
                            "Minimum required: %(min_size)s bits."
                        )
                        % {
                            "key_size": parsed_key.key_size,
                            "min_size": min_rsa_size,
                        }
                    )
            except (ValueError, Exception) as e:
                if isinstance(e, serializers.ValidationError):
                    raise
                raise serializers.ValidationError(
                    _("Key is not valid: cannot parse RSA key.")
                )

        return value


class MoveProjectSerializer(serializers.Serializer):
    customer = serializers.HyperlinkedRelatedField(
        queryset=models.Customer.objects.all(),
        view_name="customer-detail",
        lookup_field="uuid",
    )
    preserve_permissions = serializers.BooleanField(required=True)


class ProjectRecoverySerializer(serializers.Serializer):
    restore_team_members = serializers.BooleanField(
        default=False,
        help_text=_(
            "Whether to automatically restore team members who had access before project deletion (staff only)"
        ),
    )
    send_invitations_to_previous_members = serializers.BooleanField(
        default=False,
        help_text=_(
            "Whether to send invitations to users who had access before project deletion"
        ),
    )
    end_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text=_("End date for the recovered project"),
    )

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

    def validate(self, data):
        request = self.context.get("request")

        # Check mutual exclusion
        if data.get("restore_team_members") and data.get(
            "send_invitations_to_previous_members"
        ):
            raise serializers.ValidationError(
                _(
                    "Cannot both restore team members and send invitations. Choose one approach."
                )
            )

        # Check staff-only permission for automatic restoration
        if data.get("restore_team_members") and request and not request.user.is_staff:
            raise serializers.ValidationError(
                _(
                    "Only staff users can automatically restore team members. Use send_invitations_to_previous_members instead."
                )
            )

        return data


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
        choices=[(v, k) for k, v in CoreStates.choices],
        choice_mappings={v: k for k, v in CoreStates.choices},
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
    customer_uuid = serializers.UUIDField(
        source="project.customer.uuid", read_only=True
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

    def validate_project(self, project: models.Project) -> models.Project:
        """Validate that the project is not soft-deleted."""
        if project.is_removed:
            raise serializers.ValidationError(
                _("Cannot create resources for terminated projects.")
            )
        return project

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
            "customer_uuid",
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

    @extend_schema_field(serializers.ChoiceField(choices=CoreStates.labels))
    def get_state(self, obj):
        return obj.get_state_display()

    def get_filtered_field_names(self):
        return ("project", "service_settings")

    def get_resource_type(self, obj) -> str:
        return get_resource_type(obj)

    def get_resource_fields(self) -> list[str]:
        return [f.name for f in self.Meta.model._meta.get_fields()]

    # an optional generic URL for accessing a resource
    def get_access_url(self, obj) -> list[str] | str | None:
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
        fields = (
            "url",
            "uuid",
            "content",
            "agreement_type",
            "language",
            "created",
            "modified",
        )
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
        return obj.content or None

    def get_original_content(self, obj) -> str | None:
        return template_utils.get_original_content(obj.path)

    def get_is_content_overridden(self, obj) -> bool:
        return template_utils.is_template_overridden(obj)


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
    """Metadata about another user's token, deliberately without the key.

    This backs staff-only endpoints, and it used to return the raw key. That
    made a single compromised staff password enough to obtain a durable,
    passkey-free session as any user in the deployment — no second factor
    could mean anything while it stood, because the credential could simply be
    read out. The remaining fields answer the operational questions the
    endpoints exist for (does the user have a session, how old is it) without
    handing over the credential itself.
    """

    class Meta:
        model = authtoken_models.Token
        fields = (
            "created",
            "user_first_name",
            "user_last_name",
            "user_username",
            "user_is_active",
            "user_token_lifetime",
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


class AvailableProjectDigestSectionSerializer(serializers.Serializer):
    key = serializers.CharField()
    title = serializers.CharField()


class ProjectDigestConfigSerializer(serializers.ModelSerializer):
    available_sections = serializers.SerializerMethodField()
    enabled_sections = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )

    class Meta:
        model = models.ProjectDigestConfiguration
        fields = [
            "uuid",
            "is_enabled",
            "frequency",
            "enabled_sections",
            "day_of_week",
            "day_of_month",
            "last_sent_at",
            "available_sections",
        ]
        read_only_fields = ["uuid", "last_sent_at", "available_sections"]

    @extend_schema_field(AvailableProjectDigestSectionSerializer(many=True))
    def get_available_sections(self, obj):
        from waldur_core.structure.digest_providers import get_available_providers

        return [
            {"key": p.section_key, "title": p.section_title}
            for p in get_available_providers()
        ]

    def validate_enabled_sections(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError(_("Must be a list of section keys."))
        from waldur_core.structure.digest_providers import get_all_providers

        valid_keys = set(get_all_providers().keys())
        invalid = [k for k in value if k not in valid_keys]
        if invalid:
            raise serializers.ValidationError(
                _("Unknown section keys: %(keys)s") % {"keys": ", ".join(invalid)}
            )
        return value


class ProjectDigestPreviewSerializer(serializers.Serializer):
    project_uuid = serializers.UUIDField()


class ProjectDigestPreviewResponseSerializer(serializers.Serializer):
    subject = serializers.CharField()
    html_body = serializers.CharField()
    text_body = serializers.CharField()


class SetErredSerializer(serializers.Serializer):
    error_message = serializers.CharField(required=False, allow_blank=True, default="")
    error_traceback = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class DashboardGeneralStatsSerializer(serializers.Serializer):
    pending_permission_requests = serializers.IntegerField(read_only=True)
    active_invitations = serializers.IntegerField(read_only=True)
    pending_onboarding_applications = serializers.IntegerField(read_only=True)


class DashboardPendingActionSerializer(serializers.Serializer):
    type = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    variant = serializers.ChoiceField(
        read_only=True, choices=["info", "warning", "error"]
    )
    deadline = serializers.DateTimeField(read_only=True, allow_null=True)
    count = serializers.IntegerField(read_only=True, allow_null=True)
    # The object the action is about (invoice, offering, ...) and the customer
    # it belongs to, so the frontend can deep-link: an invoice detail page
    # needs both the organisation and the invoice uuid. Route names live in
    # the frontend, so the feed carries identifiers rather than URLs.
    target_uuid = serializers.UUIDField(read_only=True, allow_null=True)
    customer_uuid = serializers.UUIDField(read_only=True, allow_null=True)
