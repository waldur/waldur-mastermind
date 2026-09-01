import datetime
import decimal
import json
import logging
from typing import cast

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.invoices import compensations as invoices_compensations
from waldur_mastermind.invoices.models import CustomerCredit, PeriodMixin, ProjectCredit
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BillingTypes, ResourceStates
from waldur_mastermind.policy.policy_actions import (
    POLICY_ACTIONS,
    _filter_resources_by_scope,
)

from . import models
from .models import LIMIT_PERIOD_TO_POLICY_PERIOD
from .utils import system_actor_event_context

logger = logging.getLogger(__name__)


class PolicySerializer(serializers.HyperlinkedModelSerializer):
    scope_name = serializers.CharField(read_only=True, source="scope.name")
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")
    created_by_full_name = serializers.CharField(
        read_only=True, source="created_by.full_name"
    )
    created_by_username = serializers.CharField(
        read_only=True, source="created_by.username"
    )
    has_fired = serializers.BooleanField(read_only=True)
    fired_datetime = serializers.DateTimeField(read_only=True)
    affected_resources_count = serializers.SerializerMethodField()

    def get_affected_resources_count(self, instance) -> int:
        if not instance.has_fired:
            return 0
        actions = set((instance.actions or "").split(","))
        qs = marketplace_models.Resource.objects.exclude(
            state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING)
        )
        qs = _filter_resources_by_scope(qs, instance)
        if qs is None:
            return 0

        # Filter by user visibility to prevent information disclosure.
        # Non-staff users should only see counts of resources they have access to.
        request = self.context.get("request")
        if request and request.user and not request.user.is_staff:
            qs = filter_queryset_for_user(qs, request.user)

        q = Q()
        if "request_pausing" in actions:
            q |= Q(paused=True, offering__plugin_options__supports_pausing=True)
        if "request_downscaling" in actions:
            q |= Q(
                downscaled=True,
                offering__plugin_options__supports_downscaling=True,
            )
        if "restrict_members" in actions:
            q |= Q(
                restrict_member_access=True,
                offering__plugin_options__service_provider_can_create_offering_user=True,
            )
        if not q:
            return 0
        return qs.filter(q).count()

    def validate_actions(self, value):
        if not value:
            return

        actions = set(value.split(","))
        if actions - self.Meta.model.available_actions:
            raise ValidationError(
                _("%(value)s includes unavailable actions."),
                params={"value": value},
            )

        return value

    def validate_options(self, options):
        if not options:
            return {}

        # Handle case where options might be passed as a string (JSON string)
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except json.JSONDecodeError:
                raise ValidationError(_("Options must be a valid JSON object."))

        # Ensure options is a dictionary
        if not isinstance(options, dict):
            raise ValidationError(_("Options must be a dictionary."))

        for key, value in options.items():
            validator = getattr(POLICY_ACTIONS[key], "options_validator", None)
            if validator:
                if not validator(options[key]):
                    raise ValidationError(f"Options for {key} are invalid.")

        return options

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)

    def save(self, **kwargs):
        policy = super().save(**kwargs)

        if policy.is_triggered():
            if not policy.has_fired:
                policy.has_fired = True
                policy.fired_datetime = timezone.now()
                policy.save()
                logger.info(
                    "Policy %s has fired.",
                    policy.uuid.hex,
                )

                # Execute actions after the transaction is committed to ensure
                # the policy exists in the database when Celery tasks run
                def execute_actions():
                    # Attribute the policy-driven events (and the resource saves
                    # they trigger) to the system robot rather than the user whose
                    # request saved the policy.
                    with system_actor_event_context():
                        for action in policy.get_immediate_actions():
                            action.method(policy)
                            logger.info(
                                "%s action of policy %s has been triggered.",
                                action.method.__name__,
                                policy.uuid.hex,
                            )

                transaction.on_commit(execute_actions)
        else:
            # Policy is not triggered - reset has_fired if it was previously fired
            if policy.has_fired:
                policy.has_fired = False
                # Keep fired_datetime as a record of last state change
                # This preserves the timestamp for auditing purposes
                policy.save()
                logger.info(
                    "Policy %s has been reset (no longer triggered).",
                    policy.uuid.hex,
                )

                # Execute reset actions after transaction commit
                def execute_reset_actions():
                    with system_actor_event_context():
                        for action in policy.get_all_actions():
                            if action.reset_method:
                                action.reset_method(policy)
                                logger.info(
                                    "Reset method %s of policy %s has been executed.",
                                    action.reset_method.__name__,
                                    policy.uuid.hex,
                                )

                transaction.on_commit(execute_reset_actions)

        return policy

    class Meta:
        fields = (
            "uuid",
            "url",
            "scope",
            "scope_name",
            "scope_uuid",
            "actions",
            "created",
            "created_by_full_name",
            "created_by_username",
            "has_fired",
            "fired_datetime",
            "options",
            "affected_resources_count",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "scope": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }


