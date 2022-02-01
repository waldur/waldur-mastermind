from django.db import transaction
from django.utils.translation import gettext_lazy as _
from libcloud.compute.types import NodeState
from rest_framework import serializers

from waldur_core.structure import serializers as structure_serializers

from . import models


class AwsServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ('username', 'token')

    username = serializers.CharField(label=_('Access key ID'))

    token = serializers.CharField(label=_('Secret access key'))

    images_regex = serializers.CharField(
        source='options.images_regex',
        help_text=_('Regular expression to limit images list'),
        required=False,
    )


class RegionSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Region
        fields = ('url', 'uuid', 'name')
        extra_kwargs = {'url': {'lookup_field': 'uuid'}}


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Image
        fields = ('url', 'uuid', 'name', 'region')
        extra_kwargs = {'url': {'lookup_field': 'uuid'}}

    region = RegionSerializer(read_only=True)


class SizeSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Size
        fields = (
            'url',
            'uuid',
            'name',
            'cores',
            'ram',
            'disk',
            'regions',
            'description',
        )
        extra_kwargs = {'url': {'lookup_field': 'uuid'}}

    # AWS expose a more technical backend_id as a name. AWS's short codes are more popular
    name = serializers.ReadOnlyField(source='backend_id')
    description = serializers.ReadOnlyField(source='name')
    regions = RegionSerializer(many=True, read_only=True)


class InstanceSerializer(structure_serializers.VirtualMachineSerializer):
    region = serializers.HyperlinkedRelatedField(
        view_name='aws-region-detail',
        lookup_field='uuid',
        queryset=models.Region.objects.all(),
        write_only=True,
    )

    image = serializers.HyperlinkedRelatedField(
        view_name='aws-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all(),
        write_only=True,
    )

    size = serializers.HyperlinkedRelatedField(
        view_name='aws-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
        write_only=True,
    )

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Instance
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            'region',
            'image',
            'size',
        )
        protected_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.protected_fields
            + ('region', 'image', 'size')
        )

    def validate(self, attrs):
        attrs = super(InstanceSerializer, self).validate(attrs)

        region = attrs['region']
        image = attrs['image']
        size = attrs['size']

        if image.region != region:
            raise serializers.ValidationError(
                _('Image is missing in region %s') % region.name
            )

        if not size.regions.filter(pk=region.pk).exists():
            raise serializers.ValidationError(
                _('Size is missing in region %s') % region.name
            )

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        size = validated_data.get('size')
        ssh_key = validated_data.get('ssh_public_key')

        validated_data['ram'] = size.ram
        validated_data['cores'] = size.cores
        validated_data['disk'] = size.disk
        validated_data['size_backend_id'] = size.backend_id

        if ssh_key is not None:
            validated_data['key_name'] = ssh_key.name
            validated_data['key_fingerprint'] = ssh_key.fingerprint

        instance = super(InstanceSerializer, self).create(validated_data)
        volume = {
            'name': ('temp-%s' % instance.name)[:150],
            'state': models.Volume.States.CREATION_SCHEDULED,
            'instance': instance,
            'service_settings': instance.service_settings,
            'project': instance.project,
            'region': instance.region,
            # Size will be received from the backend
            'size': 0,
        }
        models.Volume.objects.create(**volume)

        return instance


class InstanceResizeSerializer(
    structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer
):
    size = serializers.HyperlinkedRelatedField(
        view_name='aws-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
    )

    def get_filtered_field_names(self):
        return ('size',)

    def validate(self, attrs):
        size = attrs['size']
        instance = self.instance

        if not size.regions.filter(uuid=self.instance.region.uuid).exists():
            raise serializers.ValidationError(
                _('New size is not within the same region.')
            )

        if (size.ram, size.disk, size.cores) == (
            self.instance.ram,
            self.instance.disk,
            self.instance.cores,
        ):
            raise serializers.ValidationError(_('New size is the same as current.'))

        if size.disk < self.instance.disk:
            raise serializers.ValidationError(
                _('New disk size should be greater than the previous value')
            )

        if instance.runtime_state not in [
            NodeState.TERMINATED,
            NodeState.STOPPED,
            NodeState.SUSPENDED,
            NodeState.PAUSED,
        ]:
            raise serializers.ValidationError(
                _('Instance runtime state must be in one of offline states.')
            )

        return attrs

    def update(self, instance, validated_data):
        size = validated_data.get('size')

        instance.ram = size.ram
        instance.cores = size.cores
        instance.disk = size.disk
        instance.save(update_fields=['ram', 'cores', 'disk'])

        return instance


class VolumeSerializer(structure_serializers.BaseResourceSerializer):
    region = serializers.HyperlinkedRelatedField(
        view_name='aws-region-detail',
        lookup_field='uuid',
        queryset=models.Region.objects.all(),
        write_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Volume
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'size',
            'region',
            'volume_type',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ('device', 'instance', 'runtime_state')
        )
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'size',
            'region',
            'volume_type',
            'device',
            'instance',
            'runtime_state',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'instance': {'lookup_field': 'uuid', 'view_name': 'aws-instance-detail'},
        }


class VolumeAttachSerializer(
    structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer
):
    instance = serializers.HyperlinkedRelatedField(
        view_name='aws-instance-detail',
        lookup_field='uuid',
        queryset=models.Instance.objects.all(),
    )
    device = serializers.CharField(
        max_length=128,
        help_text=_(
            'The device name for attachment. For example, use /dev/sd[f-p] for Linux instances.'
        ),
    )

    def get_filtered_field_names(self):
        return ('instance',)

    def validate(self, attrs):
        volume = self.instance
        instance = attrs['instance']

        if volume.instance:
            raise serializers.ValidationError(
                _('Volume is already attached to instance.')
            )

        if volume.region != instance.region:
            raise serializers.ValidationError(
                _('Instance is not within the same region.')
            )

        if instance.runtime_state not in [
            NodeState.TERMINATED,
            NodeState.STOPPED,
            NodeState.SUSPENDED,
            NodeState.PAUSED,
        ]:
            raise serializers.ValidationError(
                _('Instance runtime state must be in one of offline states.')
            )

        return attrs

    def update(self, volume, validated_data):
        volume.instance = validated_data.get('instance')
        volume.device = validated_data.get('device')
        volume.save(update_fields=['instance', 'device'])

        return volume
