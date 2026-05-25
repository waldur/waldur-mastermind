import logging

from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers as rf_serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.permissions import serializers as permissions_serializers
from waldur_core.structure import models as structure_models
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.permissions import _has_admin_access
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers

from . import models

logger = logging.getLogger(__name__)


class OpenPortalServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ("instance_name", "project_template")

    instance_name = rf_serializers.CharField(
        source="options.instance_name",
        label=_("Full path name for the OpenPortal Agent managing this instance"),
        default=None,
        required=False,
    )

    project_template = rf_serializers.CharField(
        source="options.project_template",
        label=_("Class for projects created on the remote OpenPortal instance"),
        default=None,
        required=False,
    )

    allocation_unit = rf_serializers.CharField(
        source="options.allocation_unit",
        label=_("Unit for allocation limits"),
        default="NHR",
        required=False,
    )

    default_allocation = rf_serializers.FloatField(
        source="options.default_allocation",
        label=_("Default allocation for new projects on this resource"),
        default=None,
        required=False,
    )

    max_allocation = rf_serializers.FloatField(
        source="options.max_allocation",
        label=_("Maximum allocation for new projects on this resource"),
        default=None,
        required=False,
    )


class AllocationSerializer(
    structure_serializers.BaseResourceSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Allocation
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "node_limit",
            "groupname",
            "node_usage",
            "is_active",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "node_usage",
                "is_active",
            )
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openportal-allocation-detail"},
            node_limit={"validators": [MinValueValidator(0)]},
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Skip validation on update
        if self.instance:
            return attrs

        project = attrs["project"]
        user = self.context["request"].user
        if not _has_admin_access(user, project):
            raise rf_exceptions.PermissionDenied(
                _("You do not have permissions to create allocation for given project.")
            )
        return attrs


class RemoteAllocationSerializer(
    structure_serializers.BaseResourceSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.RemoteAllocation
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "node_limit",
            "remote_project_identifier",
            "node_usage",
            "is_active",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "node_usage",
                "is_active",
            )
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "openportal-remote-allocation-detail",
            },
            node_limit={"validators": [MinValueValidator(0)]},
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Skip validation on update
        if self.instance:
            return attrs

        project = attrs["project"]
        user = self.context["request"].user
        if not _has_admin_access(user, project):
            raise rf_exceptions.PermissionDenied(
                _("You do not have permissions to create allocation for given project.")
            )
        return attrs


class AllocationSetLimitsSerializer(rf_serializers.ModelSerializer):
    node_limit = rf_serializers.IntegerField(min_value=-1)

    class Meta:
        model = models.Allocation
        fields = ["node_limit"]


class RemoteAllocationSetLimitsSerializer(rf_serializers.ModelSerializer):
    node_limit = rf_serializers.IntegerField(min_value=-1)

    class Meta:
        model = models.RemoteAllocation
        fields = ["node_limit"]


class AllocationUserUsageCreateSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.AllocationUserUsage
        fields = (
            "node_usage",
            "user",
            "username",
        )
        extra_kwargs = {
            "user": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }


class AllocationUserUsageSerializer(rf_serializers.HyperlinkedModelSerializer):
    full_name = rf_serializers.ReadOnlyField(source="user.full_name")

    class Meta:
        model = models.AllocationUserUsage
        fields = (
            "node_usage",
            "month",
            "year",
            "allocation",
            "user",
            "username",
            "full_name",
        )
        extra_kwargs = {
            "allocation": {
                "lookup_field": "uuid",
                "view_name": "openportal-allocation-detail",
            },
            "user": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }


class AssociationSerializer(rf_serializers.HyperlinkedModelSerializer):
    allocation = rf_serializers.HyperlinkedRelatedField(
        queryset=models.Association.objects.all(),
        view_name="openportal-allocation-detail",
        lookup_field="uuid",
    )

    class Meta:
        model = models.Association
        fields = (
            "uuid",
            "username",
            "groupname",
            "useridentifier",
            "allocation",
        )


class RemoteAssociationSerializer(rf_serializers.HyperlinkedModelSerializer):
    allocation = rf_serializers.HyperlinkedRelatedField(
        queryset=models.RemoteAssociation.objects.all(),
        view_name="openportal-remote-allocation-detail",
        lookup_field="uuid",
    )

    class Meta:
        model = models.RemoteAssociation
        fields = (
            "uuid",
            "allocation",
        )


class HistoricalAllocationSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.HistoricalAllocation
        fields = ("node_usage", "month", "year", "allocation", "is_complete")
        extra_kwargs = {
            "allocation": {
                "lookup_field": "uuid",
                "view_name": "openportal-allocation-detail",
            },
        }


class HistoricalRemoteAllocationSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.HistoricalRemoteAllocation
        fields = ("node_usage", "month", "year", "allocation", "is_complete")
        extra_kwargs = {
            "allocation": {
                "lookup_field": "uuid",
                "view_name": "openportal-allocation-detail",
            },
        }


class UsageSerializer(rf_serializers.Serializer):
    seconds = rf_serializers.IntegerField()


class DailyProjectUsageReportSerializer(rf_serializers.Serializer):
    reports = rf_serializers.DictField(
        child=UsageSerializer(), help_text="local_username → Usage"
    )
    components = rf_serializers.DictField(
        child=rf_serializers.DictField(child=UsageSerializer()),
        required=False,
        help_text='component_name → local_username → Usage. e.g. { "cpu": { "chris.aiproject": { "seconds": 41055 } } }',
    )
    user_job_counts = rf_serializers.DictField(
        child=rf_serializers.IntegerField(),
        required=False,
        help_text="local_username → job count",
    )
    user_wait_seconds = rf_serializers.DictField(
        child=rf_serializers.IntegerField(),
        required=False,
        help_text="local_username → wait seconds",
    )
    num_jobs = rf_serializers.IntegerField(required=False)
    total_wait_seconds = rf_serializers.IntegerField(required=False)
    is_complete = rf_serializers.BooleanField()


class ProjectUsageReportSerializer(rf_serializers.Serializer):
    project = rf_serializers.CharField(
        help_text='ProjectIdentifier string e.g. "aiproject.brics"'
    )
    reports = rf_serializers.DictField(
        child=DailyProjectUsageReportSerializer(),
        help_text='"YYYY-MM-DD" → DailyProjectUsageReportJson',
    )
    users = rf_serializers.DictField(
        child=rf_serializers.CharField(),
        help_text='UserIdentifier → local_username. e.g. { "chris.aiproject.brics": "chris.aiproject" }',
    )


class OpenPortalQuotaSerializer(rf_serializers.Serializer):
    limit = rf_serializers.CharField(
        help_text='Size limit. "unlimited" or a size string e.g. "1024.00 GB"'
    )
    usage = rf_serializers.CharField(
        required=False,
        help_text='Size usage e.g. "24.00 KB". Absent when the server has no usage data.',
    )


class DailyStorageReportSerializer(rf_serializers.Serializer):
    project = rf_serializers.CharField()
    generated_at = rf_serializers.CharField(help_text="RFC3339 timestamp")
    project_quotas = rf_serializers.DictField(
        child=OpenPortalQuotaSerializer(), help_text="Volume → Quota"
    )
    user_quotas = rf_serializers.DictField(
        child=rf_serializers.DictField(child=OpenPortalQuotaSerializer()),
        help_text="UserIdentifier → (Volume → Quota)",
    )


class ProjectStorageReportSerializer(rf_serializers.Serializer):
    project = rf_serializers.CharField()
    generated_at = rf_serializers.CharField(help_text="RFC3339 timestamp")
    project_quotas = rf_serializers.DictField(
        child=OpenPortalQuotaSerializer(), help_text="Volume → Quota"
    )
    user_quotas = rf_serializers.DictField(
        child=rf_serializers.DictField(child=OpenPortalQuotaSerializer()),
        help_text="UserIdentifier → (Volume → Quota)",
    )
    users = rf_serializers.DictField(
        child=rf_serializers.CharField(), help_text="UserIdentifier → local_username"
    )
    daily_reports = rf_serializers.DictField(
        child=DailyStorageReportSerializer(),
        required=False,
        help_text='"YYYY-MM-DD" → DailyStorageReportJson. Absent from JSON when there are no daily snapshots.',
    )


@extend_schema_field(ProjectUsageReportSerializer)
class ProjectUsageReportField(rf_serializers.JSONField):
    pass


@extend_schema_field(ProjectStorageReportSerializer)
class ProjectStorageReportField(rf_serializers.JSONField):
    pass


class CachedProjectUsageReportSerializer(rf_serializers.ModelSerializer):
    report = ProjectUsageReportField()

    class Meta:
        model = models.CachedProjectUsageReport
        fields = (
            "id",
            "year",
            "month",
            "project_identifier",
            "resource",
            "is_complete",
            "report",
        )


class CachedProjectStorageReportSerializer(rf_serializers.ModelSerializer):
    report = ProjectStorageReportField()

    class Meta:
        model = models.CachedProjectStorageReport
        fields = (
            "id",
            "year",
            "month",
            "project_identifier",
            "resource",
            "report",
        )


class UserInfoSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.UserInfo
        fields = (
            "shortname",
            "user",
        )
        extra_kwargs = {
            "user": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
            },
        }


class ProjectInfoSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ProjectInfo
        fields = (
            "project",
            "shortname",
            "allowed_destinations",
        )
        extra_kwargs = {
            "project": {
                "lookup_field": "uuid",
                "view_name": "project-detail",
            },
        }


class ProjectTemplateSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    rf_serializers.ModelSerializer,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Handle permission filtering for many-to-many fields
        if hasattr(self, "context") and "request" in self.context:
            user = self.context["request"].user
            # Access the child field of the ManyRelatedField to set the queryset
            if hasattr(self.fields["offerings"], "child"):
                self.fields["offerings"].child.queryset = filter_queryset_for_user(
                    marketplace_models.Offering.objects.all(), user
                )

    def get_fields(self):
        fields = rf_serializers.ModelSerializer.get_fields(self)

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        # Skip filtering during creation to avoid blocking valid relationships
        if hasattr(self, "instance") and self.instance is None:
            # This is a creation operation, be more permissive
            return fields

        for field_name in self.get_filtered_field_names():
            if field_name not in fields:  # field could be not required by user
                continue
            field = fields[field_name]

            # Handle ManyRelatedField (many=True relationships)
            if hasattr(field, "child") and hasattr(field.child, "queryset"):
                field.child.queryset = filter_queryset_for_user(
                    field.child.queryset, user
                )
            # Handle regular fields with queryset
            elif hasattr(field, "queryset"):
                field.queryset = filter_queryset_for_user(field.queryset, user)

        return fields

    def filter_field_queryset(self, field, queryset, field_name):
        """Override to handle ManyRelatedField properly"""
        # Check if this is a ManyRelatedField (many=True relationship)
        if hasattr(field, "child") and hasattr(field.child, "queryset"):
            # Filter the child field's queryset instead
            if hasattr(self, "context") and "request" in self.context:
                user = self.context["request"].user
                field.child.queryset = filter_queryset_for_user(
                    field.child.queryset, user
                )

            return field

        # Use the default behavior for other fields
        return super().filter_field_queryset(field, queryset, field_name)

    provider = rf_serializers.HyperlinkedRelatedField(
        queryset=structure_models.Customer.objects.all(),
        view_name="customer-detail",
        lookup_field="uuid",
    )

    provider_data = structure_serializers.BasicCustomerSerializer(
        source="provider", read_only=True
    )

    customer = rf_serializers.HyperlinkedRelatedField(
        queryset=structure_models.Customer.objects.all(),
        view_name="customer-detail",
        lookup_field="uuid",
    )

    customer_data = structure_serializers.BasicCustomerSerializer(
        source="customer", read_only=True
    )

    offerings = rf_serializers.HyperlinkedRelatedField(
        many=True,
        queryset=marketplace_models.Offering.objects.all(),
        view_name="marketplace-provider-offering-detail",
        lookup_field="uuid",
    )

    offerings_data = marketplace_serializers.ResourceOfferingSerializer(
        source="offerings", many=True, read_only=True
    )

    role_mapping_data = rf_serializers.SerializerMethodField()

    def get_role_mapping_data(self, obj) -> dict[str, dict[str, str]]:
        """
        Serialize the role mapping dictionary returned by get_role_mapping()
        """
        role_mapping = obj.get_role_mapping()
        if not role_mapping:
            return {}

        serialized_mapping = {}
        for key, role in role_mapping.items():
            serialized_mapping[key] = permissions_serializers.RoleDetailsSerializer(
                role
            ).data

        return serialized_mapping

    class Meta:
        model = models.ProjectTemplate
        fields = (
            "uuid",
            "name",
            "offering",
            "provider",
            "provider_data",
            "portal",
            "key",
            "customer",
            "customer_data",
            "shortname",
            "offerings",
            "offerings_data",
            "approval_limit",
            "max_credit_limit",
            "allocation_units_mapping",
            "role_mapping",
            "role_mapping_data",
        )

        related_paths = ("provider", "customer", "offerings")

    def get_filtered_field_names(self):
        return ("provider", "customer", "offerings")


