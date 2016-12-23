from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework import serializers

from nodeconductor.core import serializers as core_serializers
from nodeconductor.structure import models as structure_models, SupportedServices

from nodeconductor_assembly_waldur.support import backend

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
    class EventType:
        CREATED = 'jira:issue_created'
        UPDATED = 'jira:issue_updated'
        DELETED = 'jira:issue_deleted'

    def validate(self, attrs):
        return self.initial_data

    def create(self, validated_data):
        fields = validated_data["issue"]["fields"]
        backend_id = validated_data["issue"]["key"]
        link = validated_data['issue']['self']
        caller_backend_id = validated_data['user']['key']
        support_user, _ = models.SupportUser.objects.get_or_create(backend_id=caller_backend_id)
        caller = support_user.user

        event_type = validated_data['webhookEvent']

        issue = None
        if event_type == self.EventType.UPDATED:
            try:
                issue = models.Issue.objects.get(backend_id=backend_id)
            except ObjectDoesNotExist:
                pass
            else:
                issue = self._update_issue(issue=issue, fields=fields, link=link, caller=caller)
        elif event_type == self.EventType.DELETED:
            issue = models.Issue.objects.get(backend_id=backend_id)
            issue.delete()
        else:
            issue = self._create_issue(fields=fields, link=link, caller=caller)

        return issue

    def _create_issue(self, fields, link, caller):
        issue = models.Issue.objects.create(
            resolution=fields['resolution'] or '',
            status=fields['issuetype']['name'],
            link=link,
            impact=self._get_impact_field(fields=fields),
            summary=fields['summary'],
            priority=fields['priority']['name'],
            description=fields['description'],
            type=fields['issuetype']['name'],
            assignee=self._get_support_user_by_field_name(field_name="assignee", fields=fields),
            reporter=self._get_support_user_by_field_name(field_name="reporter", fields=fields),
            caller=caller,
        )

        self._update_comments(issue=issue, fields=fields)
        issue.save()
        return issue

    def _update_issue(self, issue, fields, link, caller):
        issue.resolution = fields['resolution'] or ''
        issue.status = fields['issuetype']['name']
        issue.link = link
        issue.impact = self._get_impact_field(fields=fields)
        issue.summary = fields['summary']
        issue.priority = fields['priority']['name']
        issue.description = fields['description']
        issue.type = fields['issuetype']['name']
        issue.caller = caller

        assignee = self._get_support_user_by_field_name(field_name="assignee", fields=fields)
        if assignee:
            issue.assignee = assignee

        reporter = self._get_support_user_by_field_name(field_name="reporter", fields=fields)
        if reporter:
            issue.reporter = reporter

        self._update_comments(issue=issue, fields=fields)
        issue.save()
        return issue

    def _get_impact_field(self, fields):
        impact_field = ''
        impact_field_name = backend.get_active_backend().project_details.get('impact_field', None)
        if impact_field_name and impact_field_name in fields:
            impact_field = fields[impact_field_name]

        return impact_field

    def _update_comments(self, issue, fields):
        if 'comment' in fields:
            # update comments
            for comment in fields['comment']['comments']:
                author, _ = models.SupportUser.objects.get_or_create(backend_id=comment['author']['key'])
                issue.comments.update_or_create(
                    author=author,
                    description=comment['body'],
                    backend_id=comment['id']
                )

            # delete comments if required
            if fields['comment']['total'] != issue.comments.count():
                ids = [c['id'] for c in fields['comment']['comments']]
                issue.comments.exclude(backend_id__in=ids).delete()

    def _get_support_user_by_field_name(self, fields, field_name):
        support_user = None

        if field_name in fields:
            support_user_backend_key = fields[field_name]['key']

            if support_user_backend_key:
                support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

        return support_user
