from __future__ import unicode_literals

import copy
import logging
import os

import dateutil.parser
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError
from django.template import Context, Template
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
import jira
from rest_framework import serializers, exceptions
import six

from waldur_core.core import serializers as core_serializers, utils as core_utils
from waldur_core.structure import models as structure_models, SupportedServices, serializers as structure_serializers
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend

from . import models, backend

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
            'first_response_sla',
        )
        read_only_fields = ('key', 'status', 'resolution', 'backend_id', 'link', 'priority', 'first_response_sla')
        protected_fields = ('customer', 'project', 'resource', 'type', 'caller')
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
            reporter = models.SupportUser.objects.filter(user=self.context['request'].user).first()
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


class AttachmentSynchronizer(object):
    def __init__(self, current_issue, backend_id):
        self.current_issue = current_issue
        self.backend_issue = self._get_backend_issue(backend_id)

    def perform_update(self):
        if self.stale_attachment_ids:
            models.Attachment.objects.filter(backend_id__in=self.stale_attachment_ids).delete()

        for attachment_id in self.new_attachment_ids:
            self._add_attachment(
                self.current_issue,
                self.get_backend_attachment(attachment_id)
            )

        for attachment_id in self.updated_attachments_ids:
            self._update_attachment(
                self.current_issue,
                self.get_backend_attachment(attachment_id),
                self.get_current_attachment(attachment_id)
            )

    def get_current_attachment(self, attachment_id):
        return self.current_attachments_map[attachment_id]

    def get_backend_attachment(self, attachment_id):
        return self.backend_attachments_map[attachment_id]

    @cached_property
    def current_attachments_map(self):
        return {
            six.text_type(attachment.backend_id): attachment
            for attachment in self.current_issue.attachments.all()
        }

    @cached_property
    def current_attachments_ids(self):
        return set(self.current_attachments_map.keys())

    @cached_property
    def backend_attachments_map(self):
        return {
            six.text_type(attachment['id']): attachment
            for attachment in self.backend_issue['attachment']
        }

    @cached_property
    def backend_attachments_ids(self):
        return set(self.backend_attachments_map.keys())

    @cached_property
    def stale_attachment_ids(self):
        return self.current_attachments_ids - self.backend_attachments_ids

    @cached_property
    def new_attachment_ids(self):
        return self.backend_attachments_ids - self.current_attachments_ids

    @cached_property
    def updated_attachments_ids(self):
        return filter(self._is_attachment_updated, self.backend_attachments_ids)

    def _is_attachment_updated(self, attachment_id):
        """
        Attachment is considered updated if its thumbnail just has been created.
        """

        if 'thumbnail' not in self.get_backend_attachment(attachment_id):
            return False

        if attachment_id not in self.current_attachments_ids:
            return False

        if self.get_current_attachment(attachment_id).thumbnail:
            return False

        return True

    def _download_file(self, url):
        """
        Download file from URL using secure JIRA session.
        :return: byte stream
        :raises: requests.RequestException
        """
        session = backend.get_active_backend().manager._session
        response = session.get(url)
        response.raise_for_status()
        return six.BytesIO(response.content)

    def _get_backend_issue(self, backend_id):
        manager = backend.get_active_backend().manager
        return manager.issue(backend_id).raw['fields']

    def _add_attachment(self, issue, backend_attachment):
        try:
            content = self._download_file(backend_attachment['content'])
            if 'thumbnail' in backend_attachment:
                thumbnail_content = self._download_file(backend_attachment['thumbnail'])

        except jira.JIRAError as error:
            logger.error('Unable to load attachment for issue with backend id {backend_id}. Error: {error}).'
                         .format(backend_id=issue.backend_id, error=error))
            return

        author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_attachment['author']['key'])
        try:
            waldur_attachment = models.Attachment.objects.create(
                issue=issue,
                backend_id=backend_attachment['id'],
                mime_type=backend_attachment['mimeType'],
                file_size=backend_attachment['size'],
                created=dateutil.parser.parse(backend_attachment['created']),
                author=author
            )
        except IntegrityError:
            waldur_attachment = models.Attachment.objects.get(
                backend_id=backend_attachment['id']
            )
        waldur_attachment.file.save(backend_attachment['filename'], content, save=True)
        if 'thumbnail' in backend_attachment:
            waldur_attachment.thumbnail.save(backend_attachment['filename'], thumbnail_content, save=True)

    def _update_attachment(self, issue, backend_attachment, current_attachment):
        try:
            content = self._download_file(backend_attachment['thumbnail'])
        except jira.JIRAError as error:
            logger.error('Unable to load attachment thumbnail for issue with backend id {backend_id}. Error: {error}).'
                         .format(backend_id=issue.backend_id, error=error))
            return

        current_attachment.thumbnail.save(backend_attachment['filename'], content, save=True)


