import logging
import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.template import Context, Template
from django.template import exceptions as template_exceptions
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core.utils import is_uuid_like
from waldur_core.media.serializers import ProtectedMediaSerializerMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure.registry import get_resource_type
from waldur_jira import serializers as jira_serializers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend

from . import backend, models

logger = logging.getLogger(__name__)
User = get_user_model()


def render_issue_template(config_name, issue):
    try:
        template = get_template('support/' + config_name + '.txt').template
    except template_exceptions.TemplateDoesNotExist:
        issue_settings = settings.WALDUR_ATLASSIAN.get('ISSUE', {})
        if not issue_settings:
            return ''

        raw = issue_settings[config_name]
        template = Template(raw)

    return template.render(
        Context({'issue': issue, 'settings': settings}, autoescape=False)
    )


class NestedFeedbackSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    evaluation = serializers.ReadOnlyField(source='get_evaluation_display')
    evaluation_number = serializers.ReadOnlyField(source='evaluation')

    class Meta:
        model = models.Feedback
        fields = (
            'evaluation',
            'evaluation_number',
            'comment',
            'state',
        )


class IssueSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    resource = core_serializers.GenericRelatedField(
        related_models=structure_models.BaseResource.get_all_models()
        + [marketplace_models.Resource],
        required=False,
    )
    caller = serializers.HyperlinkedRelatedField(
        view_name='user-detail',
        lookup_field='uuid',
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )
    reporter = serializers.HyperlinkedRelatedField(
        view_name='support-user-detail', lookup_field='uuid', read_only=True
    )
    assignee = serializers.HyperlinkedRelatedField(
        view_name='support-user-detail',
        lookup_field='uuid',
        queryset=models.SupportUser.objects.all(),
        required=False,
        allow_null=True,
    )
    template = serializers.HyperlinkedRelatedField(
        view_name='support-template-detail',
        lookup_field='uuid',
        queryset=models.Template.objects.all(),
        required=False,
        allow_null=True,
    )
    resource_type = serializers.SerializerMethodField()
    resource_name = serializers.ReadOnlyField(source='resource.name')
    type = serializers.ChoiceField(
        choices=[(t, t) for t in settings.WALDUR_ATLASSIAN['ISSUE']['types']],
        initial=settings.WALDUR_ATLASSIAN['ISSUE']['types'][0],
        default=settings.WALDUR_ATLASSIAN['ISSUE']['types'][0],
    )
    is_reported_manually = serializers.BooleanField(
        initial=False,
        default=False,
        write_only=True,
        help_text=_('Set true if issue is created by regular user via portal.'),
    )
    feedback = NestedFeedbackSerializer(required=False)
    update_is_available = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()

    class Meta:
        model = models.Issue
        fields = (
            'url',
            'uuid',
            'type',
            'key',
            'backend_id',
            'remote_id',
            'link',
            'summary',
            'description',
            'status',
            'resolution',
            'priority',
            'caller',
            'caller_uuid',
            'caller_full_name',
            'reporter',
            'reporter_uuid',
            'reporter_name',
            'assignee',
            'assignee_uuid',
            'assignee_name',
            'customer',
            'customer_uuid',
            'customer_name',
            'project',
            'project_uuid',
            'project_name',
            'resource',
            'resource_type',
            'resource_name',
            'created',
            'modified',
            'is_reported_manually',
            'first_response_sla',
            'template',
            'feedback',
            'resolved',
            'update_is_available',
            'destroy_is_available',
        )
        read_only_fields = (
            'key',
            'status',
            'resolution',
            'backend_id',
            'link',
            'first_response_sla',
            'feedback',
        )
        protected_fields = (
            'customer',
            'project',
            'resource',
            'type',
            'caller',
            'template',
            'priority',
            'remote_id',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            customer={'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
        )
        related_paths = dict(
            caller=(
                'uuid',
                'full_name',
            ),
            reporter=(
                'uuid',
                'name',
            ),
            assignee=(
                'uuid',
                'name',
            ),
            customer=(
                'uuid',
                'name',
            ),
            project=(
                'uuid',
                'name',
            ),
        )

    def get_fields(self):
        fields = super().get_fields()

        if (
            'view' not in self.context
        ):  # On docs generation context does not contain "view".
            return fields

        user = self.context['view'].request.user
        if not user.is_staff and not user.is_support:
            del fields['link']

        return fields

    def get_resource_type(self, obj):
        if isinstance(obj.resource, structure_models.BaseResource):
            return get_resource_type(obj.resource_content_type.model_class())
        if isinstance(obj.resource, marketplace_models.Resource):
            return 'Marketplace.Resource'

    def get_update_is_available(self, obj):
        return backend.get_active_backend().comment_update_is_available(obj)

    def get_destroy_is_available(self, obj):
        return backend.get_active_backend().comment_update_is_available(obj)

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        request_user = self.context['request'].user
        if attrs.pop('is_reported_manually'):
            attrs['caller'] = request_user
            if attrs.get('assignee'):
                raise serializers.ValidationError(
                    {
                        'assignee': _(
                            'Assignee cannot be defined if issue is reported manually.'
                        )
                    }
                )
        else:
            # create a request on behalf of an agent
            if not attrs.get('caller'):
                raise serializers.ValidationError(
                    {'caller': _('This field is required.')}
                )
            # if change of reporter is supported, use it
            if settings.WALDUR_ATLASSIAN['MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS']:
                reporter = models.SupportUser.objects.filter(
                    user=request_user,
                    is_active=True,
                    backend_name=backend.get_active_backend().backend_name,
                ).first()
                if not reporter:
                    raise serializers.ValidationError(
                        _(
                            'You cannot report issues because your help desk account is not connected to profile.'
                        )
                    )
                attrs['reporter'] = reporter
            else:
                # leave a mark about reporter in the description field
                attrs[
                    'description'
                ] = f'Reported by {request_user.full_name}.\n\n' + attrs.get(
                    'description', ''
                )

        return attrs

    def validate_summary(self, summary):
        """
        Remove leading and trailing spaces from summary.
        """
        return summary.strip()

    def validate_customer(self, customer):
        """User has to be customer owner, staff or global support"""
        if not customer:
            return customer
        user = self.context['request'].user
        if (
            not customer
            or user.is_staff
            or user.is_support
            or customer.has_user(user, structure_models.CustomerRole.OWNER)
        ):
            return customer
        raise serializers.ValidationError(
            _('Only customer owner, staff or support can report customer issues.')
        )

    def validate_project(self, project):
        if not project:
            return project
        user = self.context['request'].user
        if (
            not project
            or user.is_staff
            or user.is_support
            or project.customer.has_user(user, structure_models.CustomerRole.OWNER)
            or project.has_user(user, structure_models.ProjectRole.MANAGER)
            or project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR)
            or project.has_user(user, structure_models.ProjectRole.MEMBER)
        ):
            return project
        raise serializers.ValidationError(
            _(
                'Only customer owner, project manager, project admin, project support, staff or support can report such issue.'
            )
        )

    def validate_resource(self, resource):
        if resource:
            self.validate_project(resource.project)
        return resource

    def validate_priority(self, priority):
        user = self.context['request'].user
        if not user.is_staff and not user.is_support:
            raise serializers.ValidationError(
                _('Only staff or support can specify issue priority.')
            )
        try:
            models.Priority.objects.get(name=priority)
        except (models.Priority.DoesNotExist, models.Priority.MultipleObjectsReturned):
            raise serializers.ValidationError(
                _('Priority with requested name does not exist.')
            )
        return priority

    @transaction.atomic()
    def create(self, validated_data):
        resource = validated_data.get('resource')
        if resource:
            validated_data['project'] = resource.project
        project = validated_data.get('project')
        if project:
            validated_data['customer'] = project.customer

        validated_data['description'] = render_issue_template(
            'description', validated_data
        )
        validated_data['summary'] = render_issue_template('summary', validated_data)
        return super().create(validated_data)

    def _render_template(self, config_name, issue):
        raw = self.issue_settings[config_name]
        template = Template(raw)
        return template.render(Context({'issue': issue}))


class PrioritySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.Priority
        fields = ('url', 'uuid', 'name', 'description', 'icon_url')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class CommentSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    # should be initialized with issue in context on creation
    author_user = serializers.HyperlinkedRelatedField(
        source='author.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )

    author_uuid = serializers.ReadOnlyField(source='author.user.uuid')
    author_email = serializers.ReadOnlyField(source='author.user.email')
    update_is_available = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()

    class Meta:
        model = models.Comment
        fields = (
            'url',
            'uuid',
            'issue',
            'issue_key',
            'description',
            'is_public',
            'author_name',
            'author_uuid',
            'author_user',
            'author_email',
            'backend_id',
            'remote_id',
            'created',
            'update_is_available',
            'destroy_is_available',
        )
        read_only_fields = (
            'issue',
            'backend_id',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        )
        related_paths = dict(
            author=('name',),
            issue=('key',),
        )
        protected_fields = ('remote_id',)

    def get_update_is_available(self, obj):
        return backend.get_active_backend().comment_update_is_available(obj)

    def get_destroy_is_available(self, obj):
        return backend.get_active_backend().comment_update_is_available(obj)

    @transaction.atomic()
    def create(self, validated_data):
        author_user = self.context['request'].user
        (
            validated_data['author'],
            _,
        ) = models.SupportUser.objects.get_or_create_from_user(author_user)
        validated_data['issue'] = self.context['view'].get_object()
        return super().create(validated_data)


class SupportUserSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.SupportUser
        fields = ('url', 'uuid', 'name', 'backend_id', 'user', 'backend_name')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            user={'lookup_field': 'uuid', 'view_name': 'user-detail'},
        )


