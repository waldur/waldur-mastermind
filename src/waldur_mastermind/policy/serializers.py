import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models
from waldur_core.structure.permissions import _get_customer

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
        if actions - {a.__name__ for a in self.Meta.model.available_actions}:
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

            for action in policy.get_one_time_actions():
                action(policy)
                logger.info(
                    "%s action of policy %s has been triggerd.",
                    action.__name__,
                    policy.uuid.hex,
                )

        return policy

    class Meta:
        fields = (
            "uuid",
            "url",
            "limit_cost",
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


class ProjectEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, PolicySerializer
):
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

    class Meta(PolicySerializer.Meta):
        model = models.ProjectEstimatedCostPolicy
        view_name = "marketplace-project-estimated-cost-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "scope": {"lookup_field": "uuid", "view_name": "project-detail"},
        }


class CustomerEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, PolicySerializer
):
    def validate_scope(self, scope):
        if not scope:
            return

        user = self.context["request"].user

        if user.is_staff:
            return scope

        raise serializers.ValidationError(
            _("User is not allowed to configure policies.")
        )

    class Meta(PolicySerializer.Meta):
        model = models.CustomerEstimatedCostPolicy
        view_name = "marketplace-customer-estimated-cost-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "scope": {"lookup_field": "uuid", "view_name": "customer-detail"},
        }


class OfferingEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, PolicySerializer
):
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

    class Meta(PolicySerializer.Meta):
        fields = PolicySerializer.Meta.fields + ("organization_groups",)
        model = models.OfferingEstimatedCostPolicy
        view_name = "marketplace-offering-estimated-cost-policy-detail"
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "scope": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
            "organization_group": {
                "lookup_field": "uuid",
                "view_name": "organization-group-detail",
            },
        }