class EstimatedCostPolicySerializer(
    core_serializers.RestrictedSerializerMixin, PolicySerializer
):
    period_name = serializers.CharField(read_only=True, source="get_period_display")
    current_cost = serializers.SerializerMethodField()
    eta_days = serializers.SerializerMethodField()
    eta_date = serializers.SerializerMethodField()

    class Meta(PolicySerializer.Meta):
        fields = PolicySerializer.Meta.fields + (
            "limit_cost",
            "period",
            "period_name",
            "current_cost",
            "eta_days",
            "eta_date",
        )

    @extend_schema_field(
        serializers.DecimalField(
            max_digits=16,
            decimal_places=2,
            help_text=(
                "The cost this policy compares against limit_cost right now: the period's invoice total, less the credit already applied and the credit still to be drawn. Do not re-derive it — only the server can simulate the pending draw, and a figure computed from the invoice alone will not match what the policy evaluates."
            ),
        )
    )
    def get_current_cost(self, instance) -> decimal.Decimal:
        """The cost the policy compares against `limit_cost` right now.

        Clients showing saturation must not re-derive this: the figure is the
        period's invoice total less the credit still to be drawn, and only the
        server can simulate the latter. Pass `?field=` without `current_cost`
        to skip the simulation on requests that do not need it.
        """
        # Building a MonthlyCompensation costs two queries before it is asked
        # anything, so skip it for policies that deduct no credit.
        compensation = (
            self._compensation_for(instance)
            if instance.uses_credit_compensation
            else None
        )
        return instance.get_current_cost(compensation)

    @extend_schema_field(
        serializers.IntegerField(
            allow_null=True,
            help_text=(
                "Days until the policy fires, or null when no projection exists. 0 means the threshold is already crossed and the policy is triggered — measured, not projected. Null must be rendered as no date, never as 'now': it is the common case, and it is also what an unprojectable or more-than-a-year-away policy reports. Nothing is projected beyond 365 days, because the rate comes from the current month's spend. Note that a cost policy does not fire on cost alone — it also waits for the credit balance to fall to limit_cost — so a policy far over its cap can still report a future date or null."
            ),
        )
    )
    def get_eta_days(self, instance) -> int | None:
        """Days until the policy crosses `limit_cost`; null when unprojectable.

        0 means the limit is already exceeded. Null is the common case and must
        be rendered as "no date", never as "now": the projection needs both the
        gross cost and the credit still to be drawn, and a client holding only
        their difference cannot derive it — waldur/waldur-homeport#244 is what
        happens when one tries. Costs the same simulation as `current_cost`, so
        it is excluded by the same `?field=` as that one.
        """
        # Memoised per response: `eta_date` is the same projection formatted,
        # and the simulation behind it is the expensive part.
        cache = self.context.setdefault("_policy_eta_cache", {})
        key = (type(instance).__name__, instance.pk)
        if key not in cache:
            compensation = (
                self._compensation_for(instance)
                if instance.uses_credit_compensation
                else None
            )
            cache[key] = instance.get_eta_days(compensation)
        return cache[key]

    @extend_schema_field(
        serializers.DateField(
            allow_null=True,
            help_text=(
                "eta_days as a calendar date, so clients do not each re-derive it. Null whenever eta_days is null; today when eta_days is 0."
            ),
        )
    )
    def get_eta_date(self, instance) -> datetime.date | None:
        """`eta_days` as a date, so clients do not each re-derive the calendar."""
        eta_days = self.get_eta_days(instance)
        if eta_days is None:
            return None
        return datetime.date.today() + datetime.timedelta(days=eta_days)

    def _compensation_for(self, instance):
        """One MonthlyCompensation per customer, shared across the response."

        Serializing a list of policies for one customer would otherwise re-run
        the same simulation for every row.
        """
        customer = getattr(instance.scope, "customer", instance.scope)
        cache = self.context.setdefault("_monthly_compensations", {})
        if customer.id not in cache:
            cache[customer.id] = invoices_compensations.MonthlyCompensation(customer)
        return cache[customer.id]


class ProjectEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, EstimatedCostPolicySerializer
):
    class Meta(EstimatedCostPolicySerializer.Meta):
        model = models.ProjectEstimatedCostPolicy
        view_name = "marketplace-project-estimated-cost-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "scope": {"lookup_field": "uuid", "view_name": "project-detail"},
        }
        fields = EstimatedCostPolicySerializer.Meta.fields + (
            "project_credit",
            "customer_credit",
            "resource",
            "resource_name",
            "use_credit",
        )

    project_credit = serializers.SerializerMethodField()
    customer_credit = serializers.SerializerMethodField()
    resource = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=marketplace_models.Resource.objects.all(),
        allow_null=True,
        required=False,
    )
    resource_name = serializers.CharField(read_only=True, source="resource.name")

    def validate(self, attrs):
        attrs = super().validate(attrs)

        resource = (
            attrs.get("resource")
            if "resource" in attrs
            else (self.instance.resource if self.instance else None)
        )
        if resource is None:
            return attrs

        scope = attrs.get("scope") or (self.instance.scope if self.instance else None)
        if scope is not None and resource.project_id != scope.id:
            raise serializers.ValidationError(
                {"resource": _("Resource must belong to the selected project.")}
            )

        # Only reject a terminated resource when it is being (re)assigned.
        if "resource" in attrs and resource.state in (
            ResourceStates.TERMINATED,
            ResourceStates.TERMINATING,
        ):
            raise serializers.ValidationError(
                {"resource": _("Cannot scope a policy to a terminated resource.")}
            )

        actions = attrs.get("actions")
        if actions is None and self.instance:
            actions = self.instance.actions
        action_set = set((actions or "").split(","))
        if "block_creation_of_new_resources" in action_set:
            raise serializers.ValidationError(
                {
                    "actions": _(
                        "block_creation_of_new_resources cannot be used with a "
                        "resource-scoped policy."
                    )
                }
            )

        return attrs

    def get_project_credit(self, instance) -> str | None:
        project = cast(structure_models.Project, instance.scope)
        try:
            return ProjectCredit.objects.get(project=project).value
        except ProjectCredit.DoesNotExist:
            return None

    def get_customer_credit(self, instance) -> str | None:
        customer: structure_models.Customer = instance.scope.customer
        try:
            return CustomerCredit.objects.get(customer=customer).value
        except CustomerCredit.DoesNotExist:
            return None

    def validate_scope(self, scope):
        if not scope:
            return

        # Check if scope is a terminated project
        if scope._meta.model_name == "project" and getattr(scope, "is_removed", False):
            raise serializers.ValidationError(
                _("Cannot create policies for terminated projects.")
            )

        user = self.context["request"].user
        customer = _get_customer(scope)

        if user.is_staff or customer.has_user(user, CustomerRole.OWNER):
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )


class CustomerEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, EstimatedCostPolicySerializer
):
    class Meta(EstimatedCostPolicySerializer.Meta):
        model = models.CustomerEstimatedCostPolicy
        view_name = "marketplace-customer-estimated-cost-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-customer-estimated-cost-policy-detail",
            },
            "scope": {"lookup_field": "uuid", "view_name": "customer-detail"},
        }
        fields = EstimatedCostPolicySerializer.Meta.fields + ("customer_credit",)

    customer_credit = serializers.SerializerMethodField()

    def get_customer_credit(self, instance) -> str | None:
        customer = cast(structure_models.Customer, instance.scope)
        try:
            return CustomerCredit.objects.get(customer=customer).value
        except CustomerCredit.DoesNotExist:
            return None

    def validate_scope(self, scope):
        if not scope:
            return

        user = self.context["request"].user

        if user.is_staff:
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )


class OfferingPolicySerializerMixin(core_serializers.AugmentedSerializerMixin):
    organization_groups = serializers.HyperlinkedRelatedField(
        queryset=structure_models.OrganizationGroup.objects.all(),
        view_name="organization-group-detail",
        lookup_field="uuid",
        many=True,
        required=False,
        allow_empty=True,
    )
    apply_to_all = serializers.BooleanField(
        default=False,
        help_text="If True, policy applies to all customers. "
        "Mutually exclusive with organization_groups.",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Check for new policies and updates
        apply_to_all = attrs.get("apply_to_all", False)
        organization_groups = attrs.get("organization_groups", [])

        # For existing instances, check current values if not provided
        if self.instance and "apply_to_all" not in attrs:
            apply_to_all = self.instance.apply_to_all
        if self.instance and "organization_groups" not in attrs:
            organization_groups = list(self.instance.organization_groups.all())

        # Check mutual exclusivity
        if apply_to_all and organization_groups:
            raise serializers.ValidationError(
                "Cannot set apply_to_all=True when organization_groups are specified. "
                "Choose one approach: either apply_to_all or specific organization_groups."
            )

        # Ensure at least one is set
        if not apply_to_all and not organization_groups:
            raise serializers.ValidationError(
                "Must either set apply_to_all=True or specify organization_groups."
            )

        return attrs

    def validate_scope(self, scope):
        if not scope:
            return

        user = self.context["request"].user

        customer = _get_customer(scope)

        if user.is_staff or customer.has_user(user, CustomerRole.OWNER):
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )

    class Meta:
        fields = ("organization_groups", "apply_to_all")
        extra_kwargs = {
            "organization_groups": {
                "lookup_field": "uuid",
                "view_name": "organization-group-detail",
            },
        }


class OfferingEstimatedCostPolicySerializer(
    OfferingPolicySerializerMixin, EstimatedCostPolicySerializer
):
    period_name = serializers.CharField(read_only=True, source="get_period_display")

    class Meta(EstimatedCostPolicySerializer.Meta):
        fields = (
            EstimatedCostPolicySerializer.Meta.fields
            + OfferingPolicySerializerMixin.Meta.fields
            + ("period", "period_name")
        )
        model = models.OfferingEstimatedCostPolicy
        view_name = "marketplace-offering-estimated-cost-policy-detail"
        extra_kwargs = EstimatedCostPolicySerializer.Meta.extra_kwargs
        extra_kwargs.update(OfferingPolicySerializerMixin.Meta.extra_kwargs)


class NestedOfferingComponentLimitSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="component.type")

    class Meta:
        model = models.OfferingComponentLimit
        fields = ("type", "limit")


