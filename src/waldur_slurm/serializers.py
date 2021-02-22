import re

from django.core.validators import MinValueValidator
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers as rf_serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.permissions import _has_owner_access
from waldur_freeipa import models as freeipa_models

from . import models


class ServiceSerializer(
    core_serializers.ExtraFieldOptionsMixin,
    core_serializers.RequiredFieldsMixin,
    structure_serializers.BaseServiceSerializer,
):
    SERVICE_ACCOUNT_FIELDS = {
        'username': '',
    }
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'hostname': _('Hostname or IP address of master node'),
        'port': '',
        'use_sudo': _('Set to true to activate privilege escalation'),
        'gateway': _('Hostname or IP address of gateway node'),
        'default_account': _('Default SLURM account for user'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.SlurmService
        required_fields = ('hostname', 'username')
        extra_field_options = {
            'username': {'default_value': 'root',},
            'use_sudo': {'default_value': False,},
            'default_account': {'required': True,},
        }


class ServiceProjectLinkSerializer(
    structure_serializers.BaseServiceProjectLinkSerializer
):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.SlurmServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'slurm-detail'},
        }


class AllocationSerializer(
    structure_serializers.BaseResourceSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    service = rf_serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='slurm-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = rf_serializers.HyperlinkedRelatedField(
        view_name='slurm-spl-detail',
        queryset=models.SlurmServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    username = rf_serializers.SerializerMethodField()
    gateway = rf_serializers.SerializerMethodField()
    homepage = rf_serializers.ReadOnlyField(
        source='service_project_link.service.settings.homepage'
    )

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

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Allocation
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'cpu_limit',
            'cpu_usage',
            'gpu_limit',
            'gpu_usage',
            'ram_limit',
            'ram_usage',
            'username',
            'gateway',
            'is_active',
            'homepage',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                'cpu_usage',
                'gpu_usage',
                'ram_usage',
                'cpu_limit',
                'gpu_limit',
                'ram_limit',
                'is_active',
            )
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

        correct_name_regex = '^([%s]{1,63})$' % models.SLURM_ALLOCATION_REGEX
        name = attrs.get('name')
        if not re.match(correct_name_regex, name):
            raise core_serializers.ValidationError(
                _(
                    "Name '%s' must be 1-63 characters long, each of "
                    "which can only be alphanumeric or a hyphen"
                )
                % name
            )

        spl = attrs['service_project_link']
        user = self.context['request'].user
        if not _has_owner_access(user, spl.project.customer):
            raise rf_exceptions.PermissionDenied(
                _('You do not have permissions to create allocation for given project.')
            )
        return attrs


class AllocationUserUsageSerializer(rf_serializers.HyperlinkedModelSerializer):
    full_name = rf_serializers.ReadOnlyField(source='user.full_name')

    class Meta:
        model = models.AllocationUserUsage
        fields = (
            'cpu_usage',
            'ram_usage',
            'gpu_usage',
            'month',
            'year',
            'allocation',
            'user',
            'username',
            'full_name',
        )
        extra_kwargs = {
            'allocation': {
                'lookup_field': 'uuid',
                'view_name': 'slurm-allocation-detail',
            },
            'user': {'lookup_field': 'uuid', 'view_name': 'user-detail',},
        }


class AssociationSerializer(rf_serializers.HyperlinkedModelSerializer):
    allocation = rf_serializers.HyperlinkedRelatedField(
        queryset=models.Allocation.objects.all(),
        view_name='slurm-allocation-detail',
        lookup_field='uuid',
    )

    class Meta:
        model = models.Association
        fields = (
            'uuid',
            'username',
            'allocation',
        )
