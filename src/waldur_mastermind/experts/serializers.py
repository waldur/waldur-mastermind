from __future__ import unicode_literals

import copy
import itertools

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions, serializers
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

from . import models


class ExpertProviderSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):
    agree_with_policy = serializers.BooleanField(write_only=True, required=False)

    class Meta(object):
        model = models.ExpertProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name',
                  'agree_with_policy', 'enable_notifications')
        read_only_fields = ('url', 'uuid', 'created')
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation')
        }
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'expert-provider-detail'},
            'customer': {'lookup_field': 'uuid'},
        }

    def validate(self, attrs):
        # We do not need to check ToS acceptance if provider is already created.
        if self.instance:
            structure_permissions.is_owner(self.context['request'], None, self.instance.customer)
            return attrs

        agree_with_policy = attrs.pop('agree_with_policy', False)
        if not agree_with_policy:
            raise serializers.ValidationError(
                {'agree_with_policy': _('User must agree with policies to register organization.')})

        structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs


class ExpertContractSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):
    file = serializers.SerializerMethodField()
    filename = serializers.SerializerMethodField()

    def get_file(self, obj):
        if not obj.has_file():
            return None

        request = self.context['request']
        return reverse('expert-request-pdf', kwargs={'uuid': obj.request.uuid}, request=request)

    def get_filename(self, obj):
        if not obj.has_file():
            return None

        return obj.get_filename()

    class Meta(object):
        model = models.ExpertContract
        fields = (
            'price', 'description', 'team', 'team_uuid', 'team_name', 'file', 'filename'
        )
        related_paths = {
            'team': ('uuid', 'name'),
        }
        extra_kwargs = {
            'team': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }


class ExpertRequestSerializer(support_serializers.ConfigurableSerializerMixin,
                              core_serializers.AugmentedSerializerMixin,
                              serializers.HyperlinkedModelSerializer):
    type = serializers.ChoiceField(choices=settings.WALDUR_EXPERTS['CONTRACT']['offerings'].keys())
    state = serializers.ReadOnlyField(source='get_state_display')
    contract = ExpertContractSerializer(required=False, read_only=True)
    customer = serializers.HyperlinkedRelatedField(
        source='project.customer',
        view_name='customer-detail',
        read_only=True,
        lookup_field='uuid'
    )
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    type_label = serializers.SerializerMethodField()
    extra = serializers.JSONField(read_only=True)

    def get_type_label(self, instance):
        return self._get_configuration(instance.type).get('label', instance.type)

    def _get_offerings_configuration(self):
        return copy.deepcopy(settings.WALDUR_EXPERTS['CONTRACT']['offerings'])

    def _get_configuration(self, type):
        contract_config = copy.deepcopy(settings.WALDUR_EXPERTS['CONTRACT'])
        offering_config = contract_config['offerings'].get(type)
        configured_options = [tab.get('options', {}) for tab in contract_config.get('options').values()]
        options_order = [tab.get('order', []) for tab in contract_config.get('options').values()]
        offering_config['order'] = offering_config['order'] + list(itertools.chain(*options_order))
        [offering_config['options'].update(options) for options in configured_options]

        return offering_config

    class Meta(object):
        model = models.ExpertRequest
        fields = ('url', 'uuid', 'name', 'type', 'state', 'type_label', 'extra',
                  'customer', 'customer_name', 'customer_uuid',
                  'project', 'project_name', 'project_uuid',
                  'created', 'modified', 'contract', 'recurring_billing',
                  'objectives', 'milestones', 'contract_methodology', 'out_of_scope', 'common_tos',
                  'issue', 'issue_name', 'issue_link', 'issue_key', 'issue_description', 'issue_uuid', 'issue_status',)
        read_only_fields = ('price', 'state', 'issue', 'recurring_billing', 'extra')
        protected_fields = ('project', 'type', 'common_tos')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'expert-request-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
            'issue': {'lookup_field': 'uuid', 'view_name': 'support-issue-detail'},
        }
        related_paths = {
            'project': ('uuid', 'name'),
            'issue': ('uuid', 'name', 'status', 'key', 'description', 'link'),
        }

    def validate_project(self, project):
        request = self.context['request']
        structure_permissions.is_owner(request, None, project.customer)
        if models.ExpertRequest.objects.filter(
            state=models.ExpertRequest.States.ACTIVE,
            project=project
        ).exists():
            raise serializers.ValidationError(_('Active expert request for current project already exists.'))
        return project

    def create(self, validated_data):
        request = self.context['request']
        project = validated_data['project']
        type = validated_data['type']

        configuration = self._get_configuration(type)
        type_label = configuration.get('label', type)
        issue_details = dict(
            caller=request.user,
            project=project,
            customer=project.customer,
            type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
            description=self._form_description(configuration, validated_data),
            summary='Request for \'%s\'' % type_label)
        issue_details['summary'] = support_serializers.render_issue_template('summary', issue_details)
        issue_details['description'] = support_serializers.render_issue_template('description', issue_details)
        issue = support_models.Issue.objects.create(**issue_details)

        return models.ExpertRequest.objects.create(
            user=request.user,
            project=project,
            name=validated_data.get('name'),
            type=type,
            recurring_billing=configuration.get('recurring_billing', False),
            description=issue_details['description'],
            extra=self._get_extra(configuration, validated_data),
            issue=issue,
            objectives=validated_data.get('objectives', ''),
            milestones=validated_data.get('milestones', ''),
            contract_methodology=validated_data.get('contract_methodology', ''),
            out_of_scope=validated_data.get('out_of_scope', ''),
            common_tos=validated_data.get('common_tos', ''),
        )


