from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_autoprovisioning import models
from waldur_core.core import serializers as core_serializers
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
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
    customer_role = serializers.HyperlinkedRelatedField(
        queryset=Role.objects.all(),
        view_name="role-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )
    customer_role_display_name = serializers.CharField(
        source="customer_role.name", read_only=True
    )
    customer_role_description = serializers.CharField(
        source="customer_role.description", read_only=True
    )
    customer_role_name = serializers.CharField(
        required=False, allow_null=True, write_only=True
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
    # These four exist on the model and are honoured by the evaluator, but were
    # never exposed on the API — the dry-run dialog rendered filter rows nobody
    # could configure.
    user_identity_sources = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_nationalities = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_organization_types = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_assurance_levels = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    user_claims = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        default=dict,
        help_text='Identity provider claims the user must carry, as {"claim": '
        '["accepted", "values"]}. All claims must match; within one claim any '
        "value matches. A value ending in '*' matches by prefix.",
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
            "user_identity_sources",
            "user_nationalities",
            "user_organization_types",
            "user_assurance_levels",
            "user_claims",
            "customer",
            "customer_name",
            "customer_uuid",
            "use_user_organization_as_customer_name",
            "create_project",
            "revoke_when_unmatched",
            "project_role",
            "project_role_name",  # used for accepting role name to set
            "project_role_display_name",  # used for displaying the role name
            "project_role_description",
            "customer_role",
            "customer_role_name",  # used for accepting role name to set
            "customer_role_display_name",  # used for displaying the role name
            "customer_role_description",
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

    def _resolve_role(self, attrs, field: str, scope_model: str, label: str):
        """Normalise the ``<field>`` / ``<field>_name`` pair into a single Role.

        Returns the role that will be in effect after the write. Mutates
        ``attrs``: the write-only ``_name`` alias is dropped (it is not a model
        field), and an explicitly cleared role is written back as ``None`` —
        without that, unsetting a role over the API silently kept the old one.
        """
        name_key = f"{field}_name"
        instance = getattr(self, "instance", None)
        # Distinguishes "left alone" (absent) from "cleared" (present as null).
        provided = field in attrs or name_key in attrs

        role = attrs.get(field)
        role_name = attrs.get(name_key)
        attrs.pop(name_key, None)

        if role_name == "":
            role_name = None

        if role and role_name:
            raise serializers.ValidationError(
                f"Cannot specify both {field} and {name_key}. Choose one."
            )

        if role_name:
            try:
                role = Role.objects.get(name=role_name)
            except Role.DoesNotExist:
                raise serializers.ValidationError(
                    f"{label} with name '{role_name}' does not exist."
                )

        if role is not None:
            attrs[field] = role
        elif provided:
            attrs[field] = None
        else:
            role = getattr(instance, field, None)

        if role and role.content_type != ContentType.objects.get_by_natural_key(
            "structure", scope_model
        ):
            raise serializers.ValidationError(
                f"The specified role is not a valid {scope_model} role."
            )

        return role

    def validate_user_claims(self, value):
        for claim, accepted in (value or {}).items():
            if not claim or not claim.strip():
                raise serializers.ValidationError("Claim name cannot be empty.")
            if not accepted:
                raise serializers.ValidationError(
                    f"Claim '{claim}' must list at least one accepted value."
                )
            for entry in accepted:
                if not entry or not entry.strip():
                    raise serializers.ValidationError(
                        f"Claim '{claim}' has an empty accepted value."
                    )
                if entry.strip() == "*":
                    raise serializers.ValidationError(
                        f"Claim '{claim}' cannot accept a bare '*' — that would "
                        "match every value the claim carries."
                    )
        return value

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        customer = attrs.get("customer", getattr(instance, "customer", None))
        use_org_as_customer = attrs.get(
            "use_user_organization_as_customer_name",
            getattr(instance, "use_user_organization_as_customer_name", False),
        )
        create_project = attrs.get(
            "create_project", getattr(instance, "create_project", True)
        )

        project_role = self._resolve_role(
            attrs, "project_role", "project", "Project role"
        )
        customer_role = self._resolve_role(
            attrs, "customer_role", "customer", "Organization role"
        )

        # A rule must grant something. Before organization-level rules existed
        # this was "a project role is mandatory"; now either half suffices.
        if not project_role and not customer_role:
            raise serializers.ValidationError(
                "Either project_role or customer_role must be provided."
            )

        if not create_project and not customer_role:
            raise serializers.ValidationError(
                "A rule that does not create a project must specify a customer_role, "
                "otherwise it grants nothing."
            )

        # Either explicit customer must be set or we must take customer from user's organization
        if not customer and not use_org_as_customer:
            raise serializers.ValidationError(
                "Either customer must be specified or use_user_organization_as_customer_name must be true."
            )

        return attrs


class RuleTestMatchRequestSerializer(serializers.Serializer):
    """Input for the dry-run test-match action."""

    user_uuid = serializers.UUIDField(
        help_text="UUID of the user to evaluate this rule against.",
    )


class FilterCheckResultSerializer(serializers.Serializer):
    """Per-filter outcome of a rule evaluation."""

    name = serializers.CharField()
    configured = serializers.BooleanField()
    matched = serializers.BooleanField()
    user_value = serializers.JSONField(allow_null=True, required=False)
    rule_value = serializers.JSONField(allow_null=True, required=False)
    reason = serializers.CharField(allow_blank=True)


class CustomerCandidateSerializer(serializers.ModelSerializer):
    """Lightweight representation of a Customer that matched the user's organization."""

    url = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ("uuid", "name", "abbreviation", "url")

    def get_url(self, obj: Customer) -> str | None:
        request = self.context.get("request")
        if request is None:
            return None
        return reverse(
            "customer-detail", kwargs={"uuid": obj.uuid.hex}, request=request
        )


class RuleTestMatchResponseSerializer(serializers.Serializer):
    """Structured outcome of evaluating a rule against a user (dry-run)."""

    would_provision = serializers.BooleanField()
    block_reason = serializers.CharField(allow_blank=True)
    user_username = serializers.CharField()
    user_email = serializers.CharField(allow_blank=True)
    user_organization = serializers.CharField(allow_blank=True)
    user_registration_method = serializers.CharField(allow_blank=True)
    user_identity_source = serializers.CharField(allow_blank=True)
    user_affiliations = serializers.ListField(child=serializers.CharField())
    user_claims = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text="Values the user carries for each claim the rule requires.",
    )
    user_is_protected = serializers.BooleanField()
    filter_results = FilterCheckResultSerializer(many=True)
    customer_lookup_performed = serializers.BooleanField()
    customer_candidates = CustomerCandidateSerializer(many=True)
    customer_lookup_ambiguous = serializers.BooleanField()
    resolved_project_name = serializers.CharField(allow_blank=True, allow_null=True)
