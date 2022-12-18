from rest_framework import serializers

from waldur_core.structure import serializers as structure_serializers
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
        slug_field='uuid',
        queryset=Customer.objects.all(),
        many=True,
        required=False,
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
        slug_field='uuid',
        queryset=Offering.objects.all(),
        many=True,
        required=False,
    )
    customer_roles = serializers.MultipleChoiceField(
        choices=CustomerRole.CHOICES, required=False
    )
    project_roles = serializers.MultipleChoiceField(
        choices=ProjectRole.CHOICES, required=False
    )


def format_options(options):
    return [{'name': option.name, 'uuid': option.uuid.hex} for option in options]


def serialize_query(query):
    serialized_query = {}
    if 'customers' in query:
        serialized_query['customers'] = format_options(query['customers'])
    if 'projects' in query:
        serialized_query['projects'] = format_options(query['projects'])
    if 'offerings' in query:
        serialized_query['offerings'] = format_options(query['offerings'])
    if 'customer_division_types' in query:
        serialized_query['customer_division_types'] = format_options(
            query['customer_division_types']
        )
    if 'customer_roles' in query:
        serialized_query['customer_roles'] = list(query['customer_roles'])
    if 'project_roles' in query:
        serialized_query['project_roles'] = list(query['project_roles'])
    return serialized_query


class BroadcastMessageSerializer(serializers.ModelSerializer):
    author_full_name = serializers.ReadOnlyField(source='author.full_name')
    state = serializers.ReadOnlyField()
    emails = serializers.ReadOnlyField()

    class Meta:
        model = models.BroadcastMessage
        fields = (
            'uuid',
            'created',
            'subject',
            'body',
            'query',
            'author_full_name',
            'emails',
            'state',
            'send_at',
        )

    def validate_query(self, query):
        serializer = QuerySerializer(data=query)
        serializer.is_valid()
        return serializer.validated_data

    def create(self, validated_data):
        current_user = self.context['request'].user
        validated_data['author'] = current_user
        matching_users = utils.get_users_for_query(validated_data['query'])
        validated_data['emails'] = [user.email for user in matching_users if user.email]
        validated_data['query'] = serialize_query(validated_data['query'])
        return super().create(validated_data)

    def update(self, instance, validated_data):
        matching_users = utils.get_users_for_query(validated_data['query'])
        validated_data['emails'] = [user.email for user in matching_users if user.email]
        validated_data['query'] = serialize_query(validated_data['query'])
        return super().update(instance, validated_data)


class DryRunBroadcastMessageSerializer(serializers.Serializer):
    query = QuerySerializer(write_only=True)

    class Meta:
        fields = ('query',)


class UsersBroadcastMessageSerializer(serializers.Serializer):
    query = QuerySerializer(write_only=True)
    project_users = structure_serializers.UserSerializer(many=True, read_only=True)
    customer_users = structure_serializers.UserSerializer(many=True, read_only=True)

    class Meta:
        fields = ('query', 'project_users', 'customer_users')

    def validate(self, attrs):
        attrs = super(UsersBroadcastMessageSerializer, self).validate(attrs)
        query = attrs['query']
        users = utils.get_grouped_users_for_query(query)
        attrs.update(users)
        return attrs


class MessageTemplateSerializer(
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.MessageTemplate
        fields = (
            'url',
            'uuid',
            'name',
            'subject',
            'body',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
