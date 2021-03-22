from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core.validators import BackendURLValidator, validate_x509_certificate
from waldur_core.structure import serializers as structure_serializers

from . import models


class BaseOpenStackServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ('backend_url', 'username', 'password', 'domain', 'certificate')

    certificate = serializers.CharField(
        required=False, validators=[validate_x509_certificate]
    )

    backend_url = serializers.CharField(
        max_length=200,
        label=_('API URL'),
        default='http://keystone.example.com:5000/v3',
        help_text=_('Keystone auth URL (e.g. http://keystone.example.com:5000/v3)'),
        validators=[BackendURLValidator],
    )

    username = serializers.CharField(
        max_length=100, help_text=_('Administrative user'), default='admin'
    )

    password = serializers.CharField(max_length=100)

    domain = serializers.CharField(
        max_length=200,
        help_text=_('Domain name. If not defined default domain will be used.'),
        required=False,
    )

    availability_zone = serializers.CharField(
        source='options.availability_zone',
        help_text=_('Default availability zone for provisioned instances'),
        required=False,
    )

    flavor_exclude_regex = serializers.CharField(
        source='options.flavor_exclude_regex',
        help_text=_(
            'Flavors matching this regex expression will not be pulled from the backend.'
        ),
        required=False,
    )

    console_type = serializers.CharField(
        source='options.console_type',
        help_text=_(
            'The type of remote console. '
            'The valid values are novnc, xvpvnc, rdp-html5, '
            'spice-html5, serial, and webmks.'
        ),
        default='novnc',
        required=False,
    )

    config_drive = serializers.CharField(
        source='options.config_drive',
        help_text=_('Indicates whether a config drive enables metadata injection'),
        required=False,
    )


class BaseVolumeTypeSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.BaseVolumeType
        fields = ('url', 'uuid', 'name', 'description', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {
                'lookup_field': 'uuid',
                'view_name': 'servicesettings-detail',
            },
        }


class BaseSecurityGroupRuleSerializer(serializers.ModelSerializer):
    remote_group_name = serializers.ReadOnlyField(source='remote_group.name')
    remote_group_uuid = serializers.ReadOnlyField(source='remote_group.uuid')

    class Meta:
        model = models.BaseSecurityGroupRule
        fields = (
            'ethertype',
            'direction',
            'protocol',
            'from_port',
            'to_port',
            'cidr',
            'description',
            'remote_group_name',
            'remote_group_uuid',
        )
