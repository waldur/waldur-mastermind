from __future__ import unicode_literals

import collections
import logging

from django.core.exceptions import ObjectDoesNotExist
import pytz
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ugettext, ugettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import (serializers as core_serializers,
                              utils as core_utils,
                              signals as core_signals)
from waldur_core.quotas import serializers as quotas_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack import serializers as openstack_serializers
from waldur_openstack.openstack_base.backend import OpenStackBackendError

from . import models, fields

logger = logging.getLogger(__name__)


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        core_serializers.RequiredFieldsMixin,
                        structure_serializers.BaseServiceSerializer):
    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Keystone auth URL (e.g. http://keystone.example.com:5000/v3)'),
        'domain': _('Tenant domain'),
        'username': _('Tenant user username'),
        'password': _('Tenant user password'),
    }
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'tenant_id': _('Tenant ID in OpenStack'),
        'availability_zone': _('Default availability zone for provisioned instances'),
        'flavor_exclude_regex': _('Flavors matching this regex expression will not be pulled from the backend.'),
        'external_network_id': _('It is used to automatically assign floating IP to your virtual machine.'),
        'console_type': _('The type of remote console. '
                          'The valid values are novnc, xvpvnc, rdp-html5, '
                          'spice-html5, serial, and webmks.'),
    }

    # Expose service settings quotas as service quotas as a temporary workaround.
    # It is needed in order to render quotas table in service provider details dialog.
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True, source='settings.quotas')

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.OpenStackTenantService
        required_fields = ('backend_url', 'username', 'password', 'tenant_id',)
        extra_field_options = {
            'backend_url': {
                'label': 'API URL',
                'default_value': 'http://keystone.example.com:5000/v3',
            },
            'tenant_id': {
                'label': 'Tenant ID',
            },
            'availability_zone': {
                'placeholder': 'default',
            },
            'external_network_id': {
                'required': True,
            },
            'console_type': {
                'default_value': 'novnc',
            },
        }


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.OpenStackTenantServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-detail'},
        }


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Image
        fields = ('url', 'uuid', 'name', 'settings', 'min_disk', 'min_ram',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }


class FlavorSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Flavor
        fields = ('url', 'uuid', 'name', 'settings', 'cores', 'ram', 'disk',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }


class UsageStatsSerializer(serializers.Serializer):
    shared = serializers.BooleanField()
    service_provider = serializers.ListField(child=serializers.CharField())


class NetworkSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Network
        fields = ('url', 'uuid', 'name',
                  'type', 'is_external', 'segmentation_id', 'subnets')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
            'subnets': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-subnet-detail'}
        }


class SubNetSerializer(structure_serializers.BasePropertySerializer):
    dns_nameservers = serializers.JSONField(read_only=True)
    allocation_pools = serializers.JSONField(read_only=True)

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.SubNet
        fields = ('url', 'uuid', 'name',
                  'cidr', 'gateway_ip', 'allocation_pools', 'ip_version', 'enable_dhcp', 'dns_nameservers', 'network')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
            'network': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-network-detail'},
        }


class FloatingIPSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.FloatingIP
        fields = ('url', 'uuid', 'settings', 'address', 'runtime_state', 'is_booked',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }


class SecurityGroupSerializer(structure_serializers.BasePropertySerializer):
    rules = serializers.SerializerMethodField()

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.SecurityGroup
        fields = ('url', 'uuid', 'name', 'settings', 'description', 'rules')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }

    def get_rules(self, security_group):
        rules = []
        for rule in security_group.rules.all():
            rules.append({
                'protocol': rule.protocol,
                'from_port': rule.from_port,
                'to_port': rule.to_port,
                'cidr': rule.cidr,
            })
        return rules


class VolumeImportableSerializer(core_serializers.AugmentedSerializerMixin,
                                 serializers.HyperlinkedModelSerializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        queryset=models.OpenStackTenantServiceProjectLink.objects.all(),
        write_only=True)

    instance_name = serializers.ReadOnlyField(source='instance.name')
    instance_uuid = serializers.ReadOnlyField(source='instance.uuid')

    def get_filtered_field_names(self):
        return 'service_project_link',

    class Meta(object):
        model = models.Volume
        model_fields = ('name', 'description', 'size', 'bootable', 'type', 'device',
                        'runtime_state', 'instance_name', 'instance_uuid')
        fields = ('service_project_link', 'backend_id') + model_fields
        read_only_fields = model_fields + ('backend_id',)


class VolumeImportSerializer(VolumeImportableSerializer):
    class Meta(VolumeImportableSerializer.Meta):
        fields = VolumeImportableSerializer.Meta.fields + ('url', 'uuid', 'created')
        read_only_fields = VolumeImportableSerializer.Meta.model_fields
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Volume.objects.filter(
            service_project_link__service__settings=service_project_link.service.settings,
            backend_id=backend_id
        ).exists():
            raise serializers.ValidationError({
                'backend_id': _('Volume has been imported already.')
            })

        try:
            backend = service_project_link.get_backend()
            volume = backend.import_volume(backend_id, save=True, service_project_link=service_project_link)
        except OpenStackBackendError:
            raise serializers.ValidationError({
                'backend_id': _("Can't import volume with ID %s") % validated_data['backend_id']
            })

        return volume


class VolumeSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstacktenant-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        queryset=models.OpenStackTenantServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.SerializerMethodField()
    type_name = serializers.CharField(source='type.name', read_only=True)
    availability_zone_name = serializers.CharField(source='availability_zone.name', read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Volume
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'source_snapshot', 'size', 'bootable', 'metadata',
            'image', 'image_metadata', 'image_name', 'type', 'type_name', 'runtime_state',
            'availability_zone', 'availability_zone_name',
            'device', 'action', 'action_details', 'instance', 'instance_name',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'image_metadata', 'image_name', 'bootable', 'source_snapshot', 'runtime_state', 'device', 'metadata',
            'action', 'instance'
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'size', 'image', 'type',
        )
        extra_kwargs = dict(
            instance={'lookup_field': 'uuid', 'view_name': 'openstacktenant-instance-detail'},
            image={'lookup_field': 'uuid', 'view_name': 'openstacktenant-image-detail'},
            source_snapshot={'lookup_field': 'uuid', 'view_name': 'openstacktenant-snapshot-detail'},
            type={'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-type-detail'},
            availability_zone={'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-availability-zone-detail'},
            size={'required': False, 'allow_null': True},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def get_instance_name(self, volume):
        if volume.instance:
            return volume.instance.name

    def validate(self, attrs):
        attrs = super(VolumeSerializer, self).validate(attrs)

        if self.instance is None:
            # image validation
            image = attrs.get('image')
            spl = attrs['service_project_link']
            if image and image.settings != spl.service.settings:
                raise serializers.ValidationError({'image': _('Image must belong to the same service settings')})
            # snapshot & size validation
            size = attrs.get('size')
            snapshot = attrs.get('snapshot')
            if not size and not snapshot:
                raise serializers.ValidationError(_('Snapshot or size should be defined'))
            if size and snapshot:
                raise serializers.ValidationError(_('It is impossible to define both snapshot and size'))
            # image & size validation
            size = size or snapshot.size
            if image and image.min_disk > size:
                raise serializers.ValidationError({
                    'size': _('Volume size should be equal or greater than %s for selected image') % image.min_disk
                })
            # type validation
            type = attrs.get('type')
            if type and type.settings != spl.service.settings:
                raise serializers.ValidationError({'type': _('Volume type must belong to the same service settings')})
        return attrs

    def create(self, validated_data):
        if not validated_data.get('size'):
            validated_data['size'] = validated_data['snapshot'].size
        if validated_data.get('image'):
            validated_data['image_name'] = validated_data['image'].name
        return super(VolumeSerializer, self).create(validated_data)


class VolumeExtendSerializer(serializers.Serializer):
    disk_size = serializers.IntegerField(min_value=1, label='Disk size')

    def validate_disk_size(self, disk_size):
        if disk_size < self.instance.size + 1024:
            raise serializers.ValidationError(
                _('Disk size should be greater or equal to %s') % (self.instance.size + 1024))
        return disk_size

    @transaction.atomic
    def update(self, instance, validated_data):
        new_size = validated_data.get('disk_size')

        settings = instance.service_project_link.service.settings
        spl = instance.service_project_link

        for quota_holder in [settings, spl]:
            quota_holder.add_quota_usage(quota_holder.Quotas.storage, new_size - instance.size, validate=True)

        instance.size = new_size
        instance.save(update_fields=['size'])
        return instance


class VolumeAttachSerializer(structure_serializers.PermissionFieldFilteringMixin,
                             serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Volume
        fields = ('instance', 'device')
        extra_kwargs = dict(
            instance={
                'required': True,
                'allow_null': False,
                'view_name': 'openstacktenant-instance-detail',
                'lookup_field': 'uuid',
            }
        )

    def get_fields(self):
        fields = super(VolumeAttachSerializer, self).get_fields()
        volume = self.instance
        if volume:
            fields['instance'].display_name_field = 'name'
            fields['instance'].query_params = {
                'project_uuid': volume.service_project_link.project.uuid.hex,
                'service_uuid': volume.service_project_link.service.uuid.hex,
            }
        return fields

    def get_filtered_field_names(self):
        return ('instance',)

    def validate_instance(self, instance):
        States, RuntimeStates = models.Instance.States, models.Instance.RuntimeStates
        if instance.state != States.OK or instance.runtime_state not in (RuntimeStates.SHUTOFF, RuntimeStates.ACTIVE):
            raise serializers.ValidationError(
                _('Volume can be attached only to shutoff or active instance in OK state.'))
        volume = self.instance
        if instance.service_project_link != volume.service_project_link:
            raise serializers.ValidationError(_('Volume and instance should belong to the same service and project.'))
        return instance

    def validate(self, attrs):
        instance = attrs['instance']
        device = attrs.get('device')
        if device and instance.volumes.filter(device=device).exists():
            raise serializers.ValidationError({'device': _('The supplied device path (%s) is in use.') % device})
        return attrs


class SnapshotRestorationSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(write_only=True, help_text=_('New volume name.'))
    description = serializers.CharField(required=False, help_text=_('New volume description.'))
    volume_state = serializers.CharField(source='volume.human_readable_state', read_only=True)

    class Meta(object):
        model = models.SnapshotRestoration
        fields = ('uuid', 'created', 'name', 'description',
                  'volume', 'volume_name', 'volume_state', 'volume_runtime_state', 'volume_size', 'volume_device')
        read_only_fields = ('uuid', 'created', 'volume')
        related_paths = {
            'volume': ('name', 'state', 'runtime_state', 'size', 'device')
        }
        extra_kwargs = dict(
            volume={'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-detail'},
        )

    @transaction.atomic
    def create(self, validated_data):
        snapshot = self.context['view'].get_object()
        validated_data['snapshot'] = snapshot
        description = validated_data.pop('description', None) or 'Restored from snapshot %s' % snapshot.name

        volume = models.Volume(
            source_snapshot=snapshot,
            service_project_link=snapshot.service_project_link,
            name=validated_data.pop('name'),
            description=description,
            size=snapshot.size,
        )

        volume.save()
        volume.increase_backend_quotas_usage()
        validated_data['volume'] = volume

        return super(SnapshotRestorationSerializer, self).create(validated_data)


class SnapshotSerializer(structure_serializers.BaseResourceActionSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstacktenant-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        read_only=True)

    source_volume_name = serializers.ReadOnlyField(source='source_volume.name')
    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(required=False)
    restorations = SnapshotRestorationSerializer(many=True, read_only=True)
    snapshot_schedule_uuid = serializers.ReadOnlyField(source='snapshot_schedule.uuid')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Snapshot
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'source_volume', 'size', 'metadata', 'runtime_state', 'source_volume_name', 'action', 'action_details',
            'restorations', 'kept_until', 'snapshot_schedule', 'snapshot_schedule_uuid'
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'size', 'source_volume', 'metadata', 'runtime_state', 'action', 'snapshot_schedule',
            'service_settings', 'project',
        )
        extra_kwargs = dict(
            source_volume={'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-detail'},
            snapshot_schedule={'lookup_field': 'uuid', 'view_name': 'openstacktenant-snapshot-schedule-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['source_volume'] = source_volume = self.context['view'].get_object()
        attrs['service_project_link'] = source_volume.service_project_link
        attrs['size'] = source_volume.size
        return super(SnapshotSerializer, self).validate(attrs)


class SnapshotImportableSerializer(core_serializers.AugmentedSerializerMixin,
                                   serializers.HyperlinkedModelSerializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        queryset=models.OpenStackTenantServiceProjectLink.objects.all(),
        write_only=True)
    source_volume_name = serializers.ReadOnlyField(source='source_volume.name')

    def get_filtered_field_names(self):
        return 'service_project_link',

    class Meta(object):
        model = models.Snapshot
        model_fields = ('name', 'description', 'size', 'action', 'action_details',
                        'metadata', 'runtime_state', 'state', 'source_volume_name', 'source_volume_name')
        fields = ('service_project_link', 'backend_id') + model_fields
        read_only_fields = model_fields + ('backend_id',)
        extra_kwargs = dict(
            source_volume={'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-detail'},
        )


class SnapshotImportSerializer(SnapshotImportableSerializer):
    class Meta(SnapshotImportableSerializer.Meta):
        fields = SnapshotImportableSerializer.Meta.fields + ('url', 'uuid', 'created')
        read_only_fields = SnapshotImportableSerializer.Meta.model_fields
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Snapshot.objects.filter(
            service_project_link__service__settings=service_project_link.service.settings,
            backend_id=backend_id
        ).exists():
            raise serializers.ValidationError({
                'backend_id': _('Snapshot has been imported already.')
            })

        try:
            backend = service_project_link.get_backend()
            snapshot = backend.import_snapshot(backend_id, save=True, service_project_link=service_project_link)
        except OpenStackBackendError:
            raise serializers.ValidationError({
                'backend_id': _("Can't import snapshot with ID %s") % validated_data['backend_id']
            })

        return snapshot


class NestedVolumeSerializer(core_serializers.AugmentedSerializerMixin,
                             serializers.HyperlinkedModelSerializer,
                             structure_serializers.BasicResourceSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    type_name = serializers.CharField(source='type.name', read_only=True)

    class Meta:
        model = models.Volume
        fields = ('url', 'uuid', 'name', 'image_name', 'state', 'bootable', 'size', 'device', 'resource_type',
                  'type', 'type_name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'type': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-type-detail'},
        }


class NestedSecurityGroupRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SecurityGroupRule
        fields = ('id', 'protocol', 'from_port', 'to_port', 'cidr')

    def to_internal_value(self, data):
        # Return exist security group as internal value if id is provided
        if 'id' in data:
            try:
                return models.SecurityGroupRule.objects.get(id=data['id'])
            except models.SecurityGroup:
                raise serializers.ValidationError(_('Security group with id %s does not exist') % data['id'])
        else:
            internal_data = super(NestedSecurityGroupRuleSerializer, self).to_internal_value(data)
            return models.SecurityGroupRule(**internal_data)


class NestedSecurityGroupSerializer(core_serializers.AugmentedSerializerMixin,
                                    core_serializers.HyperlinkedRelatedModelSerializer):
    rules = NestedSecurityGroupRuleSerializer(
        many=True,
        read_only=True,
    )
    state = serializers.ReadOnlyField(source='human_readable_state')

    class Meta(object):
        model = models.SecurityGroup
        fields = ('url', 'name', 'rules', 'description', 'state')
        read_only_fields = ('name', 'rules', 'description', 'state')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'}
        }


class NestedInternalIPSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):

    class Meta(object):
        model = models.InternalIP
        fields = (
            'ip4_address', 'mac_address', 'subnet', 'subnet_uuid', 'subnet_name', 'subnet_description', 'subnet_cidr')
        read_only_fields = (
            'ip4_address', 'mac_address', 'subnet_uuid', 'subnet_name', 'subnet_description', 'subnet_cidr')
        related_paths = {
            'subnet': ('uuid', 'name', 'description', 'cidr'),
        }
        extra_kwargs = {
            'subnet': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-subnet-detail'},
        }

    def to_internal_value(self, data):
        internal_value = super(NestedInternalIPSerializer, self).to_internal_value(data)
        return models.InternalIP(subnet=internal_value['subnet'], settings=internal_value['subnet'].settings)


class NestedFloatingIPSerializer(core_serializers.AugmentedSerializerMixin,
                                 core_serializers.HyperlinkedRelatedModelSerializer):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        source='internal_ip.subnet',
        view_name='openstacktenant-subnet-detail',
        lookup_field='uuid')
    subnet_uuid = serializers.ReadOnlyField(source='internal_ip.subnet.uuid')
    subnet_name = serializers.ReadOnlyField(source='internal_ip.subnet.name')
    subnet_description = serializers.ReadOnlyField(source='internal_ip.subnet.description')
    subnet_cidr = serializers.ReadOnlyField(source='internal_ip.subnet.cidr')

    class Meta(object):
        model = models.FloatingIP
        fields = ('url', 'uuid', 'address', 'internal_ip_ip4_address', 'internal_ip_mac_address',
                  'subnet', 'subnet_uuid', 'subnet_name', 'subnet_description', 'subnet_cidr')
        read_only_fields = ('address', 'internal_ip_ip4_address', 'internal_ip_mac_address')
        related_paths = {
            'internal_ip': ('ip4_address', 'mac_address')
        }
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-fip-detail'},
        }

    def to_internal_value(self, data):
        """
        Return pair (floating_ip, subnet) as internal value.

        On floating IP creation user should specify what subnet should be used
        for connection and may specify what exactly floating IP should be used.
        If floating IP is not specified it will be represented as None.
        """
        floating_ip = None
        if 'url' in data:
            # use HyperlinkedRelatedModelSerializer (parent of NestedFloatingIPSerializer)
            # method to convert "url" to FloatingIP object
            floating_ip = super(NestedFloatingIPSerializer, self).to_internal_value(data)

        # use HyperlinkedModelSerializer (parent of HyperlinkedRelatedModelSerializer)
        # to convert "subnet" to SubNet object
        internal_value = super(core_serializers.HyperlinkedRelatedModelSerializer, self).to_internal_value(data)
        subnet = internal_value['internal_ip']['subnet']

        return floating_ip, subnet


