from datetime import datetime

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
            'first_response_sla',
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

    def validate_customer(self, customer):
        """ User has to be customer owner or staff """
        user = self.context['request'].user
        if not customer or user.is_staff or customer.has_user(user, structure_models.CustomerRole.OWNER):
            return customer
        raise serializers.ValidationError('Only customer owner or staff can report customer issues.')

    def validate_project(self, project):
        user = self.context['request'].user
        if (not project or user.is_staff or
                project.customer.has_user(user, structure_models.CustomerRole.OWNER) or
                project.has_user(user, structure_models.ProjectRole.MANAGER) or
                project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR)):
            return project
        raise serializers.ValidationError(
            'Only customer owner, project manager, project admin or staff can report such issue.')

    def validate_resource(self, resource):
        self.validate_project(resource.service_project_link.project)
        return resource

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
        validated_data['issue'] = self.context['view'].get_object()
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
    TIME_TO_RESPONSE_NAME = 'Time to first response'

    class EventType:
        CREATED = 'jira:issue_created'
        UPDATED = 'jira:issue_updated'
        DELETED = 'jira:issue_deleted'

    @transaction.atomic()
    def save(self, **kwargs):
        fields = self.initial_data['issue']['fields']
        backend_id = self.initial_data['issue']['key']
        link = self.initial_data['issue']['self']

        event_type = self.initial_data['webhookEvent']

        if event_type == self.EventType.UPDATED:
            try:
                issue = models.Issue.objects.get(backend_id=backend_id)
            except models.Issue.DoesNotExist:
                pass
            else:
                self._update_issue(issue=issue, fields=fields, link=link)
        elif event_type == self.EventType.DELETED:
            issue = models.Issue.objects.get(backend_id=backend_id)
            issue.delete()

    def _update_issue(self, issue, fields, link):
        issue.resolution = fields['resolution'] or ''
        issue.status = fields['issuetype']['name']
        issue.link = link
        issue.impact = self._get_impact_field(fields=fields)
        issue.summary = fields['summary']
        issue.priority = fields['priority']['name']
        issue.description = fields['description']
        issue.type = fields['issuetype']['name']

        custom_field_values = [fields[customfield] for customfield in fields if customfield.startswith('customfield')]
        self._update_custom_fields(issue, custom_field_values)

        assignee = self._get_support_user_by_field_name(field_name='assignee', fields=fields)
        if assignee:
            issue.assignee = assignee

        reporter = self._get_support_user_by_field_name(field_name='reporter', fields=fields)
        if reporter:
            issue.reporter = reporter
            issue.caller = reporter.user

        if 'comment' in fields:
            self._update_comments(issue=issue, fields=fields)

        issue.save()
        return issue

    def _update_custom_fields(self, issue, custom_field_values):
        for field in custom_field_values:
            if isinstance(field, dict):
                name = field.get('name', None)
                if name == self.TIME_TO_RESPONSE_NAME:
                    ongoing_cycle = field.get('ongoingCycle', {})
                    breach_time = ongoing_cycle.get('breachTime', {})
                    epoch_milliseconds = breach_time.get('epochMillis', None)
                    if epoch_milliseconds:
                        issue.first_response_sla = datetime.fromtimestamp(epoch_milliseconds / 1000.0)

    def _get_impact_field(self, fields):
        project_settings = settings.WALDUR_SUPPORT.get('PROJECT', {})
        impact_field_name = project_settings.get('impact_field', None)
        return fields.get(impact_field_name, '')

    @transaction.atomic()
    def _update_comments(self, issue, fields):
        backend_comments = {c['id']: c for c in fields['comment']['comments']}
        comments = {c.backend_id: c for c in issue.comments.all()}

        for exist_comment_id in set(backend_comments) & set(comments):
            backend_comment = backend_comments[exist_comment_id]
            comment = comments[exist_comment_id]
            if comment.description != backend_comment['body']:
                comment.description = backend_comment['body']
                comment.save()

        for new_comment_id in set(backend_comments) - set(comments):
            backend_comment = backend_comments[new_comment_id]
            author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_comment['author']['key'])
            models.Comment.objects.create(
                issue=issue,
                author=author,
                description=backend_comment['body'],
                backend_id=backend_comment['id'],
            )

        models.Comment.objects.filter(backend_id__in=set(comments) - set(backend_comments)).delete()

    def _get_support_user_by_field_name(self, fields, field_name):
        support_user = None

        if field_name in fields:
            support_user_backend_key = fields[field_name]['key']

            if support_user_backend_key:
                support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

        return support_user

