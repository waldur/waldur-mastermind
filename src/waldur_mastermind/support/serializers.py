from __future__ import unicode_literals

import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.template import Context, Template
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers, exceptions

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models, SupportedServices, serializers as structure_serializers
from waldur_jira.serializers import WebHookReceiverSerializer as JiraWebHookReceiverSerializer
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend

from . import models

logger = logging.getLogger(__name__)
User = get_user_model()


def render_issue_template(config_name, issue):
    issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})
    if not issue_settings:
        return ''

    raw = issue_settings[config_name]
    template = Template(raw)
    return template.render(Context({'issue': issue}))


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
        choices=[(t, t) for t in settings.WALDUR_SUPPORT['ISSUE']['types']],
        initial=settings.WALDUR_SUPPORT['ISSUE']['types'][0],
        default=settings.WALDUR_SUPPORT['ISSUE']['types'][0])
    is_reported_manually = serializers.BooleanField(
        initial=False, default=False, write_only=True,
        help_text=_('Set true if issue is created by regular user via portal.'))

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
            'first_response_sla', 'template'
        )
        read_only_fields = ('key', 'status', 'resolution', 'backend_id', 'link', 'priority', 'first_response_sla')
        protected_fields = ('customer', 'project', 'resource', 'type', 'caller', 'template')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
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

    def get_fields(self):
        fields = super(IssueSerializer, self).get_fields()

        if 'view' not in self.context:  # On docs generation context does not contain "view".
            return fields

        user = self.context['view'].request.user
        if not user.is_staff and not user.is_support:
            del fields['link']

        return fields

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
                    {'assignee': _('Assignee cannot be defined if issue is reported manually.')})
        else:
            if not attrs.get('caller'):
                raise serializers.ValidationError({'caller': _('This field is required.')})
            reporter = models.SupportUser.objects.filter(
                user=self.context['request'].user, is_active=True).first()
            if not reporter:
                raise serializers.ValidationError(
                    _('You cannot report issues because your help desk account is not connected to profile.'))
            attrs['reporter'] = reporter
        return attrs

    def validate_customer(self, customer):
        """ User has to be customer owner, staff or global support """
        if not customer:
            return customer
        user = self.context['request'].user
        if (not customer or
                user.is_staff or
                user.is_support or
                customer.has_user(user, structure_models.CustomerRole.OWNER)):
            return customer
        raise serializers.ValidationError(_('Only customer owner, staff or support can report customer issues.'))

    def validate_project(self, project):
        if not project:
            return project
        user = self.context['request'].user
        if (not project or
                user.is_staff or
                user.is_support or
                project.customer.has_user(user, structure_models.CustomerRole.OWNER) or
                project.has_user(user, structure_models.ProjectRole.MANAGER) or
                project.has_user(user, structure_models.ProjectRole.ADMINISTRATOR)):
            return project
        raise serializers.ValidationError(
            _('Only customer owner, project manager, project admin, staff or support can report such issue.'))

    def validate_resource(self, resource):
        if resource:
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

        validated_data['description'] = render_issue_template('description', validated_data)
        validated_data['summary'] = render_issue_template('summary', validated_data)
        return super(IssueSerializer, self).create(validated_data)

    def _render_template(self, config_name, issue):
        raw = self.issue_settings[config_name]
        template = Template(raw)
        return template.render(Context({'issue': issue}))