def _validate_instance_internal_ips(internal_ips, settings):
    """ - make sure that internal_ips belong to specified setting;
        - make sure that internal_ips does not connect to the same subnet twice;
    """
    if not internal_ips:
        raise serializers.ValidationError(
            {'internal_ips_set': _('Instance should be connected to at least one network.')})
    subnets = [internal_ip.subnet for internal_ip in internal_ips]
    for subnet in subnets:
        if subnet.settings != settings:
            message = _('Subnet %s does not belong to the same service settings as service project link.') % subnet
            raise serializers.ValidationError({'internal_ips_set': message})
    pairs = [(internal_ip.subnet, internal_ip.backend_id) for internal_ip in internal_ips]
    duplicates = [subnet for subnet, count in collections.Counter(pairs).items() if count > 1]
    if duplicates:
        raise serializers.ValidationError(_('It is impossible to connect to subnet %s twice.') % duplicates[0][0])


def _validate_instance_security_groups(security_groups, settings):
    """ Make sure that security_group belong to specified setting.
    """
    for security_group in security_groups:
        if security_group.settings != settings:
            error = _('Security group %s does not belong to the same service settings as service project link.')
            raise serializers.ValidationError({'security_groups': error % security_group.name})


def _validate_instance_floating_ips(floating_ips_with_subnets, settings, instance_subnets):
    if floating_ips_with_subnets and 'external_network_id' not in settings.options:
        raise serializers.ValidationError(
            ugettext('Please specify tenant external network to perform floating IP operations.'))

    for floating_ip, subnet in floating_ips_with_subnets:
        if subnet not in instance_subnets:
            message = ugettext('SubNet %s is not connected to instance.') % subnet
            raise serializers.ValidationError({'floating_ips': message})
        if not floating_ip:
            continue
        if floating_ip.is_booked:
            message = ugettext('Floating IP %s is already booked for another instance creation')
            raise serializers.ValidationError({'floating_ips': message % floating_ip})
        if floating_ip.settings != settings:
            message = ugettext('Floating IP %s does not belong to the same service settings as service project link.')
            raise serializers.ValidationError({'floating_ips': message % floating_ip})

    subnets = [subnet for _, subnet in floating_ips_with_subnets]
    duplicates = [subnet for subnet, count in collections.Counter(subnets).items() if count > 1]
    if duplicates:
        raise serializers.ValidationError(ugettext('It is impossible to use subnet %s twice.') % duplicates[0])


