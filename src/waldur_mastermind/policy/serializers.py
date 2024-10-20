import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.invoices.models import CustomerCredit, ProjectCredit

from . import models

logger = logging.getLogger(__name__)


class PolicySerializer(serializers.HyperlinkedModelSerializer):
    scope_name = serializers.ReadOnlyField(source="scope.name")
    scope_uuid = serializers.ReadOnlyField(source="scope.uuid")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    has_fired = serializers.BooleanField(read_only=True)
    fired_datetime = serializers.DateTimeField(read_only=True)

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

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        return super().create(validated_data)

    def save(self, **kwargs):
        policy = super().save(**kwargs)

        if policy.is_triggered():
            policy.has_fired = True
            policy.fired_datetime = timezone.now()
            policy.save()
            logger.info(
                "A newly created policy %s has fired.",
                policy.uuid.hex,
            )

            for action in policy.get_immediate_actions():
                action.method(policy)
                logger.info(
                    "%s action of policy %s has been triggered.",
                    action.method.__name__,
                    policy.uuid.hex,
                )

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


class EstimatedCostPolicySerializer(PolicySerializer):
    period_name = serializers.ReadOnlyField(source="get_period_display")

    class Meta(PolicySerializer.Meta):
        fields = PolicySerializer.Meta.fields + (
            "limit_cost",
            "period",
            "period_name",
        )


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
        fields = EstimatedCostPolicySerializer.Meta.fields + ("project_credit",)

    project_credit = serializers.SerializerMethodField()

    def get_project_credit(self, instance):
        project: structure_models.Project = instance.scope
        try:
            return ProjectCredit.objects.get(project=project).value
        except ProjectCredit.DoesNotExist:
            return None

    def validate_scope(self, scope):
        if not scope:
            return

        user = self.context["request"].user
        customer = _get_customer(scope)

        if user.is_staff or customer.has_user(
            user, structure_models.CustomerRole.OWNER
        ):
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )


class CustomerEstimatedCostPolicySerializer(EstimatedCostPolicySerializer):
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

    def get_customer_credit(self, instance):
        customer: structure_models.Customer = instance.scope
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
    )

    def validate_scope(self, scope):
        if not scope:
            return

        user = self.context["request"].user

        customer = _get_customer(scope)

        if user.is_staff or customer.has_user(
            user, structure_models.CustomerRole.OWNER
        ):
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )

    class Meta:
        fields = ("organization_groups",)
        extra_kwargs = {
            "organization_groups": {
                "lookup_field": "uuid",
                "view_name": "organization-group-detail",
            },
        }


class OfferingEstimatedCostPolicySerializer(
    OfferingPolicySerializerMixin, EstimatedCostPolicySerializer
):
    period_name = serializers.ReadOnlyField(source="get_period_display")

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
    period_name = serializers.ReadOnlyField(source="get_period_display")

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

        offering = policy.scope
        components = []

        for component_limit in component_limits:
            component_type = component_limit["component"]["type"]
            limit = component_limit["limit"]
            component = offering.components.filter(type=component_type).first()
            components.append(component)

            if not component:
                raise ValidationError(
                    f"Offering has not component with type {component_type}."
                )

            models.OfferingComponentLimit.objects.update_or_create(
                policy=policy, defaults={"component": component, "limit": limit}
            )

        models.OfferingComponentLimit.objects.filter(policy=policy).exclude(
            component__in=components
        ).delete()

    def create(self, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        policy = super().create(validated_data)
        self._create_or_update(policy, component_limits)
        return policy

    def update(self, policy, validated_data):
        component_limits = validated_data.pop("component_limits_set", None)
        self._create_or_update(policy, component_limits)
        return super().update(policy, validated_data)
