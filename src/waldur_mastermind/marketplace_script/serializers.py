from rest_framework import serializers


class OrderItemSerializer(serializers.Serializer):
    attributes = serializers.ReadOnlyField(source='attributes')
    limits = serializers.ReadOnlyField(source='limits')
    project_uuid = serializers.ReadOnlyField(source='order.project.uuid')
    project_name = serializers.ReadOnlyField(source='order.project.name')
    customer_uuid = serializers.ReadOnlyField(source='order.project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='order.project.customer.name')
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    plan_uuid = serializers.ReadOnlyField(source='plan.uuid')
    plan_name = serializers.ReadOnlyField(source='plan.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.uuid')
    resource_name = serializers.ReadOnlyField(source='resource.name')


class ResourceSerializer(serializers.Serializer):
    attributes = serializers.ReadOnlyField(source='attributes')
    limits = serializers.ReadOnlyField(source='limits')
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