def _connect_floating_ip_to_instance(floating_ip, subnet, instance):
    """ Connect floating IP to instance via specified subnet.
        If floating IP is not defined - take exist free one or create a new one.
    """
    settings = instance.service_project_link.service.settings
    external_network_id = settings.options.get('external_network_id')
    if not core_utils.is_uuid_like(external_network_id):
        raise serializers.ValidationError(
            ugettext('Service provider does not have valid value of external_network_id'))

    if not floating_ip:
        kwargs = {
            'settings': settings,
            'is_booked': False,
            'backend_network_id': external_network_id,
        }
        # TODO: figure out why internal_ip__isnull throws errors when added to kwargs
        floating_ip = models.FloatingIP.objects.filter(internal_ip__isnull=True).filter(**kwargs).first()
        if not floating_ip:
            floating_ip = models.FloatingIP(**kwargs)
            floating_ip.increase_backend_quotas_usage()
    floating_ip.is_booked = True
    floating_ip.internal_ip = models.InternalIP.objects.filter(instance=instance, subnet=subnet).first()
    floating_ip.save()
    return floating_ip


class InstanceSerializer(structure_serializers.VirtualMachineSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstacktenant-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        queryset=models.OpenStackTenantServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    flavor = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-flavor-detail',
        lookup_field='uuid',
        queryset=models.Flavor.objects.all().select_related('settings'),
        write_only=True)

    image = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all().select_related('settings'),
        write_only=True)

    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(), many=True, required=False)
    internal_ips_set = NestedInternalIPSerializer(many=True, required=False)
    floating_ips = NestedFloatingIPSerializer(
        queryset=models.FloatingIP.objects.all().filter(internal_ip__isnull=True),
        many=True,
        required=False
    )

    system_volume_size = serializers.IntegerField(min_value=1024, write_only=True)
    data_volume_size = serializers.IntegerField(min_value=1024, required=False, write_only=True)

    volumes = NestedVolumeSerializer(many=True, required=False, read_only=True)
    action_details = serializers.JSONField(read_only=True)

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Instance
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            'flavor', 'image', 'system_volume_size', 'data_volume_size',
            'security_groups', 'internal_ips', 'flavor_disk', 'flavor_name',
            'floating_ips', 'volumes', 'runtime_state', 'action', 'action_details', 'internal_ips_set',
        )
        protected_fields = structure_serializers.VirtualMachineSerializer.Meta.protected_fields + (
            'flavor', 'image', 'system_volume_size', 'data_volume_size',
            'floating_ips', 'security_groups', 'internal_ips_set',
        )
        read_only_fields = structure_serializers.VirtualMachineSerializer.Meta.read_only_fields + (
            'flavor_disk', 'runtime_state', 'flavor_name', 'action',
        )

    def get_fields(self):
        fields = super(InstanceSerializer, self).get_fields()
        floating_ip_field = fields.get('floating_ips')
        if floating_ip_field:
            floating_ip_field.value_field = 'url'
            floating_ip_field.display_name_field = 'address'

        return fields

    @staticmethod
    def eager_load(queryset, request):
        queryset = structure_serializers.VirtualMachineSerializer.eager_load(queryset, request)
        return queryset.prefetch_related(
            'security_groups',
            'security_groups__rules',
            'volumes',
        )

    def validate(self, attrs):
        attrs = super(InstanceSerializer, self).validate(attrs)

        # skip validation on object update
        if self.instance is not None:
            return attrs

        service_project_link = attrs['service_project_link']
        settings = service_project_link.service.settings
        flavor = attrs['flavor']
        image = attrs['image']

        if any([flavor.settings != settings, image.settings != settings]):
            raise serializers.ValidationError(
                _('Flavor and image must belong to the same service settings as service project link.'))

        if image.min_ram > flavor.ram:
            raise serializers.ValidationError(
                {'flavor': _('RAM of flavor is not enough for selected image %s') % image.min_ram})

        if image.min_disk > attrs['system_volume_size']:
            raise serializers.ValidationError(
                {'system_volume_size': _('System volume size has to be greater than %s') % image.min_disk})

        internal_ips = attrs.get('internal_ips_set', [])
        _validate_instance_security_groups(attrs.get('security_groups', []), settings)
        _validate_instance_internal_ips(internal_ips, settings)
        subnets = [internal_ip.subnet for internal_ip in internal_ips]
        _validate_instance_floating_ips(attrs.get('floating_ips', []), settings, subnets)

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """ Store flavor, ssh_key and image details into instance model.
            Create volumes and security groups for instance.
        """
        security_groups = validated_data.pop('security_groups', [])
        internal_ips = validated_data.pop('internal_ips_set', [])
        floating_ips_with_subnets = validated_data.pop('floating_ips', [])
        spl = validated_data['service_project_link']
        ssh_key = validated_data.get('ssh_public_key')
        if ssh_key:
            # We want names to be human readable in backend.
            # OpenStack only allows latin letters, digits, dashes, underscores and spaces
            # as key names, thus we mangle the original name.
            safe_name = re.sub(r'[^-a-zA-Z0-9 _]+', '_', ssh_key.name)[:17]
            validated_data['key_name'] = '{0}-{1}'.format(ssh_key.uuid.hex, safe_name)
            validated_data['key_fingerprint'] = ssh_key.fingerprint

        flavor = validated_data['flavor']
        validated_data['flavor_name'] = flavor.name
        validated_data['cores'] = flavor.cores
        validated_data['ram'] = flavor.ram
        validated_data['flavor_disk'] = flavor.disk

        image = validated_data['image']
        validated_data['image_name'] = image.name
        validated_data['min_disk'] = image.min_disk
        validated_data['min_ram'] = image.min_ram

        system_volume_size = validated_data['system_volume_size']
        data_volume_size = validated_data.get('data_volume_size', 0)
        validated_data['disk'] = data_volume_size + system_volume_size

        instance = super(InstanceSerializer, self).create(validated_data)

        # security groups
        instance.security_groups.add(*security_groups)
        # internal IPs
        for internal_ip in internal_ips:
            internal_ip.instance = instance
            internal_ip.save()
        # floating IPs
        for floating_ip, subnet in floating_ips_with_subnets:
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)
        # volumes
        volumes = []
        system_volume = models.Volume.objects.create(
            name='{0}-system'.format(instance.name[:143]),  # volume name cannot be longer than 150 symbols
            service_project_link=spl,
            size=system_volume_size,
            image=image,
            image_name=image.name,
            bootable=True,
        )
        volumes.append(system_volume)

        if data_volume_size:
            data_volume = models.Volume.objects.create(
                name='{0}-data'.format(instance.name[:145]),  # volume name cannot be longer than 150 symbols
                service_project_link=spl,
                size=data_volume_size,
            )
            volumes.append(data_volume)

        for volume in volumes:
            volume.increase_backend_quotas_usage()

        instance.volumes.add(*volumes)
        return instance