class WebHookReceiverSerializer(serializers.Serializer):
    class EventType:
        CREATED = 'jira:issue_created'
        UPDATED = 'jira:issue_updated'
        DELETED = 'jira:issue_deleted'

    PUBLIC_COMMENT_KEY = 'sd.public.comment'

    def validate(self, attrs):
        if 'issue' not in self.initial_data:
            raise serializers.ValidationError(_('"issue" is missing in request data. Cannot process issue.'))

        if 'webhookEvent' not in self.initial_data:
            raise serializers.ValidationError(_('"webhookEvent" is missing in request data. Cannot find out even type'))

        return attrs

    def save(self, **kwargs):
        fields = self.initial_data['issue']['fields']
        backend_id = self.initial_data['issue']['key']

        event_type = self.initial_data['webhookEvent']
        if event_type == self.EventType.UPDATED:
            try:
                issue = models.Issue.objects.get(backend_id=backend_id)
            except models.Issue.DoesNotExist:
                logger.warning('Skipping issue update with ID=%s because it does not exist in Waldur', backend_id)
                pass
            else:
                backend_issue = self._get_backend_issue(fields=fields)
                self._update_issue(issue=issue, backend_issue=backend_issue)

                if 'comment' in fields:
                    backend_comments = self._get_backend_comments(issue.key, fields)
                    self._update_comments(issue=issue, backend_comments=backend_comments)

                if 'attachment' in fields:
                    AttachmentSynchronizer(issue, backend_id).perform_update()

        elif event_type == self.EventType.DELETED:
            issue = models.Issue.objects.get(backend_id=backend_id)
            issue.delete()

    def _get_backend_issue(self, fields):
        """
        Builds a dictionary of issue attributes and values read from a JIRA response.
        :param fields: issue fields in a response;
        :return: a dictionary of issue attributes and values
        """
        backend_issue = {
            'resolution': self._get_field_name(fields, 'resolution'),
            'status': self._get_field_name(fields, 'status'),
            'impact': self._get_impact_field(fields=fields),
            'summary': fields['summary'],
            'priority': self._get_field_name(fields, 'priority'),
            'description': fields['description'] or '',
            'type': fields['issuetype']['name'],
        }

        custom_field_values = [fields[customfield] for customfield in fields if customfield.startswith('customfield')]
        backend_issue['first_response_sla'] = self._get_sla_field_value(custom_field_values)

        assignee = self._get_support_user_by_field_name(field_name='assignee', fields=fields)
        if assignee:
            backend_issue['assignee'] = assignee

        reporter = self._get_support_user_by_field_name(field_name='reporter', fields=fields)
        if reporter:
            backend_issue['reporter'] = reporter
            if reporter.user:
                backend_issue['caller'] = reporter.user

        return backend_issue

    def _update_issue(self, issue, backend_issue):
        """
        Updates given issue from the backend issue if it has been changed.
        :param issue: an issue to update
        :param backend_issue: a set of parameters to be updated.
        """
        updated = False

        for field, backend_value in backend_issue.items():
            current_value = getattr(issue, field)
            if current_value != backend_value:
                setattr(issue, field, backend_value)
                updated = True

        if updated:
            issue.save()

    def _get_field_name(self, fields, field_name, default_value=''):
        """ Returns 'name' attribute of the field or default_value if the value is None """
        return default_value if not fields[field_name] else fields[field_name]['name']

    def _get_sla_field_value(self, custom_field_values):
        sla_field_name = settings.WALDUR_SUPPORT.get('ISSUE', {}).get('sla_field', None)

        for field in custom_field_values:
            if isinstance(field, dict):
                name = field.get('name', None)
                if name and name == sla_field_name:
                    ongoing_cycle = field.get('ongoingCycle', {})
                    breach_time = ongoing_cycle.get('breachTime', {})
                    epoch_milliseconds = breach_time.get('epochMillis', None)
                    if epoch_milliseconds:
                        return core_utils.timestamp_to_datetime(epoch_milliseconds / 1000.0)

        return None

    def _get_impact_field(self, fields):
        issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})
        impact_field_name = issue_settings.get('impact_field', None)
        return fields.get(impact_field_name, '')

    def _get_backend_comments(self, issue_key, fields):
        """
        Forms a dictionary of a backend comments where an id is a key and a comment body is a value.
        :param issue_key: an issue key to look up for comments;
        :param fields: fields from issue in the response;
        :return: a dictionary of a backend comments with their ids as keys.
        """
        active_backend = backend.get_active_backend()
        if self._is_service_desk():
            comments = active_backend.expand_comments(issue_key)
            backend_comments = {c['id']: c for c in comments}
        else:
            backend_comments = {c['id']: c for c in fields['comment']['comments']}

        return backend_comments

    def _is_service_desk(self):
        """
        :return: True if current JIRA instance is ServiceDesk, otherwise False.
        """
        return isinstance(backend.get_active_backend(), ServiceDeskBackend)

    @transaction.atomic()
    def _update_comments(self, issue, backend_comments):
        comments = {c.backend_id: c for c in issue.comments.all()}
        active_backend = backend.get_active_backend()

        for exist_comment_id in set(backend_comments) & set(comments):
            backend_comment = backend_comments[exist_comment_id]
            comment = comments[exist_comment_id]

            update_fields = []

            backend_comment_description = active_backend.extract_comment_message(backend_comment['body'])
            if comment.description != backend_comment_description:
                comment.description = backend_comment_description
                update_fields.append('description')

            if self._is_service_desk():
                is_public = self._get_comment_public_field_value(backend_comment)
                if is_public != comment.is_public:
                    comment.is_public = is_public
                    update_fields.append('is_public')

            if update_fields:
                comment.save(update_fields=update_fields)

        for new_comment_id in set(backend_comments) - set(comments):
            backend_comment = backend_comments[new_comment_id]
            author, _ = models.SupportUser.objects.get_or_create(backend_id=backend_comment['author']['key'])
            new_comment = models.Comment(
                issue=issue,
                author=author,
                description=backend_comment['body'],
                backend_id=backend_comment['id'],
            )

            if self._is_service_desk():
                new_comment.is_public = self._get_comment_public_field_value(backend_comment)

            new_comment.save()

        models.Comment.objects.filter(backend_id__in=set(comments) - set(backend_comments)).delete()

    def _get_comment_public_field_value(self, backend_comment):
        properties = backend_comment.get('properties', {})

        try:
            internal_property = next(p for p in properties if p.get('key') == self.PUBLIC_COMMENT_KEY)
        except StopIteration:
            return True

        is_internal = internal_property.get('value', {}).get('internal', False)

        return not is_internal

    def _get_support_user_by_field_name(self, fields, field_name):
        support_user = None

        if field_name in fields and fields[field_name]:
            support_user_backend_key = fields[field_name]['key']

            if support_user_backend_key:
                support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

        return support_user


