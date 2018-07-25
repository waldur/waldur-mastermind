from __future__ import unicode_literals

import re

from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import models
from .backend import DigitalOceanBackendError


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'token': '',
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.DigitalOceanService
        extra_field_options = {
            'token': {
                'label': 'Access token'
            }
        }


class RegionSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Region
        fields = ('url', 'uuid', 'name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ImageSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Image
        fields = ('url', 'uuid', 'name', 'distribution', 'type', 'regions',
                  'is_official', 'created_at', 'min_disk_size')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    regions = RegionSerializer(many=True, read_only=True)


class SizeSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Size
        fields = ('url', 'uuid', 'name', 'cores', 'ram', 'disk', 'transfer', 'regions')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    regions = RegionSerializer(many=True, read_only=True)


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):

    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.DigitalOceanServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'digitalocean-detail'},
        }


class DropletSerializer(structure_serializers.VirtualMachineSerializer):

    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='digitalocean-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-spl-detail',
        queryset=models.DigitalOceanServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    region = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-region-detail',
        lookup_field='uuid',
        queryset=models.Region.objects.all(),
        write_only=True)

    image = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all(),
        write_only=True)

    size = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
        write_only=True)

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Droplet
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            'region', 'image', 'size', 'runtime_state', 'region_name',
        )
        protected_fields = structure_serializers.VirtualMachineSerializer.Meta.protected_fields + (
            'region', 'image', 'size',
        )
        read_only_fields = structure_serializers.VirtualMachineSerializer.Meta.read_only_fields + (
            'runtime_state', 'region_name',
        )

    def validate(self, attrs):
        attrs = super(DropletSerializer, self).validate(attrs)

        if not self.instance:
            region = attrs['region']
            image = attrs['image']
            size = attrs['size']

            if not re.match(r'[a-zA-Z0-9.-]+$', attrs['name']):
                raise serializers.ValidationError({
                    'name': _('Only valid hostname characters are allowed. (a-z, A-Z, 0-9, . and -)')
                })

            if not attrs.get('ssh_public_key') and image.is_ssh_key_mandatory:
                raise serializers.ValidationError({
                    'ssh_public_key': _('SSH public key is required for this image')
                })

            if not image.regions.filter(pk=region.pk).exists():
                raise serializers.ValidationError({
                    'image': _('Image is missing in region %s') % region
                })

            if not size.regions.filter(pk=region.pk).exists():
                raise serializers.ValidationError({
                    'size': _('Size is missing in region %s') % region
                })

            if image.min_disk_size and size.disk < image.min_disk_size:
                raise serializers.ValidationError({
                    'size': _('Disk provided by size %(size)s is not enough for image %(image)s') % ({
                        'size': size,
                        'image': image
                    })
                })

        return attrs

    def create(self, validated_data):
        validated_data['region_name'] = validated_data['region'].name
        validated_data['size_name'] = validated_data['size'].name
        return super(DropletSerializer, self).create(validated_data)


class DropletImportSerializer(structure_serializers.BaseResourceImportSerializer):

    class Meta(structure_serializers.BaseResourceImportSerializer.Meta):
        model = models.Droplet

    def create(self, validated_data):
        backend = self.context['service'].get_backend()
        backend_id = validated_data['backend_id']
        service_project_link = validated_data['service_project_link']
        try:
            return backend.import_droplet(backend_id, service_project_link)
        except DigitalOceanBackendError:
            raise serializers.ValidationError(
                {'backend_id': _("Can't find droplet with ID %s") % backend_id})


class DropletResizeSerializer(serializers.Serializer):
    size = serializers.HyperlinkedRelatedField(
        view_name='digitalocean-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
        write_only=True)
    disk = serializers.BooleanField(required=True)

    def get_fields(self):
        fields = super(DropletResizeSerializer, self).get_fields()
        field = fields['size']
        field.value_field = 'url'
        field.display_name_field = 'name'
        return fields

    class Meta:
        fields = 'size', 'disk'

    def validate_size(self, value):
        if value:
            if self.is_same_size(value):
                raise ValidationError(_('New size is the same. Please select another one.'))

            if value.disk < self.instance.disk:
                raise ValidationError(_('Disk sizes are not allowed to be decreased through a resize operation.'))
        return value

    def is_same_size(self, new_size):
        return (new_size.disk == self.instance.disk and
                new_size.cores == self.instance.cores and
                new_size.ram == self.instance.ram)