class ExpertBidSerializer(core_serializers.AugmentedSerializerMixin,
                          serializers.HyperlinkedModelSerializer):

    team_members = serializers.SerializerMethodField(source='get_team_members')
    customer_uuid = serializers.ReadOnlyField(source='team.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='team.customer.name')
    customer_email = serializers.ReadOnlyField(source='team.customer.email')

    class Meta(object):
        model = models.ExpertBid
        fields = (
            'url', 'uuid', 'created', 'modified', 'price', 'description',
            'team', 'team_uuid', 'team_name', 'team_members',
            'request', 'request_uuid', 'request_name',
            'customer_uuid', 'customer_name', 'customer_email',
        )
        related_paths = {
            'team': ('uuid', 'name'),
            'request': ('uuid', 'name'),
        }
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'expert-bid-detail'},
            'request': {'lookup_field': 'uuid', 'view_name': 'expert-request-detail'},
            'team': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }

    def validate_request(self, request):
        if request.state != models.ExpertRequest.States.PENDING:
            raise exceptions.ValidationError(_('Expert request should be in pending state.'))
        return request

    def validate_team(self, team):
        if not team.permissions.filter(is_active=True).exists():
            raise exceptions.ValidationError(_('Expert team should have at least one member.'))

        return team

    def validate(self, attrs):
        team = attrs['team']
        request = attrs['request']

        if models.ExpertBid.objects.filter(request=request, team__customer=team.customer).exists():
            raise exceptions.ValidationError({'team': _('There is a bid from this customer already.')})

        return attrs

    def create(self, validated_data):
        request = self.context['request']
        validated_data['user'] = request.user
        return super(ExpertBidSerializer, self).create(validated_data)

    def get_team_members(self, request):
        user_ids = request.team.permissions.filter(is_active=True).values_list('user_id')
        users = core_models.User.objects.filter(pk__in=user_ids)
        return users.values('username', 'email', 'full_name', 'uuid')


def get_is_expert_provider(serializer, scope):
    customer = structure_permissions._get_customer(scope)
    return models.ExpertProvider.objects.filter(customer=customer).exists()


def add_expert_provider(sender, fields, **kwargs):
    fields['is_expert_provider'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_expert_provider', get_is_expert_provider)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_expert_provider,
)
core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerPermissionSerializer,
    receiver=add_expert_provider,
)
