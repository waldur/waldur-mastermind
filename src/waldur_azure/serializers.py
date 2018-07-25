import re

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from django.db import transaction
from django.utils import six, timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import models
from .backend import AzureBackendError, SizeQueryset


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'username': _('In the format of GUID'),
        'certificate': _('X509 certificate in .PEM format'),
    }
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'location': '',
        'cloud_service_name': '',
        'images_regex': ''
    }

    location = serializers.ChoiceField(
        choices=models.AzureService.Locations,
        write_only=True,
        required=False,
        allow_blank=True)

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.AzureService
        view_name = 'azure-detail'
        extra_field_options = {
            'username': {
                'label': 'Subscription ID',
                'required': True
            },
            'certificate': {
                'label': 'Private certificate file',
                'required': True,
                'write_only': True
            },
            'location': {
                'help_text': _('Azure region where to provision resources (default: "Central US")')
            },
            'cloud_service_name': {
                'help_text': _('Cloud service group to assign all connected SPLs to'),
                'required': True,
            },
            'images_regex': {
                'help_text': _('Regular expression to limit images list')
            }
        }

    def validate_certificate(self, value):
        if value:
            try:
                x509.load_pem_x509_certificate(value.read(), default_backend())
            except ValueError:
                raise serializers.ValidationError(_('Valid X509 certificate in .PEM format is expected'))

        return value


class ImageSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Image
        view_name = 'azure-image-detail'
        fields = ('url', 'uuid', 'name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class SizeSerializer(six.with_metaclass(structure_serializers.PropertySerializerMetaclass,
                                        serializers.Serializer)):

    uuid = serializers.ReadOnlyField()
    url = serializers.SerializerMethodField()
    name = serializers.ReadOnlyField()
    cores = serializers.ReadOnlyField()
    ram = serializers.ReadOnlyField()
    disk = serializers.ReadOnlyField()

    class Meta(object):
        model = models.Size

    def get_url(self, size):
        return reverse('azure-size-detail', kwargs={'uuid': size.uuid}, request=self.context.get('request'))


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):

    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.AzureServiceProjectLink
        view_name = 'azure-spl-detail'
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'azure-detail'},
        }


class InstanceEndpointsSerializer(serializers.ModelSerializer):
    local_port = serializers.ReadOnlyField()
    public_port = serializers.ReadOnlyField()
    protocol = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()

    class Meta(object):
        model = models.InstanceEndpoint
        fields = ('local_port', 'public_port', 'protocol', 'name')


class VirtualMachineSerializer(structure_serializers.BaseResourceSerializer):
    endpoints = InstanceEndpointsSerializer(many=True, read_only=True)

    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='azure-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='azure-spl-detail',
        queryset=models.AzureServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    image = serializers.HyperlinkedRelatedField(
        view_name='azure-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all(),
        write_only=True)

    size = serializers.HyperlinkedRelatedField(
        view_name='azure-size-detail',
        lookup_field='uuid',
        queryset=SizeQueryset(),
        write_only=True)

    external_ips = serializers.ListField(
        child=serializers.IPAddressField(),
        read_only=True,
    )

    user_username = serializers.CharField(required=True)
    user_password = serializers.CharField(required=True, style={'input_type': 'password'})

    rdp = serializers.HyperlinkedIdentityField(view_name='azure-virtualmachine-rdp', lookup_field='uuid')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.VirtualMachine
        view_name = 'azure-virtualmachine-detail'
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'image', 'size', 'user_username', 'user_password', 'user_data', 'rdp', 'external_ips', 'internal_ips',
            'runtime_state', 'start_time', 'cores', 'ram', 'disk', 'image_name', 'endpoints',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'image', 'size', 'user_username', 'user_password', 'user_data'
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'external_ips', 'internal_ips', 'runtime_state', 'start_time', 'cores', 'ram', 'disk',
            'image_name'
        )

    def validate(self, attrs):
        attrs = super(VirtualMachineSerializer, self).validate(attrs)

        if not re.match(r'[a-zA-Z][a-zA-Z0-9-]{0,13}[a-zA-Z0-9]$', attrs['name']):
            raise serializers.ValidationError(
                {'name': _("The name can contain only letters, numbers, and hyphens. "
                           "The name must be shorter than 15 characters and start with "
                           "a letter and must end with a letter or a number.")})

        # passwords must contain characters from at least three of the following four categories:
        groups = (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^a-zA-Z\d\s:]')
        password = attrs['user_password']
        if not 6 <= len(password) <= 72 or sum(bool(re.search(g, password)) for g in groups) < 3:
            raise serializers.ValidationError({
                'user_password': _("The supplied password must be 6-72 characters long "
                                   "and contain 3 of the following: a lowercase character, "
                                   "an uppercase character, a number, a special character.")})

        if re.match(r'Administrator|Admin', attrs['user_username'], re.I):
            raise serializers.ValidationError({'user_username': _('Invalid administrator username.')})

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        image = validated_data['image']
        validated_data['image_name'] = image.name
        size = validated_data['size']
        validated_data['cores'] = size.cores
        validated_data['ram'] = size.ram
        validated_data['disk'] = size.disk
        return super(VirtualMachineSerializer, self).create(validated_data)


class VirtualMachineImportSerializer(structure_serializers.BaseResourceImportSerializer):

    class Meta(structure_serializers.BaseResourceImportSerializer.Meta):
        model = models.VirtualMachine
        view_name = 'azure-virtualmachine-detail'
        fields = structure_serializers.BaseResourceImportSerializer.Meta.fields + (
            'cores', 'ram', 'disk',
            'external_ips', 'internal_ips',
        )

    def create(self, validated_data):
        spl = validated_data['service_project_link']
        backend = spl.get_backend()

        try:
            vm = backend.get_vm(validated_data['backend_id'])
        except AzureBackendError:
            raise serializers.ValidationError(
                {'backend_id': _("Can't find Virtual Machine with ID %s") % validated_data['backend_id']})

        validated_data['name'] = vm.name
        validated_data['created'] = timezone.now()
        validated_data['ram'] = vm.size.ram
        validated_data['disk'] = vm.size.disk
        validated_data['cores'] = 'Shared' and 1 or vm.size.extra['cores']
        validated_data['external_ips'] = vm.public_ips[0]
        validated_data['internal_ips'] = vm.private_ips[0]
        validated_data['state'] = models.VirtualMachine.States.ONLINE \
            if vm.state == backend.State.RUNNING else models.VirtualMachine.States.OFFLINE

        return super(VirtualMachineImportSerializer, self).create(validated_data)
