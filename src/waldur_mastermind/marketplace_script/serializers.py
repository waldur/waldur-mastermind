from rest_framework import serializers

from waldur_core.core.fields import NaturalChoiceField
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_script import models as marketplace_script_models


class OrderItemSerializer(serializers.Serializer):
    attributes = serializers.ReadOnlyField()
    limits = serializers.ReadOnlyField()
    creator_email = serializers.ReadOnlyField(source='order.created_by.email')
    creator_username = serializers.ReadOnlyField(source='order.created_by.username')
    project_uuid = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    customer_uuid = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    plan_uuid = serializers.ReadOnlyField(source='plan.uuid')
    plan_name = serializers.ReadOnlyField(source='plan.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.uuid')
    resource_name = serializers.ReadOnlyField(source='resource.name')
    resource_backend_id = serializers.ReadOnlyField(source='resource.backend_id')

    def _get_project(self, order_item):
        return order_item.order.project

    def get_customer_uuid(self, order_item):
        project = self._get_project(order_item)
        return project.customer.uuid

    def get_customer_name(self, order_item):
        project = self._get_project(order_item)
        return project.customer.name

    def get_project_uuid(self, order_item):
        project = self._get_project(order_item)
        return project.uuid

    def get_project_name(self, order_item):
        project = self._get_project(order_item)
        return project.name


class ResourceSerializer(serializers.Serializer):
    attributes = serializers.ReadOnlyField()
    limits = serializers.ReadOnlyField()
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    plan_uuid = serializers.ReadOnlyField(source='plan.uuid')
    plan_name = serializers.ReadOnlyField(source='plan.name')
    resource_uuid = serializers.ReadOnlyField(source='uuid')
    resource_name = serializers.ReadOnlyField(source='name')


class DryRunTypes(models.RequestTypeMixin.Types):
    PULL = 4
    CHOICES = models.RequestTypeMixin.Types.CHOICES + ((PULL, 'Pull'),)

    @classmethod
    def get_type_display(cls, index):
        for choice in cls.CHOICES:
            if index == choice[0]:
                return choice[1].lower()

        return index


class DryRunSerializer(
    serializers.HyperlinkedModelSerializer,
):
    plan = serializers.HyperlinkedRelatedField(
        view_name='marketplace-plan-detail',
        lookup_field='uuid',
        queryset=models.Plan.objects.all(),
        write_only=True,
    )
    type = NaturalChoiceField(
        choices=DryRunTypes.CHOICES,
        required=False,
        default=DryRunTypes.CREATE,
        write_only=True,
    )
    attributes = serializers.JSONField(required=False, write_only=True)

    class Meta:
        model = marketplace_script_models.DryRun
        fields = (
            'url',
            'uuid',
            'plan',
            'type',
            'attributes',
            'order_item_attributes',
            'order_item_type',
            'order_item_offering',
            'state',
            'get_state_display',
            'output',
            'created',
        )

        read_only_fields = (
            'order_item_attributes',
            'order_item_type',
            'state',
            'output',
            'uuid',
            'created',
        )

        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-script-async-dry-run-detail',
            },
            'order_item_offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
        }
