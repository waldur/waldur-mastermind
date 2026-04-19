from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from waldur_autoprovisioning import models
from waldur_core.core import serializers as core_serializers
from waldur_core.permissions.models import Role
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.fields import PublicPlanField


class RuleSerializer(
    core_serializers.UserEmailPatternsValidatorMixin,
    serializers.HyperlinkedModelSerializer,
):
    project_role = serializers.HyperlinkedRelatedField(
        queryset=Role.objects.all(),
        view_name="role-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_uuid = serializers.CharField(source="customer.uuid", read_only=True)

    project_role_description = serializers.CharField(
        source="project_role.description", read_only=True
    )
    project_role_name = serializers.CharField(
        required=False, allow_null=True, write_only=True
    )
    project_role_display_name = serializers.CharField(
        source="project_role.name", read_only=True
    )
    plan = PublicPlanField(
        lookup_field="uuid",
        lookup_url_kwarg="plan_uuid",
        view_name="marketplace-public-offering-plan-detail",
        queryset=marketplace_models.Plan.objects.all(),
        required=False,
        allow_null=True,
    )
    plan_attributes = serializers.DictField(
        required=False,
        default=dict,
    )
    plan_limits = serializers.DictField(
        required=False,
        default=dict,
    )
    user_affiliations = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_email_patterns = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    plan_name = serializers.CharField(
        source="plan.name", required=False, read_only=True
    )
    offering_name = serializers.CharField(
        source="plan.offering.name", required=False, read_only=True
    )
    offering_uuid = serializers.UUIDField(
        source="plan.offering.uuid", required=False, read_only=True
    )
    category_title = serializers.CharField(
        source="plan.offering.category.title", required=False, read_only=True
    )
    category_url = serializers.HyperlinkedRelatedField(
        source="plan.offering.category",
        view_name="marketplace-category-detail",
        lookup_field="uuid",
        read_only=True,
    )

    class Meta:
        model = models.Rule
        fields = (
            "name",
            "uuid",
            "url",
            "user_affiliations",
            "user_email_patterns",
            "customer",
            "customer_name",
            "customer_uuid",
            "use_user_organization_as_customer_name",
            "project_role",
            "project_role_name",  # used for accepting role name to set
            "project_role_display_name",  # used for displaying the role name
            "project_role_description",
            "plan",
            "plan_attributes",
            "plan_limits",
            "plan_name",
            "offering_name",
            "offering_uuid",
            "category_title",
            "category_url",
        )
        extra_kwargs = {
            "url": {
                "view_name": "autoprovisioning-rule-detail",
                "lookup_field": "uuid",
            },
            "customer": {
                "view_name": "customer-detail",
                "lookup_field": "uuid",
            },
            "plan": {
                "view_name": "marketplace-plan-detail",
                "lookup_field": "uuid",
            },
        }

    def validate(self, attrs):
        project_role = attrs.get("project_role")
        project_role_name = attrs.get("project_role_name")
        # Compose values considering update vs create
        instance = getattr(self, "instance", None)
        customer = attrs.get("customer", getattr(instance, "customer", None))
        use_org_as_customer = attrs.get(
            "use_user_organization_as_customer_name",
            getattr(instance, "use_user_organization_as_customer_name", False),
        )

        # Treat empty string as None for project_role_name
        if project_role_name == "":
            project_role_name = None
            attrs.pop("project_role_name", None)

        # Check that exactly one of project_role or project_role_name is provided
        if project_role and project_role_name:
            raise serializers.ValidationError(
                "Cannot specify both project_role and project_role_name. Choose one."
            )

        # Require at least one role specification for both creation and updates
        if not project_role and not project_role_name:
            raise serializers.ValidationError(
                "Either project_role or project_role_name must be provided."
            )

        # Either explicit customer must be set or we must take customer from user's organization
        if not customer and not use_org_as_customer:
            raise serializers.ValidationError(
                "Either customer must be specified or use_user_organization_as_customer_name must be true."
            )

        if (
            project_role
            and project_role.content_type
            != ContentType.objects.get_by_natural_key("structure", "project")
        ):
            raise serializers.ValidationError(
                "The specified role is not a valid project role."
            )

        # If project_role_name is provided, look up the role by name
        if project_role_name:
            try:
                role = Role.objects.get(name=project_role_name)
                attrs["project_role"] = role
            except Role.DoesNotExist:
                raise serializers.ValidationError(
                    f"Project role with name '{project_role_name}' does not exist."
                )
            # Remove project_role_name from attrs as it's not a model field
            attrs.pop("project_role_name", None)

        return attrs
