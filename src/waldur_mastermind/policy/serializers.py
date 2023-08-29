import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models

from . import models

logger = logging.getLogger(__name__)


class PolicySerializer(serializers.HyperlinkedModelSerializer):
    def validate_actions(self, value):
        if not value:
            return

        actions = set(value.split(','))
        if actions - {a.__name__ for a in self.Meta.model.available_actions}:
            raise ValidationError(
                _("%(value)s includes unavailable actions."),
                params={"value": value},
            )

        return value

    def save(self, **kwargs):
        policy = super().save(**kwargs)

        if policy.is_triggered():
            policy.has_fired = True
            policy.fired_datetime = timezone.now()
            policy.save()
            logger.info(
                'A newly created policy %s has fired.',
                policy.uuid.hex,
            )

            for action in policy.get_one_time_actions():
                action(policy)
                logger.info(
                    '%s action of policy %s has been triggerd.',
                    action.__name__,
                    policy.uuid.hex,
                )

        return policy


class ProjectEstimatedCostPolicySerializer(
    core_serializers.AugmentedSerializerMixin, PolicySerializer
):
    project_name = serializers.ReadOnlyField(source='project.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    created_by_full_name = serializers.ReadOnlyField(source='created_by.full_name')
    created_by_username = serializers.ReadOnlyField(source='created_by.username')
    has_fired = serializers.BooleanField(read_only=True)
    fired_datetime = serializers.DateTimeField(read_only=True)

    class Meta:
        model = models.ProjectEstimatedCostPolicy
        fields = (
            'uuid',
            'url',
            'limit_cost',
            'project',
            'project_name',
            'project_uuid',
            'actions',
            'created',
            'created_by_full_name',
            'created_by_username',
            'has_fired',
            'fired_datetime',
        )
        view_name = 'marketplace-project-estimated-cost-policy-detail'
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'project': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }

    def validate_project(self, project):
        if not project:
            return project

        user = self.context['request'].user

        if (
            not project
            or user.is_staff
            or user.is_support
            or project.customer.has_user(user, structure_models.CustomerRole.OWNER)
        ):
            return project
        raise serializers.ValidationError(
            _('Only customer owner and staff can create policy.')
        )

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)
