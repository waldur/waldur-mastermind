from __future__ import unicode_literals

from zipfile import is_zipfile, ZipFile

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers, exceptions

from waldur_ansible.common import serializers as common_serializers
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.structure import permissions as structure
from waldur_core.structure import serializers as structure_serializers
from waldur_openstack.openstack_tenant import models as openstack_models

from . import models as playbook_jobs_models


class PlaybookParameterSerializer(serializers.ModelSerializer):
    name = serializers.RegexField('^[\w]+$')

    class Meta(object):
        model = playbook_jobs_models.PlaybookParameter
        fields = ('name', 'description', 'required', 'default')


class PlaybookSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    archive = serializers.FileField(write_only=True)
    parameters = PlaybookParameterSerializer(many=True)

    class Meta(object):
        model = playbook_jobs_models.Playbook
        fields = ('url', 'uuid', 'name', 'description', 'archive', 'entrypoint', 'parameters', 'image')
        protected_fields = ('entrypoint', 'parameters', 'archive')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def validate_archive(self, value):
        if not is_zipfile(value):
            raise serializers.ValidationError(_('ZIP file must be uploaded.'))
        elif not value.name.endswith('.zip'):
            raise serializers.ValidationError(_("File must have '.zip' extension."))

        zip_file = ZipFile(value)
        invalid_file = zip_file.testzip()
        if invalid_file is not None:
            raise serializers.ValidationError(
                _('File {filename} in archive {archive_name} has an invalid type.'.format(
                    filename=invalid_file, archive_name=zip_file.filename)))

        return value

    def validate(self, attrs):
        if self.instance:
            return attrs

        zip_file = ZipFile(attrs['archive'])
        entrypoint = attrs['entrypoint']
        if entrypoint not in zip_file.namelist():
            raise serializers.ValidationError(
                _('Failed to find entrypoint {entrypoint} in archive {archive_name}.'.format(
                    entrypoint=entrypoint, archive_name=zip_file.filename)))

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        parameters_data = validated_data.pop('parameters')
        archive = validated_data.pop('archive')
        validated_data['workspace'] = playbook_jobs_models.Playbook.generate_workspace_path()

        zip_file = ZipFile(archive)
        zip_file.extractall(validated_data['workspace'])
        zip_file.close()

        playbook = playbook_jobs_models.Playbook.objects.create(**validated_data)
        for parameter_data in parameters_data:
            playbook_jobs_models.PlaybookParameter.objects.create(playbook=playbook, **parameter_data)

        return playbook


class JobSerializer(common_serializers.BaseApplicationSerializer,
                    structure_serializers.PermissionFieldFilteringMixin):
    service_project_link = serializers.HyperlinkedRelatedField(
        lookup_field='pk',
        view_name='openstacktenant-spl-detail',
        queryset=openstack_models.OpenStackTenantServiceProjectLink.objects.all(),
    )
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        lookup_field='uuid',
        view_name='openstacktenant-detail',
        read_only=True,
    )
    service_name = serializers.ReadOnlyField(source='service_project_link.service.settings.name')
    service_uuid = serializers.ReadOnlyField(source='service_project_link.service.uuid')
    ssh_public_key = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='sshpublickey-detail',
        queryset=core_models.SshPublicKey.objects.all(),
        required=True,
    )
    ssh_public_key_name = serializers.ReadOnlyField(source='ssh_public_key.name')
    ssh_public_key_uuid = serializers.ReadOnlyField(source='ssh_public_key.uuid')
    project = serializers.HyperlinkedRelatedField(
        source='service_project_link.project',
        lookup_field='uuid',
        view_name='project-detail',
        read_only=True,
    )
    project_name = serializers.ReadOnlyField(source='service_project_link.project.name')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')
    playbook = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name=core_utils.get_detail_view_name(playbook_jobs_models.Playbook),
        queryset=playbook_jobs_models.Playbook.objects.all(),
    )
    playbook_name = serializers.ReadOnlyField(source='playbook.name')
    playbook_uuid = serializers.ReadOnlyField(source='playbook.uuid')
    playbook_image = serializers.FileField(source='playbook.image', read_only=True)
    playbook_description = serializers.ReadOnlyField(source='playbook.description')
    arguments = serializers.JSONField(default=dict)
    state = serializers.SerializerMethodField()
    tag = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()

    class Meta(object):
        model = playbook_jobs_models.Job
        fields = ('url', 'uuid', 'name', 'description',
                  'service_project_link', 'service', 'service_name', 'service_uuid',
                  'ssh_public_key', 'ssh_public_key_name', 'ssh_public_key_uuid',
                  'project', 'project_name', 'project_uuid',
                  'playbook', 'playbook_name', 'playbook_uuid',
                  'playbook_image', 'playbook_description',
                  'arguments', 'state', 'output', 'created', 'modified', 'tag', 'type')
        read_only_fields = ('output', 'created', 'modified', 'type')
        protected_fields = ('service_project_link', 'ssh_public_key', 'playbook', 'arguments')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_type(self, obj):
        return 'playbook_job'

    def get_filtered_field_names(self):
        return 'project', 'service_project_link', 'ssh_public_key'

    def get_state(self, obj):
        return obj.get_state_display()

    def get_tag(self, obj):
        return obj.get_tag()

    def check_project(self, attrs):
        if self.instance:
            project = self.instance.service_project_link.project
        else:
            project = attrs['service_project_link'].project
        if not structure._has_admin_access(self.context['request'].user, project):
            raise exceptions.PermissionDenied()

    def check_arguments(self, attrs):
        playbook = self.instance.playbook if self.instance else attrs['playbook']
        arguments = attrs['arguments']
        parameter_names = playbook.parameters.all().values_list('name', flat=True)
        for argument in arguments.keys():
            if argument not in parameter_names and argument != 'project_uuid':
                raise serializers.ValidationError(_('Argument %s is not listed in playbook parameters.' % argument))

        if playbook.parameters.exclude(name__in=arguments.keys()).filter(required=True, default__exact='').exists():
            raise serializers.ValidationError(_('Not all required playbook parameters were specified.'))

        unfilled_parameters = playbook.parameters.exclude(name__in=arguments.keys())
        for parameter in unfilled_parameters:
            if parameter.default:
                arguments[parameter.name] = parameter.default

    def check_subnet(self, attrs):
        if not self.instance:
            settings = attrs['service_project_link'].service.settings
            if not openstack_models.SubNet.objects.filter(settings=settings).exists():
                raise serializers.ValidationError(_('Selected OpenStack provider does not have any subnet yet.'))
            else:
                attrs['subnet'] = openstack_models.SubNet.objects.filter(settings=settings).first()

    def validate(self, attrs):
        if not self.instance:
            attrs['user'] = self.context['request'].user

        self.check_project(attrs)
        self.check_arguments(attrs)
        self.check_subnet(attrs)
        return attrs
