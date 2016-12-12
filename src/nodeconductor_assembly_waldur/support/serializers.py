from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from nodeconductor.core import serializers as core_serializers
from nodeconductor.structure import models as structure_models, SupportedServices

from . import models


User = get_user_model()


class IssueSerializer(core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):
    resource = core_serializers.GenericRelatedField(
        related_models=structure_models.ResourceMixin.get_all_models(), required=False)
    reporter_user = serializers.HyperlinkedRelatedField(
        source='reporter.user',
        view_name='user-detail',
        lookup_field='uuid',
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )
    caller_user = serializers.HyperlinkedRelatedField(
        source='caller.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )
    assignee_user = serializers.HyperlinkedRelatedField(
        source='assignee.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )
    resource_type = serializers.SerializerMethodField()
    resource_name = serializers.ReadOnlyField(source='resource.name')
    type = serializers.ChoiceField(
        choices=[(t, t) for t in settings.WALDUR_SUPPORT['ISSUE_TYPES']],
        initial=settings.WALDUR_SUPPORT['DEFAULT_ISSUE_TYPE'],
        default=settings.WALDUR_SUPPORT['DEFAULT_ISSUE_TYPE'])

    class Meta(object):
        model = models.Issue
        fields = (
            'url', 'uuid', 'type', 'key', 'backend_id', 'link',
            'summary', 'description', 'status', 'resolution', 'priority',
            'reporter_name', 'reporter_user',
            'caller_name', 'caller_user',
            'assignee_name', 'assignee_user',
            'customer', 'customer_uuid', 'customer_name',
            'project', 'project_uuid', 'project_name',
            'resource', 'resource_type', 'resource_name',
            'created', 'modified',
        )
        read_only_fields = ('key', 'status', 'resolution', 'backend_id', 'link', 'priority')
        protected_fields = ('customer', 'project', 'resource', 'type', 'reporter_user')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
            customer={'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
        )
        related_paths = dict(
            reporter=('name',),
            caller=('name',),
            assignee=('name',),
            customer=('uuid', 'name',),
            project=('uuid', 'name',),
        )

    def get_resource_type(self, obj):
        if obj.resource:
            return SupportedServices.get_name_for_model(obj.resource_content_type.model_class())

    @transaction.atomic()
    def create(self, validated_data):
        caller_user = self.context['request'].user
        reporter_user = validated_data.get('reporter', {}).get('user') or caller_user
        validated_data['reporter'], _ = models.SupportUser.objects.get_or_create_from_user(reporter_user)
        validated_data['caller'], _ = models.SupportUser.objects.get_or_create_from_user(caller_user)

        resource = validated_data.get('resource')
        if resource:
            validated_data['project'] = resource.service_project_link.project
        project = validated_data.get('project')
        if project:
            validated_data['customer'] = project.customer

        return super(IssueSerializer, self).create(validated_data)


class CommentSerializer(core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    # should be initialized with issue in context on creation
    author_user = serializers.HyperlinkedRelatedField(
        source='author.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )

    class Meta(object):
        model = models.Comment
        fields = ('url', 'uuid', 'issue', 'issue_key', 'description', 'is_public',
                  'author_name', 'author_user', 'backend_id', 'created')
        read_only_fields = ('issue', 'backend_id',)
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-comment-detail'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        )
        related_paths = dict(
            author=('name',),
            issue=('key',),
        )

    @transaction.atomic()
    def create(self, validated_data):
        author_user = self.context['request'].user
        validated_data['author'], _ = models.SupportUser.objects.get_or_create_from_user(author_user)
        validated_data['issue'] = self.context['issue']
        return super(CommentSerializer, self).create(validated_data)
