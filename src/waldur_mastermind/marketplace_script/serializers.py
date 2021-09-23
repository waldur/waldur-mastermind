from functools import lru_cache

from rest_framework import serializers

from waldur_core.structure import models as structure_models


class OrderItemSerializer(serializers.Serializer):
    attributes = serializers.ReadOnlyField()
    limits = serializers.ReadOnlyField()
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

    @lru_cache(maxsize=1)
    def _get_project(self, order_item):
        return structure_models.Project.all_objects.get(id=order_item.order.project_id)

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
