from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions, serializers

from nodeconductor.core import models as core_models
from nodeconductor.core import serializers as core_serializers
from nodeconductor.core import signals as core_signals
from nodeconductor.structure import permissions as structure_permissions
from nodeconductor.structure import serializers as structure_serializers
from nodeconductor_assembly_waldur.support import models as support_models
from nodeconductor_assembly_waldur.support import serializers as support_serializers

from . import models


class ExpertProviderSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):
    agree_with_policy = serializers.BooleanField(write_only=True, required=False)

    class Meta(object):
        model = models.ExpertProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name', 'agree_with_policy')
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
        agree_with_policy = attrs.pop('agree_with_policy', False)
        if not agree_with_policy:
            raise serializers.ValidationError(
                {'agree_with_policy': _('User must agree with policies to register organization.')})

        structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs


class ExpertContractSerializer(core_serializers.AugmentedSerializerMixin,
                               serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ExpertContract
        fields = (
            'price', 'description', 'team', 'team_uuid', 'team_name',
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
    type = serializers.ChoiceField(choices=settings.WALDUR_SUPPORT['OFFERINGS'].keys())
    state = serializers.ReadOnlyField(source='get_state_display')
    description = serializers.CharField(required=False)
    contract = ExpertContractSerializer(required=False, read_only=True)
    customer = serializers.HyperlinkedRelatedField(
        source='project.customer',
        view_name='customer-detail',
        read_only=True,
        lookup_field='uuid'
    )
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')

    class Meta(object):
        model = models.ExpertRequest
        fields = ('url', 'uuid', 'name', 'type', 'state', 'type_label', 'description',
                  'customer', 'customer_name', 'customer_uuid',
                  'project', 'project_name', 'project_uuid',
                  'created', 'modified', 'contract',
                  'issue', 'issue_name', 'issue_link', 'issue_key', 'issue_description', 'issue_uuid', 'issue_status',)
        read_only_fields = ('type_label', 'price', 'state', 'issue')
        protected_fields = ('project', 'type')
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
            description=issue_details['description'],
            issue=issue,
        )


class ExpertBidSerializer(core_serializers.AugmentedSerializerMixin,
                          serializers.HyperlinkedModelSerializer):

    team_members = serializers.SerializerMethodField(source='get_team_members')

    class Meta(object):
        model = models.ExpertBid
        fields = (
            'url', 'uuid', 'created', 'modified', 'price', 'description',
            'team', 'team_uuid', 'team_name', 'team_members',
            'request', 'request_uuid', 'request_name',
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