class OfferingUsagePolicySerializer(OfferingPolicySerializerMixin, PolicySerializer):
    component_limits_set = NestedOfferingComponentLimitSerializer(many=True)
    period_name = serializers.CharField(read_only=True, source="get_period_display")

    class Meta(PolicySerializer.Meta):
        fields = (
            PolicySerializer.Meta.fields
            + OfferingPolicySerializerMixin.Meta.fields
            + ("component_limits_set", "period", "period_name")
        )
        model = models.OfferingUsagePolicy
        view_name = "marketplace-offering-usage-policy-detail"
        extra_kwargs = EstimatedCostPolicySerializer.Meta.extra_kwargs
        extra_kwargs.update(OfferingPolicySerializerMixin.Meta.extra_kwargs)

    def _create_or_update(self, policy, component_limits):
        if component_limits is None:
            return

        # Validate duplicates within payload: only one limit per component type
        keys = [cl.get("component", {}).get("type") for cl in component_limits]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError(
                _("Duplicate component limits for the same component.")
            )

        offering = policy.scope
        models.OfferingComponentLimit.objects.filter(policy=policy).delete()

        for component_limit in component_limits:
            component_type = component_limit["component"]["type"]
            limit = component_limit["limit"]
            component = offering.components.filter(type=component_type).first()

            if not component:
                raise ValidationError(
                    f"Offering has not component with type {component_type}."
                )

            models.OfferingComponentLimit.objects.create(
                policy=policy, component=component, limit=limit
            )

    @transaction.atomic
    def create(self, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        policy = super().create(validated_data)
        self._create_or_update(policy, component_limits)
        return policy

    @transaction.atomic
    def update(self, policy, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        self._create_or_update(policy, component_limits)
        return super().update(policy, validated_data)


class NestedCustomerUsagePolicyComponentSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="component.type", read_only=True)
    period_name = serializers.CharField(read_only=True, source="get_period_display")
    component = serializers.UUIDField(source="component.uuid")

    class Meta:
        model = models.CustomerUsagePolicyComponent
        fields = ("type", "limit", "period", "period_name", "component")


class CustomerComponentUsagePolicySerializer(PolicySerializer):
    component_limits_set = NestedCustomerUsagePolicyComponentSerializer(many=True)

    class Meta(PolicySerializer.Meta):
        fields = PolicySerializer.Meta.fields + ("component_limits_set",)
        model = models.CustomerComponentUsagePolicy
        view_name = "marketplace-customer-component-usage-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-customer-component-usage-policy-detail",
            },
            "scope": {"lookup_field": "uuid", "view_name": "customer-detail"},
        }

    def _create_or_update(self, policy, component_limits):
        if component_limits is None:
            return

        # Validate duplicates within payload: only one limit per component and period
        keys = [
            (cl.get("component", {}).get("uuid"), cl.get("period"))
            for cl in component_limits
        ]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError(
                _("Duplicate component limits for the same component and period.")
            )

        # Clear existing component limits for this policy
        models.CustomerUsagePolicyComponent.objects.filter(policy=policy).delete()

        for component_limit in component_limits:
            component_uuid = component_limit["component"]["uuid"]
            limit = component_limit["limit"]
            period = component_limit["period"]

            try:
                component = marketplace_models.OfferingComponent.objects.get(
                    uuid=component_uuid
                )
            except marketplace_models.OfferingComponent.DoesNotExist:
                raise serializers.ValidationError(
                    f"Component with uuid {component_uuid} does not exist."
                )

            # Validate billing type
            if component.billing_type not in (BillingTypes.USAGE, BillingTypes.LIMIT):
                raise serializers.ValidationError(
                    f"Component {component.type} has billing type {component.billing_type} which is not supported for usage policies. Only USAGE and LIMIT billing types are supported."
                )

            models.CustomerUsagePolicyComponent.objects.create(
                policy=policy, component=component, limit=limit, period=period
            )

    @transaction.atomic
    def create(self, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        policy = super().create(validated_data)
        self._create_or_update(policy, component_limits)
        return policy

    @transaction.atomic
    def update(self, policy, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        self._create_or_update(policy, component_limits)
        return super().update(policy, validated_data)


class SlurmPeriodicUsagePolicySerializer(OfferingUsagePolicySerializer):
    warnings = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        required=False,
        help_text="Warnings about misconfiguration, e.g. missing site agent queue registration.",
    )

    class Meta(OfferingUsagePolicySerializer.Meta):
        model = models.SlurmPeriodicUsagePolicy
        view_name = "marketplace-slurm-periodic-usage-policy-detail"
        fields = OfferingUsagePolicySerializer.Meta.fields + (
            "limit_type",
            "tres_billing_enabled",
            "tres_billing_weights",
            "carryover_factor",
            "grace_ratio",
            "carryover_enabled",
            "raw_usage_reset",
            "qos_strategy",
            "warnings",
        )
        extra_kwargs = OfferingUsagePolicySerializer.Meta.extra_kwargs

    def validate(self, attrs):
        attrs = super().validate(attrs)
        scope = attrs.get("scope") or (self.instance.scope if self.instance else None)
        if not scope:
            return attrs

        # Collect distinct non-empty limit_period values from limit-based components
        limit_periods = set(
            scope.components.filter(billing_type=BillingTypes.LIMIT)
            .exclude(limit_period__isnull=True)
            .exclude(limit_period="")
            .values_list("limit_period", flat=True)
            .distinct()
        )
        if not limit_periods:
            return attrs
        if len(limit_periods) > 1:
            raise serializers.ValidationError(
                {
                    "period": _(
                        "Offering has multiple limit-based components with "
                        "different limit_period values: %(periods)s. "
                        "All must use the same limit_period."
                    )
                    % {"periods": ", ".join(sorted(limit_periods))}
                }
            )

        limit_period = limit_periods.pop()
        expected_period = LIMIT_PERIOD_TO_POLICY_PERIOD.get(limit_period)
        if expected_period is None:
            return attrs

        period = attrs.get("period")
        if period is None and self.instance is None:
            # Auto-set period on create when not explicitly provided
            attrs["period"] = expected_period
        elif period is not None and period != expected_period:
            raise serializers.ValidationError(
                {
                    "period": _(
                        "Period must match the offering component's limit_period "
                        "(%(limit_period)s). Expected %(expected)s, got %(actual)s."
                    )
                    % {
                        "limit_period": limit_period,
                        "expected": dict(PeriodMixin.Periods.CHOICES).get(
                            expected_period
                        ),
                        "actual": dict(PeriodMixin.Periods.CHOICES).get(period),
                    }
                }
            )
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        from waldur_core.logging.models import EventSubscriptionQueue

        queue_exists = EventSubscriptionQueue.objects.filter(
            offering_uuid=instance.scope.uuid,
            object_type="resource_periodic_limits",
        ).exists()
        if not queue_exists:
            data["warnings"] = [
                "No site agent has registered a queue for periodic limits updates on "
                "this offering. Periodic settings are delivered over STOMP only, so "
                "the policy cannot be enforced until a queue is registered.",
                "Queues are registered exclusively by a site agent running in "
                "event_process mode — the order_process, membership_sync and report "
                "modes never register one. Check that such an agent is running for "
                "this offering, that the offering has stomp_enabled: true and "
                "backend_settings.periodic_limits.enabled: true in the agent "
                "configuration, and restart the event_process agent after changing "
                "its configuration. Requires site agent 0.8.0 or newer.",
            ]
        return data


class SlurmPolicyPreviewRequestSerializer(serializers.Serializer):
    """Serializer for SLURM policy impact preview request."""

    allocation = serializers.FloatField(
        required=False,
        default=1000,
        help_text="Base allocation for the period (in node-hours or billing units)",
    )
    grace_ratio = serializers.FloatField(
        required=False,
        default=0.2,
        min_value=0,
        max_value=1,
        help_text="Grace ratio for overconsumption allowance (0.2 = 20%)",
    )
    previous_usage = serializers.FloatField(
        required=False,
        default=0,
        help_text="Usage from the previous period",
    )
    carryover_factor = serializers.IntegerField(
        required=False,
        default=50,
        min_value=0,
        max_value=100,
        help_text="Maximum percentage of base allocation that can carry over (0-100)",
    )
    carryover_enabled = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Whether unused allocation carries over to next period",
    )
    resource_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Optional resource UUID to use for current usage data",
    )
    current_usage = serializers.FloatField(
        required=False,
        default=0,
        help_text="Current usage in this period (manual input or from resource)",
    )
    daily_usage_rate = serializers.FloatField(
        required=False,
        default=0,
        help_text="Average daily usage rate for projections",
    )