class InstanceFlavorChangeSerializer(structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer):
    flavor = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-flavor-detail',
        lookup_field='uuid',
        queryset=models.Flavor.objects.all(),
    )

    def get_fields(self):
        fields = super(InstanceFlavorChangeSerializer, self).get_fields()
        if self.instance:
            fields['flavor'].query_params = {
                'settings_uuid': self.instance.service_project_link.service.settings.uuid
            }
        return fields

    def get_filtered_field_names(self):
        return ('flavor',)

    def validate_flavor(self, value):
        if value is not None:
            spl = self.instance.service_project_link

            if value.name == self.instance.flavor_name:
                raise serializers.ValidationError(
                    _('New flavor is the same as current.'))

            if value.settings != spl.service.settings:
                raise serializers.ValidationError(
                    _('New flavor is not within the same service settings'))

            if value.disk < self.instance.flavor_disk:
                raise serializers.ValidationError(
                    _('New flavor disk should be greater than the previous value'))
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        flavor = validated_data.get('flavor')

        spl = instance.service_project_link
        settings = spl.service.settings
        quota_holders = [spl, settings]

        # Service settings has optional field for related tenant resource.
        # We should update tenant quotas if related tenant is defined.
        # Otherwise stale quotas would be used for quota validation during instance provisioning.
        # Note that all tenant quotas are injected to service settings when application is bootstrapped.
        if settings.scope:
            quota_holders.append(settings.scope)

        for quota_holder in quota_holders:
            quota_holder.add_quota_usage(quota_holder.Quotas.ram, flavor.ram - instance.ram, validate=True)
            quota_holder.add_quota_usage(quota_holder.Quotas.vcpu, flavor.cores - instance.cores, validate=True)

        instance.ram = flavor.ram
        instance.cores = flavor.cores
        instance.flavor_disk = flavor.disk
        instance.flavor_name = flavor.name
        instance.save(update_fields=['ram', 'cores', 'flavor_name', 'flavor_disk'])
        return instance


class InstanceDeleteSerializer(serializers.Serializer):
    delete_volumes = serializers.BooleanField(default=True)
    release_floating_ips = serializers.BooleanField(label=_('Release floating IPs'), default=True)

    def validate(self, attrs):
        if attrs['delete_volumes'] and models.Snapshot.objects.filter(source_volume__instance=self.instance).exists():
            raise serializers.ValidationError(_('Cannot delete instance. One of its volumes has attached snapshot.'))
        return attrs


