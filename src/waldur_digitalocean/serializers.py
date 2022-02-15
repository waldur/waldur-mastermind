import re

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from waldur_core.structure import serializers as structure_serializers

from . import models


class DigitalOceanServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ('token',)

    token = serializers.CharField(label=_('Access token'))


class RegionSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Region
        fields = ('url', 'uuid', 'name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Image
        fields = (
            'url',
            'uuid',
            'name',
            'distribution',
            'type',
            'regions',
            'is_official',
            'created_at',
            'min_disk_size',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    regions = RegionSerializer(many=True, read_only=True)


class SizeSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Size
        fields = ('url', 'uuid', 'name', 'cores', 'ram', 'disk', 'transfer', 'regions')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    regions = RegionSerializer(many=True, read_only=True)


class DropletSerializer(structure_serializers.VirtualMachineSerializer):

    region = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-region-detail',
        lookup_field='uuid',
        queryset=models.Region.objects.all(),
        write_only=True,
    )

    image = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all(),
        write_only=True,
    )

    size = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
        write_only=True,
    )

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Droplet
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            'region',
            'image',
            'size',
            'runtime_state',
            'region_name',
        )
        protected_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.protected_fields
            + (
                'region',
                'image',
                'size',
            )
        )
        read_only_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.read_only_fields
            + (
                'runtime_state',
                'region_name',
            )
        )

    def validate(self, attrs):
        attrs = super(DropletSerializer, self).validate(attrs)

        if not self.instance:
            region = attrs['region']
            image = attrs['image']
            size = attrs['size']

            if not re.match(r'[a-zA-Z0-9.-]+$', attrs['name']):
                raise serializers.ValidationError(
                    {
                        'name': _(
                            'Only valid hostname characters are allowed. (a-z, A-Z, 0-9, . and -)'
                        )
                    }
                )

            if not attrs.get('ssh_public_key') and image.is_ssh_key_mandatory:
                raise serializers.ValidationError(
                    {'ssh_public_key': _('SSH public key is required for this image')}
                )

            if not image.regions.filter(pk=region.pk).exists():
                raise serializers.ValidationError(
                    {'image': _('Image is missing in region %s') % region}
                )

            if not size.regions.filter(pk=region.pk).exists():
                raise serializers.ValidationError(
                    {'size': _('Size is missing in region %s') % region}
                )

            if image.min_disk_size and size.disk < image.min_disk_size:
                raise serializers.ValidationError(
                    {
                        'size': _(
                            'Disk provided by size %(size)s is not enough for image %(image)s'
                        )
                        % ({'size': size, 'image': image})
                    }
                )

        return attrs

    def create(self, validated_data):
        validated_data['region_name'] = validated_data['region'].name
        validated_data['size_name'] = validated_data['size'].name
        return super(DropletSerializer, self).create(validated_data)


class DropletResizeSerializer(serializers.Serializer):
    size = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
        write_only=True,
    )
    disk = serializers.BooleanField(required=True)

    class Meta:
        fields = 'size', 'disk'

    def validate_size(self, value):
        if value:
            if self.is_same_size(value):
                raise ValidationError(
                    _('New size is the same. Please select another one.')
                )

            if value.disk < self.instance.disk:
                raise ValidationError(
                    _(
                        'Disk sizes are not allowed to be decreased through a resize operation.'
                    )
                )
        return value

    def is_same_size(self, new_size):
        return (
            new_size.disk == self.instance.disk
            and new_size.cores == self.instance.cores
            and new_size.ram == self.instance.ram
        )