class CommentSerializer(core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    # should be initialized with issue in context on creation
    author_user = serializers.HyperlinkedRelatedField(
        source='author.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )

    author_uuid = serializers.ReadOnlyField(source='author.user.uuid')

    class Meta(object):
        model = models.Comment
        fields = ('url', 'uuid', 'issue', 'issue_key', 'description', 'is_public',
                  'author_name', 'author_uuid', 'author_user', 'backend_id', 'created')
        read_only_fields = ('issue', 'backend_id',)
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
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


class SupportUserSerializer(core_serializers.AugmentedSerializerMixin,
                            serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.SupportUser
        fields = ('url', 'uuid', 'name', 'backend_id', 'user')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            user={'lookup_field': 'uuid', 'view_name': 'user-detail'}
        )


class WebHookReceiverSerializer(JiraWebHookReceiverSerializer):
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
                raise serializers.ValidationError('Issue with id %s does not exist.' % key)

        return issue

    def get_comment(self, issue, key, create):
        comment = None

        try:
            comment = models.Comment.objects.get(issue=issue, backend_id=key)
        except models.Comment.DoesNotExist:
            if not create:
                raise serializers.ValidationError('Comment with id %s does not exist.' % key)

        return comment


WebHookReceiverSerializer.remove_event(['jira:issue_created'])


class OfferingSerializer(structure_serializers.PermissionFieldFilteringMixin,
                         core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    type = serializers.ReadOnlyField(source='template.name')

    template = serializers.HyperlinkedRelatedField(
        queryset=models.OfferingTemplate.objects.all(),
        view_name='support-offering-template-detail',
        lookup_field='uuid',
    )
    plan = serializers.HyperlinkedRelatedField(
        queryset=models.OfferingPlan.objects.all(),
        view_name='support-offering-plan-detail',
        lookup_field='uuid',
        required=False,
        write_only=True,
    )
    state = serializers.ReadOnlyField(source='get_state_display')
    report = serializers.JSONField(required=False)
    template_uuid = serializers.ReadOnlyField(source='template.uuid')

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'name', 'project', 'type', 'template', 'template_uuid', 'plan',
                  'state', 'type_label', 'unit_price',
                  'unit', 'created', 'modified', 'issue', 'issue_name', 'issue_link',
                  'issue_key', 'issue_description', 'issue_uuid', 'issue_status',
                  'project_name', 'project_uuid', 'product_code', 'article_code', 'report')
        read_only_fields = ('type_label', 'issue', 'unit_price', 'unit', 'state', 'product_code', 'article_code')
        protected_fields = ('project', 'type', 'template', 'plan')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-offering-detail'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
            unit_price={'decimal_places': 2},
        )
        related_paths = dict(
            issue=('uuid', 'name', 'status', 'key', 'description', 'link'),
            project=('uuid', 'name',),
        )

    def validate_report(self, report):
        if not isinstance(report, list):
            raise serializers.ValidationError('Report should be an object.')

        if len(report) == 0:
            raise serializers.ValidationError('Report object should contain at least one section.')

        for section in report:
            if not isinstance(section, dict):
                raise serializers.ValidationError('Report section should be an object.')

            if not section.get('header'):
                raise serializers.ValidationError('Report section should contain header.')

            if not section.get('body'):
                raise serializers.ValidationError('Report section should contain body.')

        return report

    def get_filtered_field_names(self):
        return ('project',)


class ConfigurableFormDescriptionMixin(object):
    def _form_description(self, configuration, validated_data):
        result = []

        for key in configuration['order']:
            if key not in validated_data:
                continue

            label = configuration['options'].get(key, {})
            label_value = label.get('label', key)
            result.append('%s: \'%s\'' % (label_value, validated_data[key]))

        if 'description' in validated_data:
            result.append('\n %s' % validated_data['description'])

        return '\n'.join(result)


class OfferingCreateSerializer(OfferingSerializer, ConfigurableFormDescriptionMixin):
    attributes = serializers.JSONField(required=False, write_only=True, allow_null=True)
    description = serializers.CharField(required=False, help_text=_('Description to add to the issue.'))

    class Meta(OfferingSerializer.Meta):
        fields = OfferingSerializer.Meta.fields + ('description', 'attributes')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-offering-detail'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
            project={'lookup_field': 'uuid',
                     'view_name': 'project-detail',
                     'required': True,
                     'allow_empty': False,
                     'allow_null': False},
        )

    def create(self, validated_data):
        """
        Each offering corresponds to the single issue which has next values:
            'project' - a hyperlinked field which must be provided with every request;
            'customer' - customer is extracted from the provided project;
            'caller' - a user who sent a request is considered to be a 'caller' of the issue;
            'summary' - has a format of 'Request for <template_name>' or 'Request for "Support" if empty;
            'description' - combined list of all other fields provided with the request;
        """
        project = validated_data['project']
        template = validated_data['template']
        plan = validated_data.pop('plan', None)

        # Temporary code for backward compatibility
        if not plan:
            plan = template.plans.first()

        # It's okay if there's no plan.
        if plan and plan.template != template:
            raise serializers.ValidationError({
                'plan': _('Plan should be related to the same template.'),
            })

        attributes = validated_data.get('attributes')
        if attributes:
            if not isinstance(template.config, dict) or not(template.config.get('options')):
                raise serializers.ValidationError({'attributes': _('Extra attributes are not allowed.')})
            try:
                validate_options(template.config['options'], attributes)
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({'attributes': exc})
            else:
                validated_data.update(attributes)
        else:
            if template.config.get('options'):
                raise serializers.ValidationError({
                    'attributes': _('This field is required.'),
                })

        offering_configuration = template.config
        type_label = offering_configuration.get('label', template.name)
        issue_details = dict(
            caller=self.context['request'].user,
            project=project,
            customer=project.customer,
            type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
            summary='Request for \'%s\'' % type_label,
            description=self._form_description(offering_configuration, validated_data))
        issue_details['summary'] = render_issue_template('summary', issue_details)
        issue_details['description'] = render_issue_template('description', issue_details)
        issue = models.Issue.objects.create(**issue_details)

        payload = dict(
            issue=issue,
            project=issue.project,
            name=validated_data.get('name'),
            template=template,
        )
        if plan:
            payload.update(dict(
                product_code=plan.product_code,
                article_code=plan.article_code,
                unit_price=plan.unit_price,
                unit=plan.unit,
            ))

        offering = models.Offering.objects.create(**payload)

        return offering