class InstanceSecurityGroupsUpdateSerializer(serializers.Serializer):
    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(),
        many=True,
    )

    def get_fields(self):
        fields = super(InstanceSecurityGroupsUpdateSerializer, self).get_fields()
        instance = self.instance
        if instance:
            fields['security_groups'].display_name_field = 'name'
            fields['security_groups'].view_name = 'openstacktenant-sgp-detail'
            fields['security_groups'].query_params = {
                'settings_uuid': instance.service_project_link.service.settings.uuid
            }
        return fields

    def validate_security_groups(self, security_groups):
        spl = self.instance.service_project_link

        for security_group in security_groups:
            if security_group.settings != spl.service.settings:
                raise serializers.ValidationError(
                    _('Security group %s is not within the same service settings') % security_group.name)

        return security_groups

    @transaction.atomic
    def update(self, instance, validated_data):
        security_groups = validated_data.pop('security_groups', None)
        if security_groups is not None:
            instance.security_groups.clear()
            instance.security_groups.add(*security_groups)

        return instance


class InstanceInternalIPsSetUpdateSerializer(serializers.Serializer):
    internal_ips_set = NestedInternalIPSerializer(many=True)

    def get_fields(self):
        fields = super(InstanceInternalIPsSetUpdateSerializer, self).get_fields()
        instance = self.instance
        if instance:
            fields['internal_ips_set'].view_name = 'openstacktenant-subnet-detail'
            fields['internal_ips_set'].query_params = {
                'settings_uuid': instance.service_project_link.service.settings.uuid
            }
        return fields

    def validate_internal_ips_set(self, internal_ips_set):
        spl = self.instance.service_project_link
        _validate_instance_internal_ips(internal_ips_set, spl.service.settings)
        return internal_ips_set

    @transaction.atomic
    def update(self, instance, validated_data):
        internal_ips_set = validated_data['internal_ips_set']
        new_subnets = [ip.subnet for ip in internal_ips_set]
        # delete stale IPs
        models.InternalIP.objects.filter(instance=instance).exclude(subnet__in=new_subnets).delete()
        # create new IPs
        for internal_ip in internal_ips_set:
            match = models.InternalIP.objects.filter(instance=instance, subnet=internal_ip.subnet).first()
            if not match:
                models.InternalIP.objects.create(instance=instance,
                                                 subnet=internal_ip.subnet,
                                                 settings=internal_ip.subnet.settings)

        return instance


class InstanceFloatingIPsUpdateSerializer(serializers.Serializer):
    floating_ips = NestedFloatingIPSerializer(queryset=models.FloatingIP.objects.all(), many=True, required=False)

    def get_fields(self):
        fields = super(InstanceFloatingIPsUpdateSerializer, self).get_fields()
        instance = self.instance
        if instance:
            queryset = models.FloatingIP.objects.all().filter(
                Q(internal_ip__isnull=True) | Q(internal_ip__instance=instance)
            )
            fields['floating_ips'] = NestedFloatingIPSerializer(queryset=queryset, many=True, required=False)
            fields['floating_ips'].view_name = 'openstacktenant-fip-detail'
            fields['floating_ips'].query_params = {
                'settings_uuid': instance.service_project_link.service.settings.uuid.hex,
                'is_booked': False,
                'free': True,
            }
        return fields

    def validate_floating_ips(self, floating_ips):
        spl = self.instance.service_project_link
        subnets = self.instance.subnets.all()
        _validate_instance_floating_ips(floating_ips, spl.service.settings, subnets)
        return floating_ips

    def update(self, instance, validated_data):
        floating_ips_with_subnets = validated_data['floating_ips']
        floating_ips_to_disconnect = list(self.instance.floating_ips)

        # Store both old and new floating IP addresses for action event logger
        new_floating_ips = [floating_ip for (floating_ip, subnet) in floating_ips_with_subnets if floating_ip]
        instance._old_floating_ips = [floating_ip.address for floating_ip in floating_ips_to_disconnect]
        instance._new_floating_ips = [floating_ip.address for floating_ip in new_floating_ips]

        for floating_ip, subnet in floating_ips_with_subnets:
            if floating_ip in floating_ips_to_disconnect:
                floating_ips_to_disconnect.remove(floating_ip)
                continue
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)
        for floating_ip in floating_ips_to_disconnect:
            floating_ip.internal_ip = None
            floating_ip.save()
        return instance


class BackupRestorationSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(
        required=False, help_text=_('New instance name. Leave blank to use source instance name.'))
    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(), many=True, required=False)
    internal_ips_set = NestedInternalIPSerializer(many=True, required=False)
    floating_ips = NestedFloatingIPSerializer(
        queryset=models.FloatingIP.objects.all().filter(internal_ip__isnull=True),
        many=True,
        required=False
    )

    class Meta(object):
        model = models.BackupRestoration
        fields = ('uuid', 'instance', 'created', 'flavor', 'name', 'floating_ips', 'security_groups',
                  'internal_ips_set')
        read_only_fields = ('url', 'uuid', 'instance', 'created', 'backup')
        extra_kwargs = dict(
            instance={'lookup_field': 'uuid', 'view_name': 'openstacktenant-instance-detail'},
            flavor={'lookup_field': 'uuid', 'view_name': 'openstacktenant-flavor-detail', 'allow_null': False,
                    'required': True},
        )

    def get_fields(self):
        fields = super(BackupRestorationSerializer, self).get_fields()
        view = self.context.get('view')  # On docs generation context does not contain "view".
        if view and view.action == 'restore' and self.instance:
            backup = self.instance
            settings = backup.instance.service_project_link.service.settings
            fields['flavor'].display_name_field = 'name'
            fields['flavor'].view_name = 'openstacktenant-flavor-detail'
            fields['flavor'].query_params = {
                'settings_uuid': backup.service_project_link.service.settings.uuid,
            }

            floating_ip_field = fields.get('floating_ips')
            if floating_ip_field:
                floating_ip_field.view_name = 'openstacktenant-fip-detail'
                floating_ip_field.query_params = {
                    'settings_uuid': settings.uuid,
                    'is_booked': False,
                    'free': True,
                }
                floating_ip_field.display_name_field = 'address'

            internal_ips_set_field = fields.get('internal_ips_set')
            if internal_ips_set_field:
                internal_ips_set_field.query_params = {
                    'settings_uuid': settings.uuid,
                }
                internal_ips_set_field.view_name = 'openstacktenant-subnet-detail'
                internal_ips_set_field.display_name_field = 'name'

            security_groups_field = fields.get('security_groups')
            if security_groups_field:
                security_groups_field.query_params = {
                    'settings_uuid': settings.uuid,
                }
                security_groups_field.view_name = 'openstacktenant-sgp-detail'
                security_groups_field.display_name_field = 'name'

        return fields

    def validate(self, attrs):
        flavor = attrs['flavor']
        backup = self.context['view'].get_object()
        try:
            backup.instance.volumes.get(bootable=True)
        except ObjectDoesNotExist:
            raise serializers.ValidationError(_('OpenStack instance should have bootable volume.'))

        settings = backup.instance.service_project_link.service.settings

        if flavor.settings != settings:
            raise serializers.ValidationError({'flavor': _('Flavor is not within services\' settings.')})

        _validate_instance_security_groups(attrs.get('security_groups', []), settings)

        internal_ips = attrs.get('internal_ips_set', [])
        _validate_instance_internal_ips(internal_ips, settings)

        subnets = [internal_ip.subnet for internal_ip in internal_ips]
        _validate_instance_floating_ips(attrs.get('floating_ips', []), settings, subnets)

        return attrs

    @transaction.atomic
    def update(self, backup_instance, validated_data):
        flavor = validated_data['flavor']
        validated_data['backup'] = backup = backup_instance
        source_instance = backup.instance
        # instance that will be restored
        metadata = backup.metadata or {}
        instance = models.Instance.objects.create(
            name=validated_data.pop('name', None) or metadata.get('name', source_instance.name),
            description=metadata.get('description', ''),
            service_project_link=backup.service_project_link,
            flavor_disk=flavor.disk,
            flavor_name=flavor.name,
            key_name=source_instance.key_name,
            key_fingerprint=source_instance.key_fingerprint,
            cores=flavor.cores,
            ram=flavor.ram,
            min_ram=metadata.get('min_ram', 0),
            min_disk=metadata.get('min_disk', 0),
            image_name=metadata.get('image_name', ''),
            user_data=metadata.get('user_data', ''),
            disk=sum([snapshot.size for snapshot in backup.snapshots.all()]),
        )

        instance.internal_ips_set.add(*validated_data.pop('internal_ips_set', []), bulk=False)
        instance.security_groups.add(*validated_data.pop('security_groups', []))

        for floating_ip, subnet in validated_data.pop('floating_ips', []):
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)

        instance.increase_backend_quotas_usage()
        validated_data['instance'] = instance
        backup_restoration = super(BackupRestorationSerializer, self).create(validated_data)
        # restoration for each instance volume from snapshot.
        for snapshot in backup.snapshots.all():
            volume = models.Volume(
                source_snapshot=snapshot,
                service_project_link=snapshot.service_project_link,
                name='{0}-volume'.format(instance.name[:143]),
                description='Restored from backup %s' % backup.uuid.hex,
                size=snapshot.size,
            )
            volume.save()
            volume.increase_backend_quotas_usage()
            instance.volumes.add(volume)
        return backup_restoration


class BackupSerializer(structure_serializers.BaseResourceActionSerializer):
    # Serializer requires OpenStack Instance in context on creation
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstacktenant-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        read_only=True,
    )
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.ReadOnlyField(source='instance.name')
    instance_security_groups = NestedSecurityGroupSerializer(
        read_only=True, many=True, source='instance.security_groups')
    instance_internal_ips_set = NestedInternalIPSerializer(
        read_only=True, many=True, source='instance.internal_ips_set')
    instance_floating_ips = NestedFloatingIPSerializer(
        read_only=True, many=True, source='instance.floating_ips')

    restorations = BackupRestorationSerializer(many=True, read_only=True)
    backup_schedule_uuid = serializers.ReadOnlyField(source='backup_schedule.uuid')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Backup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'kept_until', 'metadata', 'instance', 'instance_name', 'restorations',
            'backup_schedule', 'backup_schedule_uuid',
            'instance_security_groups', 'instance_internal_ips_set', 'instance_floating_ips')
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'instance', 'service_project_link', 'backup_schedule', 'service_settings', 'project')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'instance': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-instance-detail'},
            'backup_schedule': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-backup-schedule-detail'},
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['instance'] = instance = self.context['view'].get_object()
        attrs['service_project_link'] = instance.service_project_link
        attrs['metadata'] = self.get_backup_metadata(instance)
        return super(BackupSerializer, self).validate(attrs)

    @transaction.atomic
    def create(self, validated_data):
        backup = super(BackupSerializer, self).create(validated_data)
        self.create_backup_snapshots(backup)
        return backup

    @staticmethod
    def get_backup_metadata(instance):
        return {
            'name': instance.name,
            'description': instance.description,
            'min_ram': instance.min_ram,
            'min_disk': instance.min_disk,
            'size': instance.size,
            'key_name': instance.key_name,
            'key_fingerprint': instance.key_fingerprint,
            'user_data': instance.user_data,
            'flavor_name': instance.flavor_name,
            'image_name': instance.image_name,
        }

    @staticmethod
    def create_backup_snapshots(backup):
        for volume in backup.instance.volumes.all():
            snapshot = models.Snapshot.objects.create(
                name='Part of backup: %s (volume: %s)' % (backup.name[:60], volume.name[:60]),
                service_project_link=backup.service_project_link,
                size=volume.size,
                source_volume=volume,
                description='Part of backup %s (UUID: %s)' % (backup.name, backup.uuid.hex),
            )
            snapshot.increase_backend_quotas_usage()
            backup.snapshots.add(snapshot)


class BaseScheduleSerializer(structure_serializers.BaseResourceActionSerializer):
    timezone = serializers.ChoiceField(choices=[(t, t) for t in pytz.all_timezones],
                                       initial=timezone.get_current_timezone_name(),
                                       default=timezone.get_current_timezone_name())
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstacktenant-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        read_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'retention_time', 'timezone', 'maximal_number_of_resources', 'schedule',
            'is_active', 'next_trigger_at')
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'is_active', 'next_trigger_at', 'service_project_link', 'service_settings', 'project')


