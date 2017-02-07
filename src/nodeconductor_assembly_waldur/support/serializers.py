from __future__ import unicode_literals

from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.template import Context, Template
from rest_framework import serializers

from nodeconductor.core import serializers as core_serializers
from nodeconductor.structure import models as structure_models, SupportedServices, serializers as structure_serializers

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
        choices=[(t, t) for t in settings.WALDUR_SUPPORT['ISSUE']['types']],
        initial=settings.WALDUR_SUPPORT['ISSUE']['types'][0],
        default=settings.WALDUR_SUPPORT['ISSUE']['types'][0])
    is_reported_manually = serializers.BooleanField(
        initial=False, default=False, write_only=True,
        help_text='Set true if issue is created by regular user via portal.')
    issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})

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
        """ User has to be customer owner, staff or global support """
        if not customer:
            return customer
        user = self.context['request'].user
        if (not customer or
                user.is_staff or
                user.is_support or
                customer.has_user(user, structure_models.CustomerRole.OWNER)):
            return customer
        raise serializers.ValidationError('Only customer owner, staff or support can report customer issues.')

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
            'Only customer owner, project manager, project admin, staff or support can report such issue.')

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

        validated_data['description'] = self._render_template('description', validated_data)
        validated_data['summary'] = self._render_template('summary', validated_data)
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

    class Meta(object):
        model = models.Comment
        fields = ('url', 'uuid', 'issue', 'issue_key', 'description', 'is_public',
                  'author_name', 'author_user', 'backend_id', 'created')
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


class WebHookReceiverSerializer(serializers.Serializer):
    class EventType:
        CREATED = 'jira:issue_created'
        UPDATED = 'jira:issue_updated'
        DELETED = 'jira:issue_deleted'

    def validate(self, attrs):
        if 'issue' not in self.initial_data:
            raise serializers.ValidationError('"issue" is missing in request data. Cannot process issue.')

        if 'webhookEvent' not in self.initial_data:
            raise serializers.ValidationError('"webhookEvent" is missing in request data. Cannot find out even type')

        return attrs

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
            if reporter.user:
                issue.caller = reporter.user

        if 'comment' in fields:
            self._update_comments(issue=issue, fields=fields)

        issue.save()
        return issue

    def _update_custom_fields(self, issue, custom_field_values):
        sla_field_name = settings.WALDUR_SUPPORT.get('ISSUE', {}).get('sla_field', None)

        for field in custom_field_values:
            if isinstance(field, dict):
                name = field.get('name', None)
                if name and name == sla_field_name:
                    ongoing_cycle = field.get('ongoingCycle', {})
                    breach_time = ongoing_cycle.get('breachTime', {})
                    epoch_milliseconds = breach_time.get('epochMillis', None)
                    if epoch_milliseconds:
                        issue.first_response_sla = datetime.fromtimestamp(epoch_milliseconds / 1000.0)

    def _get_impact_field(self, fields):
        issue_settings = settings.WALDUR_SUPPORT.get('ISSUE', {})
        impact_field_name = issue_settings.get('impact_field', None)
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

        if field_name in fields and fields[field_name]:
            support_user_backend_key = fields[field_name]['key']

            if support_user_backend_key:
                support_user, _ = models.SupportUser.objects.get_or_create(backend_id=support_user_backend_key)

        return support_user


class OfferingSerializer(structure_serializers.PermissionFieldFilteringMixin,
                         core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    type = serializers.ChoiceField(choices=settings.WALDUR_SUPPORT['OFFERINGS'].keys())

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'name', 'project', 'type', 'type_label', 'price', 'created', 'modified',
                  'issue', 'issue_name', 'issue_link', 'issue_key', 'issue_description', 'issue_uuid', 'issue_status',
                  'project_name', 'project_uuid')
        read_only_fields = ('type_label', 'issue', 'price', 'state')
        protected_fields = ('project', 'type')
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'support-offering-detail'},
            issue={'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
            project={'lookup_field': 'uuid', 'view_name': 'project-detail'},
        )
        related_paths = dict(
            issue=('uuid', 'name', 'status', 'key', 'description', 'link'),
            project=('uuid', 'name',),
        )

    def get_filtered_field_names(self):
        return 'project',


class OfferingCreateSerializer(OfferingSerializer):
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

    Each offering corresponds to the single issue which has next values:
        'project' - a hyperlinked field which must be provided with every request;
        'customer' - customer is extracted from the provided project;
        'caller' - a user who sent a request is considered to be a 'caller' of the issue;
        'summary' - has a format of 'Request for "OFFERING[name][label]' or 'Request for "Support" if empty;
        'description' - combined list of all other fields provided with the request;
    """
    type = serializers.ChoiceField(choices=settings.WALDUR_SUPPORT['OFFERINGS'].keys(), allow_blank=False)
    description = serializers.CharField(required=False, help_text='Description to add to the issue.')

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

    def _get_offering_configuration(self, type):
        return settings.WALDUR_SUPPORT['OFFERINGS'].get(type)

    def get_fields(self):
        result = super(OfferingSerializer, self).get_fields()
        if hasattr(self, 'initial_data') and not hasattr(self, '_errors'):
            type = self.initial_data['type']
            configuration = self._get_offering_configuration(type)
            for attr_name in configuration['order']:
                attr_options = configuration['options'].get(attr_name, {})
                result[attr_name] = self._get_field_instance(attr_options)

        return result

    def _validate_type(self, type):
        type = self._get_offering_configuration(type)
        if type is None:
            raise serializers.ValidationError({'type': 'Type configuration could not be found.'})

    def validate_empty_values(self, data):
        if 'type' not in data or ('type' in data and data['type'] is None):
            raise serializers.ValidationError({'type': 'This field is required.'})
        else:
            self._validate_type(data['type'])

        return super(OfferingSerializer, self).validate_empty_values(data)

    def _get_field_instance(self, attr_options):
        filed_type = attr_options.get('type')
        if filed_type is None or filed_type.lower() == 'string':
            field = serializers.CharField(max_length=255, write_only=True)
        elif filed_type.lower() == 'integer':
            field = serializers.IntegerField(write_only=True)
        else:
            raise NotImplementedError('Type "%s" can not be serialized.' % type)
        default_value = attr_options.get('default')
        if default_value:
            field.default = default_value
            field.required = False

        field.label = attr_options.get('label')
        field.help_text = attr_options.get('help_text')

        return field

    def create(self, validated_data):
        self.project = validated_data.pop('project')
        self.type = validated_data.pop('type')
        type_label = self._get_offering_configuration(self.type).get('label', self.type)
        issue = models.Issue.objects.create(
            caller=self.context['request'].user,
            project=self.project,
            customer=self.project.customer,
            type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
            summary='Request for \'%s\'' % type_label,
            description=self._form_description(validated_data, validated_data.pop('description', None))
        )

        offering = models.Offering.objects.create(
            issue=issue,
            project=issue.project,
            name=validated_data.get('name'),
            type=self.type)

        return offering

    def _form_description(self, validated_data, appendix):
        result = []
        for key in validated_data:
            label = self._get_offering_configuration(self.type)['options'].get(key, {})
            label_value = label.get('label', key)
            result.append('%s: \'%s\'' % (label_value, validated_data[key]))

        if appendix:
            result.append('\n %s' % appendix)

        return '\n'.join(result)


class OfferingCompleteSerializer(serializers.Serializer):
    price = serializers.DecimalField(max_digits=13, decimal_places=7)

    def update(self, instance, validated_data):
        instance.price = validated_data['price']
        instance.state = models.Offering.States.OK
        instance.save(update_fields=['state', 'price'])
        return instance