class OfferingCompleteSerializer(serializers.Serializer):
    unit_price = serializers.DecimalField(max_digits=13, decimal_places=7)
    unit = serializers.ChoiceField(choices=models.Offering.Units.CHOICES, default=models.Offering.Units.PER_DAY)

    def update(self, instance, validated_data):
        instance.unit_price = validated_data['unit_price']
        instance.unit = validated_data['unit']
        instance.state = models.Offering.States.OK
        instance.save(update_fields=['state', 'unit_price', 'unit'])
        return instance


class AttachmentSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):

    class Meta(object):
        model = models.Attachment
        fields = ('url', 'uuid', 'issue', 'issue_key', 'created', 'file',
                  'mime_type', 'file_size', 'thumbnail', 'backend_id', )
        read_only_fields = ('backend_id',)
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        )
        related_paths = dict(
            issue=('key',),
        )

    def validate(self, attrs):
        filename, file_extension = os.path.splitext(attrs['file'].name)
        if file_extension in settings.WALDUR_SUPPORT['EXCLUDED_ATTACHMENT_TYPES']:
            raise serializers.ValidationError(_('Invalid file extension'))

        user = self.context['request'].user
        issue = attrs['issue']
        if user.is_staff or \
                (issue.customer and issue.customer.has_user(user, structure_models.CustomerRole.OWNER)) or \
                issue.caller == user:
            return attrs

        raise exceptions.PermissionDenied()


class TemplateAttachmentSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.TemplateAttachment
        fields = ('name', 'file')


class TemplateSerializer(serializers.HyperlinkedModelSerializer):
    attachments = TemplateAttachmentSerializer(many=True)

    class Meta(object):
        model = models.Template
        fields = ('url', 'uuid',
                  'name', 'native_name',
                  'description', 'native_description',
                  'issue_type', 'attachments')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-template-detail'},
        )

    def get_fields(self):
        fields = super(TemplateSerializer, self).get_fields()
        if not settings.WALDUR_CORE['NATIVE_NAME_ENABLED']:
            del fields['native_name']
            del fields['native_description']
        return fields


class OfferingTemplateSerializer(serializers.HyperlinkedModelSerializer):

    class Meta(object):
        model = models.OfferingTemplate
        fields = ('url', 'uuid', 'name', 'config')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-offering-template-detail'},
        )


class OfferingPlanSerializer(serializers.HyperlinkedModelSerializer):

    class Meta(object):
        model = models.OfferingPlan
        fields = ('url', 'uuid', 'product_code', 'article_code', 'unit', 'unit_price')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-offering-plan-detail'},
        )
