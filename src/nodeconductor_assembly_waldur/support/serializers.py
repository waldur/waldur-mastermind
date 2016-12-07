from rest_framework import serializers

from nodeconductor.core import serializers as core_serializers
from nodeconductor.structure import models as structure_models

from . import models


class IssueSerializer(core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):

    scope = core_serializers.GenericRelatedField(
        related_models=structure_models.ResourceMixin.get_all_models(), required=False)

    class Meta(object):
        model = models.Issue
        fields = (
            'url', 'uuid', 'type', 'key',
            'summary', 'description', 'status', 'resolution',
            'reporter', 'reporter_uuid', 'reporter_name', 'reporter_email',
            'creator', 'creator_uuid', 'creator_name', 'creator_email',
            'assignee', 'assignee_uuid', 'assignee_name', 'assignee_email',
            'customer', 'customer_uuid', 'customer_name',
            'project', 'project_uuid', 'project_name',
            'scope', 'created', 'modified',
        )
        read_only_fields = 'key', 'status', 'resolution', 'creator'
        protected_fields = 'customer', 'project', 'scope', 'type', 'reporter'
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'waldur-issues-detail'},
            reporter={'lookup_field': 'uuid', 'view_name': 'user-detail'},
            creator={'lookup_field': 'uuid', 'view_name': 'user-detail'},
            assignee={'lookup_field': 'uuid', 'view_name': 'user-detail'},
            customer={'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
        )
        related_paths = dict(
            reporter=('uuid', 'name', 'email'),
            creator=('uuid', 'name', 'email'),
            assignee=('uuid', 'name', 'email'),
            customer=('uuid', 'name'),
            project=('uuid', 'name'),
        )

    def create(self, validated_data):
        validated_data['creator'] = self.context['request'].user
        return super(IssueSerializer, self).create(validated_data)