class ProjectAccountingSummarySerializer(rf_serializers.Serializer):
    """
    Read-only serializer for project accounting summaries.
    Data is derived from invoice items and project credits via get_project_spend_info.
    """

    project_uuid = rf_serializers.UUIDField(source="uuid", read_only=True)
    project_name = rf_serializers.CharField(source="name", read_only=True)
    customer_uuid = rf_serializers.UUIDField(source="customer.uuid", read_only=True)
    customer_name = rf_serializers.CharField(source="customer.name", read_only=True)
    start_date = rf_serializers.DateField(read_only=True, allow_null=True)
    end_date = rf_serializers.DateField(read_only=True, allow_null=True)
    total_credits = rf_serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True
    )
    total_spend = rf_serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True
    )
    current_month_spend = rf_serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True
    )

    def to_representation(self, project):
        import decimal

        from . import utils as openportal_utils

        data = {
            "project_uuid": str(project.uuid),
            "project_name": project.name,
            "customer_uuid": str(project.customer.uuid),
            "customer_name": project.customer.name,
            "start_date": (project.start_date or project.created.date()).isoformat(),
            "end_date": project.end_date.isoformat() if project.end_date else None,
        }

        try:
            (credits_no_current, spend_no_current) = (
                openportal_utils.get_project_spend_info(
                    project, include_current_month=False, silent=True
                )
            )
            (credits_with_current, spend_with_current) = (
                openportal_utils.get_project_spend_info(
                    project, include_current_month=True, silent=True
                )
            )
            data["total_credits"] = credits_with_current
            data["total_spend"] = spend_no_current
            data["current_month_spend"] = spend_with_current - spend_no_current
        except Exception as e:
            logger.error(f"Error computing spend info for project {project}: {e}")
            data["total_credits"] = decimal.Decimal(0)
            data["total_spend"] = decimal.Decimal(0)
            data["current_month_spend"] = decimal.Decimal(0)

        return data


class ProjectAttachSerializer(rf_serializers.Serializer):
    project_uuid = rf_serializers.UUIDField(
        help_text="UUID of the project to attach to this managed project"
    )

    def validate_project_uuid(self, value):
        """Validate that the project exists and is accessible"""
        try:
            structure_models.Project.objects.get(uuid=value)
            return value
        except structure_models.Project.DoesNotExist:
            raise rf_serializers.ValidationError(
                "Project with this UUID does not exist"
            )


class ManagedProjectSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    rf_serializers.ModelSerializer,
):
    state = rf_serializers.CharField(read_only=True, source="get_state_display")

    reviewed_by_full_name = rf_serializers.CharField(
        read_only=True, source="reviewed_by.full_name"
    )
    reviewed_by_uuid = rf_serializers.UUIDField(
        read_only=True, source="reviewed_by.uuid"
    )

    project = rf_serializers.HyperlinkedRelatedField(
        queryset=structure_models.Project.objects.all(),
        view_name="project-detail",
        lookup_field="uuid",
    )

    project_data = structure_serializers.BasicProjectSerializer(
        source="project", read_only=True
    )

    details = rf_serializers.JSONField(
        read_only=True,
        help_text=_("Details of the project as provided by the remote OpenPortal."),
    )

    project_template = rf_serializers.HyperlinkedRelatedField(
        queryset=models.ProjectTemplate.objects.all(),
        view_name="openportal-project-template-detail",
        lookup_field="uuid",
    )

    project_template_data = ProjectTemplateSerializer(
        source="project_template", read_only=True
    )

    class Meta:
        model = models.ManagedProject

        fields = (
            "state",
            "created",
            "reviewed_at",
            "reviewed_by_full_name",
            "reviewed_by_uuid",
            "review_comment",
            "identifier",
            "destination",
            "details",
            "project",
            "project_data",
            "project_template",
            "project_template_data",
            "local_identifier",
        )

        related_paths = ("project",)

    def get_filtered_field_names(self):
        return ("project",)


class AccessResourceSerializer(rf_serializers.Serializer):
    name = rf_serializers.CharField()
    username = rf_serializers.CharField()


class AccessProjectSerializer(rf_serializers.Serializer):
    name = rf_serializers.CharField()
    resources = AccessResourceSerializer(many=True)


class AccessResponseSerializer(rf_serializers.Serializer):
    email = rf_serializers.EmailField()
    status = rf_serializers.CharField()
    short_name = rf_serializers.CharField()
    projects = rf_serializers.DictField(child=AccessProjectSerializer())
    invited_by = rf_serializers.CharField(allow_blank=True)
    reason = rf_serializers.CharField(allow_blank=True)


class OfferingMappingSerializer(rf_serializers.Serializer):
    uuid = rf_serializers.CharField()
    name = rf_serializers.CharField()
    description = rf_serializers.CharField()
    slug = rf_serializers.CharField()


class ProjectMappingSerializer(rf_serializers.Serializer):
    uuid = rf_serializers.CharField()
    name = rf_serializers.CharField()
    customer_uuid = rf_serializers.CharField()
    customer_name = rf_serializers.CharField()


class UserMappingSerializer(rf_serializers.Serializer):
    uuid = rf_serializers.CharField()
    full_name = rf_serializers.CharField()
    email = rf_serializers.EmailField()
    username = rf_serializers.CharField()
