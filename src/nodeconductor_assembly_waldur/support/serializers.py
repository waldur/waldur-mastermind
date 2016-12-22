from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import ObjectDoesNotExist
from rest_framework import serializers

from nodeconductor.core import serializers as core_serializers
from nodeconductor.structure import models as structure_models, SupportedServices

from nodeconductor_jira import serializers as jira_serializers
from nodeconductor_jira import models as jira_models

from . import models


User = get_user_model()


class IssueSerializer(core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):
    resource = core_serializers.GenericRelatedField(
        related_models=structure_models.ResourceMixin.get_all_models(), required=False)
    caller = serializers.HyperlinkedRelatedField(
        view_name='user-detail',
        lookup_field='uuid',
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )
    reporter = serializers.HyperlinkedRelatedField(
        view_name='support-user-detail',
        lookup_field='uuid',
        read_only=True
    )
    assignee = serializers.HyperlinkedRelatedField(
        view_name='support-user-detail',
        lookup_field='uuid',
        queryset=models.SupportUser.objects.all(),
        required=False,
        allow_null=True,
    )
    resource_type = serializers.SerializerMethodField()
    resource_name = serializers.ReadOnlyField(source='resource.name')
    type = serializers.ChoiceField(
        choices=[(t, t) for t in settings.WALDUR_SUPPORT['ISSUE_TYPES']],
        initial=settings.WALDUR_SUPPORT['DEFAULT_ISSUE_TYPE'],
        default=settings.WALDUR_SUPPORT['DEFAULT_ISSUE_TYPE'])
    is_reported_manually = serializers.BooleanField(
        initial=False, default=False, write_only=True,
        help_text='Set true if issue is created by regular user via portal.')

    class Meta(object):
        model = models.Issue
        fields = (
            'url', 'uuid', 'type', 'key', 'backend_id', 'link',
            'summary', 'description', 'status', 'resolution', 'priority',
            'caller', 'caller_uuid', 'caller_full_name',
            'reporter', 'reporter_uuid', 'reporter_name',
            'assignee', 'assignee_uuid', 'assignee_name',
            'customer', 'customer_uuid', 'customer_name',
            'project', 'project_uuid', 'project_name',
            'resource', 'resource_type', 'resource_name',
            'created', 'modified', 'is_reported_manually',
        )
        read_only_fields = ('key', 'status', 'resolution', 'backend_id', 'link', 'priority')
        protected_fields = ('customer', 'project', 'resource', 'type', 'caller')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
            customer={'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
        )
        related_paths = dict(
            caller=('uuid', 'full_name',),
            reporter=('uuid', 'name',),
            assignee=('uuid', 'name',),
            customer=('uuid', 'name',),
            project=('uuid', 'name',),
        )

    def get_resource_type(self, obj):
        if obj.resource:
            return SupportedServices.get_name_for_model(obj.resource_content_type.model_class())

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        if attrs.pop('is_reported_manually'):
            attrs['caller'] = self.context['request'].user
            if attrs.get('assignee'):
                raise serializers.ValidationError(
                    {'assignee': 'Assignee cannot be defined if issue is reported manually.'})
        else:
            if not attrs.get('caller'):
                raise serializers.ValidationError({'caller': 'This field is required.'})
            reporter = models.SupportUser.objects.filter(user=self.context['request'].user).first()
            if not reporter:
                raise serializers.ValidationError(
                    'You cannot report issues because your help desk account is not connected to profile.')
            attrs['reporter'] = reporter
        return attrs

    @transaction.atomic()
    def create(self, validated_data):
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


class SupportUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.SupportUser
        fields = ('url', 'uuid', 'name', 'backend_id', 'user')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-user-detail'},
            user={'lookup_field': 'uuid', 'view_name': 'user-detail'}
        )


class WebHookReceiverSerializer(serializers.Serializer):

    class Meta(object):
        model = models.Issue
        jira_webhook_serializer = jira_serializers.WebHookReceiverSerializer()

    def validate(self, attrs):
        return self.initial_data

    def create(self, validated_data):
        fields = validated_data["issue"]["fields"]

        # TODO [TM:12/22/16] move serializer to assembly or find a better way to reuse it.
        data = self.Meta.jira_webhook_serializer.create(validated_data=validated_data)
        backend_id = data['issue']['key']
        jira_issue = jira_models.Issue.objects.get(backend_id=backend_id)
        backend_issue = models.Issue.objects.get(backend_id=backend_id)

        backend_issue.key = jira_issue.key
        backend_issue.resolution = jira_issue.resolution
        backend_issue.status = jira_issue.status
        backend_issue.link = jira_issue.get_access_url()
        backend_issue.priority = jira_issue.priority
        backend_issue.summary = jira_issue.summary
        backend_issue.impact = jira_issue.impact
        self._update_assigne(backend_issue=backend_issue, fields=fields)
        backend_issue.save()

        return backend_issue

    def _update_assigne(self, backend_issue, fields):
        field_name = "assignee"
        if field_name in fields:
            assignee_email = fields[field_name]['emailAddress']
            if assignee_email:
                try:
                    assignee = models.SupportUser.objects.get(user__email=assignee_email)
                except ObjectDoesNotExist:
                    pass
                else:
                    backend_issue.assignee = assignee