class WebHookReceiverSerializer(jira_serializers.WebHookReceiverSerializer):
    def get_project(self, project_key):
        class Project:
            def get_backend(self):
                return ServiceDeskBackend()

        return Project()

    def get_issue(self, project, key, create):
        issue = None

        try:
            issue = models.Issue.objects.get(backend_id=key)
        except models.Issue.DoesNotExist:
            if not create:
                raise serializers.ValidationError(
                    'Issue with id %s does not exist.' % key
                )

        return issue

    def get_comment(self, issue, key, create):
        comment = None

        try:
            comment = models.Comment.objects.get(issue=issue, backend_id=key)
        except models.Comment.DoesNotExist:
            if not create:
                raise serializers.ValidationError(
                    'Comment with id %s does not exist.' % key
                )

        return comment


WebHookReceiverSerializer.remove_event(['jira:issue_created'])


class AttachmentSerializer(
    ProtectedMediaSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    file_name = serializers.SerializerMethodField()
    destroy_is_available = serializers.SerializerMethodField()

    class Meta:
        model = models.Attachment
        fields = (
            'url',
            'uuid',
            'issue',
            'issue_key',
            'created',
            'file',
            'mime_type',
            'file_size',
            'file_name',
            'thumbnail',
            'backend_id',
            'destroy_is_available',
        )
        read_only_fields = (
            'mime_type',
            'file_size',
            'file_name',
            'thumbnail',
            'backend_id',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        )
        related_paths = dict(
            issue=('key',),
        )

    def get_file_name(self, attachment):
        _, file_name = os.path.split(attachment.file.name)
        return file_name

    def get_destroy_is_available(self, obj):
        return backend.get_active_backend().attachment_destroy_is_available(obj)

    def validate(self, attrs):
        filename, file_extension = os.path.splitext(attrs['file'].name)
        if file_extension in settings.WALDUR_ATLASSIAN['EXCLUDED_ATTACHMENT_TYPES']:
            raise serializers.ValidationError(_('Invalid file extension'))

        user = self.context['request'].user
        issue = attrs['issue']
        if (
            user.is_staff
            or (
                issue.customer
                and issue.customer.has_user(user, structure_models.CustomerRole.OWNER)
            )
            or issue.caller == user
        ):
            return attrs

        raise exceptions.PermissionDenied()


class TemplateAttachmentSerializer(
    ProtectedMediaSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = models.TemplateAttachment
        fields = ('name', 'file')


class TemplateSerializer(serializers.HyperlinkedModelSerializer):
    attachments = TemplateAttachmentSerializer(many=True)

    class Meta:
        model = models.Template
        fields = (
            'url',
            'uuid',
            'name',
            'native_name',
            'description',
            'native_description',
            'issue_type',
            'attachments',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-template-detail'},
        )

    def get_fields(self):
        fields = super().get_fields()
        if not settings.WALDUR_CORE['NATIVE_NAME_ENABLED']:
            del fields['native_name']
            del fields['native_description']
        return fields


class CreateFeedbackSerializer(serializers.HyperlinkedModelSerializer):
    token = serializers.CharField(required=True, write_only=True)

    class Meta:
        model = models.Feedback
        fields = (
            'uuid',
            'issue',
            'comment',
            'evaluation',
            'token',
        )

        read_only_fields = ('issue',)
        extra_kwargs = dict(
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        )

    def validate(self, attrs):
        token = attrs.pop('token')
        signer = signing.TimestampSigner()
        try:
            issue_uuid = signer.unsign(
                token, max_age=timedelta(days=settings.ISSUE_FEEDBACK_TOKEN_PERIOD)
            )

            if not is_uuid_like(issue_uuid):
                raise serializers.ValidationError(
                    {'token': _('UUID:%s is not valid.') % issue_uuid}
                )

            issue = models.Issue.objects.get(uuid=issue_uuid)

            if models.Feedback.objects.filter(issue=issue).exists():
                raise serializers.ValidationError(
                    _('Feedback for this issue already exists.')
                )
        except signing.BadSignature:
            raise serializers.ValidationError({'token': _('Token is wrong.')})
        except models.Issue.DoesNotExist:
            raise serializers.ValidationError(_('An issue is not found.'))

        attrs['issue'] = issue
        return attrs


class FeedbackSerializer(serializers.HyperlinkedModelSerializer):
    issue_uuid = serializers.ReadOnlyField(source='issue.uuid')
    issue_key = serializers.ReadOnlyField(source='issue.key')
    user_full_name = serializers.ReadOnlyField(source='issue.caller.full_name')
    issue_summary = serializers.ReadOnlyField(source='issue.summary')

    class Meta:
        model = models.Feedback
        fields = (
            'uuid',
            'created',
            'modified',
            'state',
            'evaluation',
            'comment',
            'issue_uuid',
            'user_full_name',
            'issue_key',
            'issue_summary',
        )