class OfferingSerializer(structure_serializers.PermissionFieldFilteringMixin,
                         core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    type = serializers.ChoiceField(choices=settings.WALDUR_SUPPORT['OFFERINGS'].keys())
    state = serializers.ReadOnlyField(source='get_state_display')

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'name', 'project', 'type', 'state', 'type_label', 'unit_price',
                  'unit', 'created', 'modified', 'issue', 'issue_name', 'issue_link',
                  'issue_key', 'issue_description', 'issue_uuid', 'issue_status',
                  'project_name', 'project_uuid', 'product_code', 'article_code')
        read_only_fields = ('type_label', 'issue', 'unit_price', 'unit', 'state', 'product_code', 'article_code')
        protected_fields = ('project', 'type')
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

    def get_filtered_field_names(self):
        return ('project',)


class ConfigurableSerializerMixin(object):
    """
    Serializer is built on top WALDUR_SUPPORT['OFFERINGS'] configuration.

    Each configured field get's converted to serializer field according to field type in the configuration.

    Field types:
        'integer' - corresponds to 'serializers.IntegerField'
        'string' - is a default field type even if it is not defined explicitly in configuration.
                   Corresponds to 'serializers.CharField(max_length=255)'

    Default values:
        if 'default' key is present in option field configuration it is going to be used in serializer unless
        the value itself has been provided in the create request.
    """

    def _get_offerings_configuration(self):
        return copy.deepcopy(settings.WALDUR_SUPPORT['OFFERINGS'])

    def _get_configuration(self, type):
        return self._get_offerings_configuration().get(type)

    def get_fields(self):
        result = super(ConfigurableSerializerMixin, self).get_fields()
        if hasattr(self, 'initial_data') and not hasattr(self, '_errors'):
            type = self.initial_data['type']
            configuration = self._get_configuration(type)
            for attr_name in configuration['order']:
                attr_options = configuration['options'].get(attr_name, {})
                result[attr_name] = self._get_field_instance(attr_options)

        # choices have to be added dynamically so that unit tests can mock offering configuration.
        # otherwise it is always going to be a default set up.
        result['type'] = serializers.ChoiceField(allow_blank=False, choices=self._get_offerings_configuration().keys())

        return result

    def _validate_type(self, type):
        offering_configuration = self._get_configuration(type)
        if offering_configuration is None:
            raise serializers.ValidationError({
                'type': _('Type configuration could not be found.')
            })

    def validate_empty_values(self, data):
        if 'type' not in data or ('type' in data and data['type'] is None):
            raise serializers.ValidationError({
                'type': _('This field is required.')
            })
        else:
            self._validate_type(data['type'])

        return super(ConfigurableSerializerMixin, self).validate_empty_values(data)

    def _get_field_instance(self, attr_options):
        field_type = attr_options.get('type', '').lower()

        if field_type == 'string':
            field = serializers.CharField(max_length=255, write_only=True)
        elif field_type == 'integer':
            field = serializers.IntegerField(write_only=True)
        else:
            field = serializers.CharField(write_only=True)

        default_value = attr_options.get('default')
        if default_value:
            field.default = default_value

        field.required = attr_options.get('required', False)
        field.label = attr_options.get('label')
        field.help_text = attr_options.get('help_text')

        return field

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

    def _get_extra(self, configuration, validated_data):
        result = {
            key: validated_data[key]
            for key in configuration['order']
            if key in validated_data
        }
        if 'description' in validated_data:
            result['description'] = validated_data['description']
        return result


class OfferingCreateSerializer(ConfigurableSerializerMixin, OfferingSerializer):
    description = serializers.CharField(required=False, help_text=_('Description to add to the issue.'))

    class Meta(OfferingSerializer.Meta):
        fields = OfferingSerializer.Meta.fields + ('description',)
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
            'summary' - has a format of 'Request for "OFFERING[name][label]' or 'Request for "Support" if empty;
            'description' - combined list of all other fields provided with the request;
        """
        project = validated_data['project']
        type = validated_data['type']
        offering_configuration = self._get_configuration(type)
        type_label = offering_configuration.get('label', type)
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

        offering = models.Offering.objects.create(
            issue=issue,
            project=issue.project,
            name=validated_data.get('name'),
            type=type,
            product_code=offering_configuration.get('product_code', ''),
            article_code=offering_configuration.get('article_code', ''),
            unit_price=offering_configuration.get('price', 0),
            unit=offering_configuration.get('unit', UnitPriceMixin.Units.PER_MONTH),
        )

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
                  'mime_type', 'file_size', 'thumbnail')
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
