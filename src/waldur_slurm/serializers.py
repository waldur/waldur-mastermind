from django.core.validators import MinValueValidator
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers as rf_serializers
from rest_framework import exceptions as rf_exceptions

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.permissions import _has_owner_access
from waldur_freeipa import models as freeipa_models

from . import models


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        core_serializers.RequiredFieldsMixin,
                        structure_serializers.BaseServiceSerializer):
    SERVICE_ACCOUNT_FIELDS = {
        'username': '',
    }
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'hostname': _('Hostname or IP address of master node'),
        'port': '',
        'use_sudo': _('Set to true to activate privilege escalation'),
        'gateway': _('Hostname or IP address of gateway node'),
        'default_account': _('Default SLURM account for user'),
        'batch_service': _('Batch service, SLURM or MOAB')
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.SlurmService
        required_fields = ('hostname', 'username', 'batch_service')
        extra_field_options = {
            'username': {
                'default_value': 'root',
            },
            'use_sudo': {
                'default_value': False,
            },
            'default_account': {
                'required': True,
            },
        }


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.SlurmServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'slurm-detail'},
        }


class AllocationSerializer(structure_serializers.BaseResourceSerializer,
                           core_serializers.AugmentedSerializerMixin):
    service = rf_serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='slurm-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = rf_serializers.HyperlinkedRelatedField(
        view_name='slurm-spl-detail',
        queryset=models.SlurmServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    username = rf_serializers.SerializerMethodField()
    gateway = rf_serializers.SerializerMethodField()
    backend_id = rf_serializers.SerializerMethodField()
    batch_service = rf_serializers.ReadOnlyField()
    homepage = rf_serializers.ReadOnlyField(
        source='service_project_link.service.settings.homepage')

    def get_username(self, allocation):
        request = self.context['request']
        try:
            profile = freeipa_models.Profile.objects.get(user=request.user)
            return profile.username
        except freeipa_models.Profile.DoesNotExist:
            return None

    def get_gateway(self, allocation):
        options = allocation.service_project_link.service.settings.options
        return options.get('gateway') or options.get('hostname')

    def get_backend_id(self, allocation):
        return allocation.get_backend().get_allocation_name(allocation)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Allocation
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'cpu_limit', 'cpu_usage',
            'gpu_limit', 'gpu_usage',
            'ram_limit', 'ram_usage',
            'deposit_limit', 'deposit_usage',
            'username', 'gateway',
            'is_active', 'batch_service', 'homepage'
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'cpu_usage', 'gpu_usage', 'ram_usage', 'is_active',
            'deposit_limit', 'deposit_usage',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'slurm-allocation-detail'},
            cpu_limit={'validators': [MinValueValidator(0)]},
            gpu_limit={'validators': [MinValueValidator(0)]},
            ram_limit={'validators': [MinValueValidator(0)]},
        )

    def validate(self, attrs):
        attrs = super(AllocationSerializer, self).validate(attrs)
        # Skip validation on update
        if self.instance:
            return attrs

        spl = attrs['service_project_link']
        user = self.context['request'].user
        if not _has_owner_access(user, spl.project.customer):
            raise rf_exceptions.PermissionDenied(
                _('You do not have permissions to create allocation for given project.')
            )
        return attrs


class AllocationUsageSerializer(rf_serializers.HyperlinkedModelSerializer):
    full_name = rf_serializers.ReadOnlyField(source='user.full_name')

    class Meta(object):
        model = models.AllocationUsage
        fields = ('allocation', 'year', 'month',
                  'username', 'user', 'full_name',
                  'cpu_usage', 'ram_usage', 'gpu_usage', 'deposit_usage')
        extra_kwargs = {
            'allocation': {
                'lookup_field': 'uuid',
                'view_name': 'slurm-allocation-detail',
            },
            'user': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            }
        }
