from __future__ import unicode_literals

import datetime

from django.core.validators import RegexValidator
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers, exceptions
from waldur_ansible.common import serializers as common_serializers

from waldur_core.core import models as core_models, serializers as core_serializers
from waldur_core.structure import permissions as structure_permissions, serializers as structure_serializers, models as structure_models
from . import models, utils

REQUEST_TYPES_PLAIN_NAMES = {
    models.PythonManagement: 'overall',
    models.PythonManagementInitializeRequest: 'initialization',
    models.PythonManagementSynchronizeRequest: 'synchronization',
    models.PythonManagementFindVirtualEnvsRequest: 'virtual_envs_search',
    models.PythonManagementFindInstalledLibrariesRequest: 'installed_libraries_search',
    models.PythonManagementDeleteRequest: 'python_management_deletion',
    models.PythonManagementDeleteVirtualEnvRequest: 'virtual_environment_deletion',
}

directory_and_library_allowed_pattern = '^[a-zA-Z0-9\-_]+$'


class InstalledPackageSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    name = serializers.RegexField(directory_and_library_allowed_pattern)

    class Meta(object):
        model = models.InstalledLibrary
        fields = ('name', 'version', 'uuid',)
        read_only_fields = ('uuid',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class VirtualEnvironmentSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    name = serializers.RegexField(directory_and_library_allowed_pattern)
    installed_libraries = InstalledPackageSerializer(many=True)

    class Meta(object):
        model = models.VirtualEnvironment
        fields = ('name', 'uuid', 'installed_libraries', 'jupyter_hub_global',)
        read_only_fields = ('uuid',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class PythonManagementRequestMixin(common_serializers.BaseApplicationSerializer):
    request_type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    output = serializers.SerializerMethodField()

    class Meta(object):
        model = NotImplemented
        fields = ('uuid', 'output', 'state', 'created', 'modified', 'request_type',)
        read_only_fields = ('uuid', 'output', 'state', 'created', 'modified', 'request_type',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_output(self, obj):
        if self.context.get('select_output'):
            return obj.output
        else:
            return None

    def get_request_type(self, obj):
        return REQUEST_TYPES_PLAIN_NAMES.get(type(obj))

    def get_state(self, obj):
        return obj.human_readable_state


class PythonManagementInitializeRequestSerializer(PythonManagementRequestMixin):
    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementInitializeRequest


class PythonManagementFindVirtualEnvsRequestSerializer(PythonManagementRequestMixin):
    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementFindVirtualEnvsRequest


class PythonManagementFindInstalledLibrariesRequestSerializer(PythonManagementRequestMixin):
    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementFindInstalledLibrariesRequest
        fields = PythonManagementRequestMixin.Meta.fields + ('virtual_env_name',)


class PythonManagementDeleteRequestSerializer(PythonManagementRequestMixin):
    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementDeleteRequest


class PythonManagementDeleteVirtualEnvRequestSerializer(PythonManagementRequestMixin):
    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementDeleteVirtualEnvRequest
        fields = PythonManagementRequestMixin.Meta.fields + ('virtual_env_name',)


class PythonManagementSynchronizeRequestSerializer(PythonManagementRequestMixin):
    libraries_to_install = serializers.JSONField(default=dict)
    libraries_to_remove = serializers.JSONField(default=dict)

    class Meta(PythonManagementRequestMixin.Meta):
        model = models.PythonManagementSynchronizeRequest
        fields = PythonManagementRequestMixin.Meta.fields \
            + ('libraries_to_install', 'libraries_to_remove', 'virtual_env_name')


class PythonManagementSerializer(
        common_serializers.BaseApplicationSerializer,
        structure_serializers.PermissionFieldFilteringMixin):
    REQUEST_IN_PROGRESS_STATES = (core_models.StateMixin.States.CREATION_SCHEDULED, core_models.StateMixin.States.CREATING)

    state = serializers.SerializerMethodField()
    virtual_environments = VirtualEnvironmentSerializer(many=True)
    virtual_envs_dir_path = serializers.CharField(max_length=255, validators=[
        RegexValidator(
            regex=directory_and_library_allowed_pattern,
            message=_('Virtual environments root directory has invalid format!'),
        ),
    ])
    name = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()

    instance = core_serializers.GenericRelatedField(
        related_models=structure_models.ResourceMixin.get_all_models(), required=False)
    instance_name = serializers.ReadOnlyField(source='instance.name')

    class Meta(object):
        model = models.PythonManagement
        fields = ('url', 'uuid', 'virtual_envs_dir_path', 'system_user',
                  'state', 'created', 'modified', 'virtual_environments', 'python_version', 'name', 'type', 'instance', 'instance_name',)
        read_only_fields = ('request_states', 'created', 'modified', 'python_version', 'type', 'name', 'url',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_name(self, python_management):
        instance_name = python_management.instance.name if python_management.instance else 'removed instance'
        return 'Python Management - %s - %s' % (instance_name, python_management.virtual_envs_dir_path)

    def get_type(self, python_management):
        return 'python_management'

    def get_filtered_field_names(self):
        return 'project'

    def get_state(self, python_management):
        states = []
        initialize_request = utils.execute_safely(
            lambda: models.PythonManagementInitializeRequest.objects.filter(python_management=python_management).latest('id'))
        if initialize_request and self.is_in_progress_or_errored(initialize_request):
            return [self.build_state(initialize_request)]

        states.extend(self.get_request_state(
            utils.execute_safely(lambda: models.PythonManagementDeleteRequest.objects.filter(python_management=python_management).latest('id'))))
        states.extend(
            self.get_request_state(
                utils.execute_safely(
                    lambda: models.PythonManagementDeleteVirtualEnvRequest.objects.filter(python_management=python_management).latest('id'))))
        states.extend(self.build_search_requests_states(python_management))
        states.extend(self.build_states_from_last_group_of_the_request(python_management, models.PythonManagementSynchronizeRequest))

        if not states:
            return core_models.StateMixin(state=core_models.StateMixin.States.OK).human_readable_state
        else:
            creation_scheduled_state = core_models.StateMixin(state=core_models.StateMixin.States.CREATION_SCHEDULED).human_readable_state
            creating_state = core_models.StateMixin(state=core_models.StateMixin.States.CREATING).human_readable_state
            erred_state = core_models.StateMixin(state=core_models.StateMixin.States.ERRED).human_readable_state
            if creating_state in states:
                return creating_state
            elif creation_scheduled_state in states:
                return creation_scheduled_state
            elif erred_state in states:
                return erred_state

    def build_search_requests_states(self, python_management):
        states = []
        states.extend(
            self.get_request_state(
                utils.execute_safely(
                    lambda: models.PythonManagementFindVirtualEnvsRequest.objects
                    .filter(python_management=python_management).latest('id'))))
        states.extend(self.build_states_from_last_group_of_the_request(python_management, models.PythonManagementFindInstalledLibrariesRequest))
        return states

    def get_request_state(self, request):
        if request and self.is_in_progress_or_errored(request):
            return [self.build_state(request)]
        else:
            return []

    def build_states_from_last_group_of_the_request(self, python_management, request_class):
        states = []
        requests = request_class.objects.filter(python_management=python_management).order_by('-id')
        last_request_group = self.get_last_requests_group(requests)
        for request in last_request_group:
            if self.is_in_progress_or_errored(request):
                states.append(self.build_state(request))
        return states

    def get_last_requests_group(self, requests):
        last_request_group = []

        last_request_time = None
        for request in requests:
            if not last_request_time:
                last_request_time = request.created - datetime.timedelta(minutes=1)
            if request.created < last_request_time:
                break
            last_request_group.append(request)

        return last_request_group

    def is_in_progress_or_errored(self, request):
        return request.state in PythonManagementSerializer.REQUEST_IN_PROGRESS_STATES \
            or request.state == core_models.StateMixin.States.ERRED

    def build_state(self, request, state=None):
        request_state = state if state else request
        return request_state.human_readable_state

    @transaction.atomic
    def create(self, validated_data):
        python_management = models.PythonManagement(
            user=validated_data.get('user'),
            virtual_envs_dir_path=validated_data.get('virtual_envs_dir_path'),
            instance=validated_data.get('instance'),
            project=validated_data.get('instance').service_project_link.project,
            python_version='3',
            system_user=validated_data.get('system_user'))
        python_management.save()
        return python_management

    def validate(self, attrs):
        super(PythonManagementSerializer, self).validate(attrs)
        if not self.instance:
            attrs['user'] = self.context['request'].user
            self.check_resource_type(attrs)

        self.check_project_permissions(attrs)
        return attrs

    def check_resource_type(self, attrs):
        if not issubclass(type(attrs['instance']), structure_models.VirtualMachine):
            raise exceptions.ValidationError(_('Please specify a virtual machine, not just any resource.'))

    def check_project_permissions(self, attrs):
        if self.instance:
            project = self.instance.project
        else:
            project = attrs['instance'].service_project_link.project

        if not structure_permissions._has_admin_access(self.context['request'].user, project):
            raise exceptions.PermissionDenied()


class CachedRepositoryPythonLibrarySerializer(
        core_serializers.AugmentedSerializerMixin,
        serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.CachedRepositoryPythonLibrary
        fields = ('name', 'uuid')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