class SlurmPolicyThresholdsSerializer(serializers.Serializer):
    """Serializer for QoS threshold values."""

    allocation = serializers.FloatField()
    grace_ratio = serializers.FloatField()
    notification_ratio = serializers.FloatField()
    notification_threshold = serializers.FloatField()
    slowdown_threshold = serializers.FloatField()
    blocked_threshold = serializers.FloatField()


class SlurmPolicyCarryoverSerializer(serializers.Serializer):
    """Serializer for carryover calculation details."""

    previous_usage = serializers.FloatField()
    carryover_factor = serializers.IntegerField()
    base_allocation = serializers.FloatField()
    unused = serializers.FloatField()
    carryover_cap = serializers.FloatField()
    carryover = serializers.FloatField()
    total_allocation = serializers.FloatField()


class SlurmPolicyDateProjectionSerializer(serializers.Serializer):
    """Serializer for a single date projection."""

    days = serializers.IntegerField(allow_null=True)
    date = serializers.DateField(allow_null=True)
    status = serializers.ChoiceField(choices=["never", "exceeded", "projected"])


class SlurmPolicyDateProjectionsSerializer(serializers.Serializer):
    """Serializer for all date projections."""

    notification = SlurmPolicyDateProjectionSerializer()
    slowdown = SlurmPolicyDateProjectionSerializer()
    blocked = SlurmPolicyDateProjectionSerializer()


class SlurmCommandSerializer(serializers.Serializer):
    """Serializer for a SLURM command preview."""

    type = serializers.CharField(
        help_text="Command type: fairshare, limits, qos, reset_usage"
    )
    description = serializers.CharField(help_text="Human-readable description")
    command = serializers.CharField(help_text="Actual shell command")
    parameters = serializers.DictField(help_text="Command parameters")


