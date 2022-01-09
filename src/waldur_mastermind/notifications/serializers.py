from rest_framework import serializers

from waldur_core.structure.models import (
    Customer,
    CustomerRole,
    DivisionType,
    Project,
    ProjectRole,
)
from waldur_mastermind.marketplace.models import Offering

from . import models, utils


class QuerySerializer(serializers.Serializer):
    customers = serializers.SlugRelatedField(
        slug_field='uuid', queryset=Customer.objects.all(), many=True, required=False,
    )
    customer_division_types = serializers.SlugRelatedField(
        slug_field='uuid',
        queryset=DivisionType.objects.all(),
        many=True,
        required=False,
    )
    projects = serializers.SlugRelatedField(
        slug_field='uuid',
        queryset=Project.available_objects.all(),
        many=True,
        required=False,
    )
    offerings = serializers.SlugRelatedField(
        slug_field='uuid', queryset=Offering.objects.all(), many=True, required=False,
    )
    customer_roles = serializers.MultipleChoiceField(
        choices=CustomerRole.CHOICES, required=False
    )
    project_roles = serializers.MultipleChoiceField(
        choices=ProjectRole.CHOICES, required=False
    )


class ReadNotificationSerializer(serializers.ModelSerializer):
    author_full_name = serializers.ReadOnlyField(source='author.full_name')
    query = serializers.JSONField()
    emails = serializers.JSONField()

    class Meta:
        model = models.Notification
        fields = (
            'uuid',
            'created',
            'subject',
            'body',
            'query',
            'author_full_name',
            'emails',
        )


class CreateNotificationSerializer(serializers.ModelSerializer):
    query = QuerySerializer(write_only=True)

    class Meta:
        model = models.Notification
        fields = ('uuid', 'created', 'subject', 'body', 'query')

    def create(self, validated_data):
        query = validated_data.pop('query')
        current_user = self.context['request'].user
        matching_users = utils.get_users_for_query(query)
        validated_data['author'] = current_user
        validated_data['emails'] = [user.email for user in matching_users if user.email]
        validated_data['query'] = ''
        notification = super(CreateNotificationSerializer, self).create(validated_data)
        serialized_query = {}
        if 'customers' in query:
            serialized_query['customers'] = self.format_options(query['customers'])
        if 'projects' in query:
            serialized_query['projects'] = self.format_options(query['projects'])
        if 'offerings' in query:
            serialized_query['offerings'] = self.format_options(query['offerings'])
        if 'customer_division_types' in query:
            serialized_query['customer_division_types'] = self.format_options(
                query['customer_division_types']
            )
        if 'customer_roles' in query:
            serialized_query['customer_roles'] = list(query['customer_roles'])
        if 'project_roles' in query:
            serialized_query['project_roles'] = list(query['project_roles'])
        notification.query = serialized_query
        notification.save(update_fields=['query'])
        return notification

    def format_options(self, options):
        return [{'name': option.name, 'uuid': option.uuid.hex} for option in options]


class DryRunNotificationSerializer(serializers.Serializer):
    query = QuerySerializer(write_only=True)

    class Meta:
        fields = ('query',)
