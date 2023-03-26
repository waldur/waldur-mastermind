import re

from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers as rf_serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.permissions import _has_admin_access
from waldur_freeipa import models as freeipa_models

from . import models


class SlurmServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ('hostname', 'username', 'port', 'gateway')

    username = rf_serializers.CharField(
        max_length=100, help_text=_('Administrative user'), default='root'
    )

    hostname = rf_serializers.CharField(
        source='options.hostname', label=_('Hostname or IP address of master node')
    )

    default_account = rf_serializers.CharField(
        source='options.default_account', label=_('Default SLURM account for user')
    )

    port = rf_serializers.IntegerField(source='options.port', required=False)

    use_sudo = rf_serializers.BooleanField(
        source='options.use_sudo',
        default=False,
        help_text=_('Set to true to activate privilege escalation'),
        required=False,
    )

    gateway = rf_serializers.CharField(
        source='options.gateway',
        label=_('Hostname or IP address of gateway node'),
        required=False,
    )

    firecrest_api_url = rf_serializers.CharField(
        source='options.firecrest_api_url',
        label=_('FirecREST API base URL'),
        required=False,
    )


class AllocationSerializer(
    structure_serializers.BaseResourceSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    username = rf_serializers.SerializerMethodField()
    gateway = rf_serializers.SerializerMethodField()
    homepage = rf_serializers.ReadOnlyField(source='service_settings.homepage')

    def get_username(self, allocation):
        request = self.context['request']
        try:
            profile = freeipa_models.Profile.objects.get(user=request.user)
            return profile.username
        except freeipa_models.Profile.DoesNotExist:
            return None

    def get_gateway(self, allocation):
        options = allocation.service_settings.options
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
        attrs = super().validate(attrs)
        # Skip validation on update
        if self.instance:
            return attrs

        correct_name_regex = '^([%s]{1,63})$' % models.SLURM_ALLOCATION_REGEX
        name = attrs.get('name')
        if not re.match(correct_name_regex, name):
            raise rf_serializers.ValidationError(
                _(
                    "Name '%s' must be 1-63 characters long, each of "
                    "which can only be alphanumeric or a hyphen"
                )
                % name
            )

        project = attrs['project']
        user = self.context['request'].user
        if not _has_admin_access(user, project):
            raise rf_exceptions.PermissionDenied(
                _('You do not have permissions to create allocation for given project.')
            )
        return attrs


class AllocationSetLimitsSerializer(rf_serializers.ModelSerializer):
    cpu_limit = rf_serializers.IntegerField(min_value=-1)
    gpu_limit = rf_serializers.IntegerField(min_value=-1)
    ram_limit = rf_serializers.IntegerField(min_value=-1)

    class Meta:
        model = models.Allocation
        fields = ('cpu_limit', 'gpu_limit', 'ram_limit')


class AllocationUserUsageCreateSerializer(rf_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.AllocationUserUsage
        fields = (
            'cpu_usage',
            'ram_usage',
            'gpu_usage',
            'month',
            'year',
            'user',
            'username',
        )
        extra_kwargs = {
            'user': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
        }


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
            'user': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
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