class SlurmCommandHistorySerializer(serializers.ModelSerializer):
    """Serializer for executed command history."""

    class Meta:
        model = models.SlurmCommandHistory
        fields = [
            "uuid",
            "command_type",
            "description",
            "shell_command",
            "parameters",
            "executed_at",
            "execution_mode",
            "success",
            "error_message",
        ]


class SlurmPolicyPreviewResponseSerializer(serializers.Serializer):
    """Serializer for SLURM policy impact preview response."""

    base_allocation = serializers.FloatField()
    effective_allocation = serializers.FloatField()
    carryover_enabled = serializers.BooleanField()
    carryover = SlurmPolicyCarryoverSerializer(allow_null=True)
    thresholds = SlurmPolicyThresholdsSerializer()
    grace_ratio = serializers.FloatField()
    carryover_factor = serializers.IntegerField()
    # Resource-specific fields (optional)
    current_usage = serializers.FloatField(required=False)
    daily_usage_rate = serializers.FloatField(required=False)
    usage_percentage = serializers.FloatField(required=False)
    current_qos_status = serializers.ChoiceField(
        choices=["normal", "notification", "slowdown", "blocked"],
        required=False,
    )
    date_projections = SlurmPolicyDateProjectionsSerializer(required=False)
    # Command preview and history (new fields)
    preview_commands = SlurmCommandSerializer(many=True, required=False)
    command_history = SlurmCommandHistorySerializer(many=True, required=False)
    billing_period_start = serializers.DateField(required=False)
    billing_period_end = serializers.DateField(required=False)


class SlurmCommandResultSerializer(serializers.Serializer):
    """Serializer for site agent command result report-back."""

    resource_uuid = serializers.UUIDField(
        help_text="UUID of the resource the command was applied to",
    )
    success = serializers.BooleanField(
        help_text="Whether the command was applied successfully",
    )
    error_message = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Error message if the command failed",
    )
    mode = serializers.ChoiceField(
        choices=["production", "emulator"],
        default="production",
        help_text="Execution mode of the command",
    )
    commands_executed = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="List of shell commands actually executed by the site agent",
    )


class SlurmPolicyEvaluationLogSerializer(serializers.ModelSerializer):
    """Serializer for SLURM policy evaluation log entries."""

    resource_name = serializers.CharField(source="resource.name", read_only=True)
    resource_uuid = serializers.UUIDField(source="resource.uuid", read_only=True)
    actions_taken = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = models.SlurmPolicyEvaluationLog
        fields = [
            "uuid",
            "resource_uuid",
            "resource_name",
            "billing_period",
            "usage_percentage",
            "grace_limit_percentage",
            "actions_taken",
            "previous_state",
            "new_state",
            "stomp_message_sent",
            "site_agent_confirmed",
            "site_agent_response",
            "evaluated_at",
        ]


class SlurmPolicyEvaluateRequestSerializer(serializers.Serializer):
    """Request serializer for dry-run and evaluate actions."""

    resource_uuid = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Evaluate a specific resource. If omitted, evaluates all offering resources.",
    )


class SlurmPolicyDryRunResourceSerializer(serializers.Serializer):
    """Serializer for a single resource result in a dry-run."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    usage_percentage = serializers.FloatField()
    paused = serializers.BooleanField()
    downscaled = serializers.BooleanField()
    would_trigger = serializers.ListField(child=serializers.CharField())


class SlurmPolicyDryRunResponseSerializer(serializers.Serializer):
    """Response serializer for dry-run action."""

    policy_uuid = serializers.UUIDField()
    billing_period = serializers.CharField()
    grace_limit_percentage = serializers.FloatField()
    resources = SlurmPolicyDryRunResourceSerializer(many=True)


class SlurmPolicyEvaluateResourceSerializer(serializers.Serializer):
    """Serializer for a single resource result in an evaluation."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    usage_percentage = serializers.FloatField()
    actions_taken = serializers.ListField(child=serializers.CharField())
    previous_state = serializers.DictField()
    new_state = serializers.DictField()


class SlurmPolicyEvaluateResponseSerializer(serializers.Serializer):
    """Response serializer for evaluate action."""

    policy_uuid = serializers.UUIDField()
    billing_period = serializers.CharField()
    resources = SlurmPolicyEvaluateResourceSerializer(many=True)