class BackupScheduleSerializer(BaseScheduleSerializer):

    class Meta(BaseScheduleSerializer.Meta):
        model = models.BackupSchedule
        fields = BaseScheduleSerializer.Meta.fields + (
            'instance', 'instance_name')
        read_only_fields = BaseScheduleSerializer.Meta.read_only_fields + (
            'backups', 'instance')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'instance': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-instance-detail'},
        }
        related_paths = {
            'instance': ('name',),
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        instance = self.context['view'].get_object()
        if not instance.volumes.filter(bootable=True).exists():
            raise serializers.ValidationError(_('OpenStack instance should have bootable volume.'))
        attrs['instance'] = instance
        attrs['service_project_link'] = instance.service_project_link
        attrs['state'] = instance.States.OK
        return super(BackupScheduleSerializer, self).validate(attrs)


class SnapshotScheduleSerializer(BaseScheduleSerializer):

    class Meta(BaseScheduleSerializer.Meta):
        model = models.SnapshotSchedule
        fields = BaseScheduleSerializer.Meta.fields + (
            'source_volume', 'source_volume_name')
        read_only_fields = BaseScheduleSerializer.Meta.read_only_fields + (
            'snapshots', 'source_volume')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'source_volume': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-volume-detail'},
        }
        related_paths = {
            'source_volume': ('name',),
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        volume = self.context['view'].get_object()
        attrs['source_volume'] = volume
        attrs['service_project_link'] = volume.service_project_link
        attrs['state'] = volume.States.OK
        return super(SnapshotScheduleSerializer, self).validate(attrs)


class MeterSampleSerializer(serializers.Serializer):
    name = serializers.CharField(source='counter_name')
    value = serializers.FloatField(source='counter_volume')
    type = serializers.CharField(source='counter_type')
    unit = serializers.CharField(source='counter_unit')
    timestamp = fields.StringTimestampField(formats=('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'))
    recorded_at = fields.StringTimestampField(formats=('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'))


class MeterTimestampIntervalSerializer(core_serializers.TimestampIntervalSerializer):
    def get_fields(self):
        fields = super(MeterTimestampIntervalSerializer, self).get_fields()
        fields['start'].default = core_utils.timeshift(hours=-1)
        fields['end'].default = core_utils.timeshift()
        return fields


def get_instance(openstack_floating_ip):
    # cache openstack instance on openstack floating_ip instance
    if hasattr(openstack_floating_ip, '_instance'):
        return openstack_floating_ip._instance
    if not openstack_floating_ip.backend_id or not openstack_floating_ip.address:
        openstack_floating_ip._instance = None
        return
    try:
        floating_ip = models.FloatingIP.objects\
            .exclude(internal_ip__isnull=True)\
            .get(backend_id=openstack_floating_ip.backend_id,
                 address=openstack_floating_ip.address)
    except models.FloatingIP.DoesNotExist:
        openstack_floating_ip._instance = None
    else:
        instance = getattr(floating_ip.internal_ip, 'instance', None)
        openstack_floating_ip._instance = instance
        return instance


def get_instance_attr(openstack_floating_ip, name):
    instance = get_instance(openstack_floating_ip)
    return getattr(instance, name, None)


def get_instance_uuid(serializer, openstack_floating_ip):
    return get_instance_attr(openstack_floating_ip, 'uuid')


def get_instance_name(serializer, openstack_floating_ip):
    return get_instance_attr(openstack_floating_ip, 'name')


def get_instance_url(serializer, openstack_floating_ip):
    instance = get_instance(openstack_floating_ip)
    if instance:
        return reverse('openstacktenant-instance-detail', kwargs={'uuid': instance.uuid.hex},
                       request=serializer.context['request'])


def add_instance_fields(sender, fields, **kwargs):
    fields['instance_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_instance_uuid', get_instance_uuid)
    fields['instance_name'] = serializers.SerializerMethodField()
    setattr(sender, 'get_instance_name', get_instance_name)
    fields['instance_url'] = serializers.SerializerMethodField()
    setattr(sender, 'get_instance_url', get_instance_url)


core_signals.pre_serializer_fields.connect(add_instance_fields, sender=openstack_serializers.FloatingIPSerializer)


class InstanceImportableSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-spl-detail',
        queryset=models.OpenStackTenantServiceProjectLink.objects.all(),
        write_only=True)

    def get_filtered_field_names(self):
        return 'service_project_link',

    class Meta(object):
        model = models.Instance
        model_fields = ('name', 'description', 'state', 'runtime_state', 'flavor_name', 'size', 'ram', 'cores')
        fields = ('service_project_link', 'backend_id') + model_fields
        read_only_fields = model_fields + ('backend_id',)


class InstanceImportSerializer(InstanceImportableSerializer):
    class Meta(InstanceImportableSerializer.Meta):
        fields = InstanceImportableSerializer.Meta.fields + ('url', 'uuid', 'created')
        read_only_fields = InstanceImportableSerializer.Meta.model_fields
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Instance.objects.filter(
            service_project_link__service__settings=service_project_link.service.settings,
            backend_id=backend_id
        ).exists():
            raise serializers.ValidationError({'backend_id': _('Instance has been imported already.')})

        try:
            backend = service_project_link.get_backend()
            instance = backend.import_instance(backend_id, save=True, service_project_link=service_project_link)
        except OpenStackBackendError:
            raise serializers.ValidationError({
                'backend_id': _("Can't import instance with ID %s") % validated_data['backend_id']
            })

        return instance


class ConsoleLogSerializer(serializers.Serializer):
    length = serializers.IntegerField(required=False)


class VolumeTypeSerializer(structure_serializers.BasePropertySerializer):
    settings = serializers.HyperlinkedRelatedField(
        queryset=structure_models.ServiceSettings.objects.all(),
        view_name='servicesettings-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
    )

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.VolumeType
        fields = ('url', 'uuid', 'name', 'description', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }


class VolumeAvailabilitySerializer(structure_serializers.BasePropertySerializer):
    settings = serializers.HyperlinkedRelatedField(
        queryset=structure_models.ServiceSettings.objects.all(),
        view_name='servicesettings-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
    )

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.VolumeAvailabilityZone
        fields = ('url', 'uuid', 'name', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }


class SharedSettingsCustomerSerializer(serializers.Serializer):
    name = serializers.ReadOnlyField()
    uuid = serializers.ReadOnlyField()
    created = serializers.ReadOnlyField()
    abbreviation = serializers.ReadOnlyField()
    vm_count = serializers.ReadOnlyField()
