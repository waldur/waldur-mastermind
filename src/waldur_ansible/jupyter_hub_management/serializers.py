from __future__ import unicode_literals

import datetime
from itertools import chain

from django.core import validators
from django.db import transaction
from passlib.hash import sha512_crypt
from rest_framework import serializers, exceptions
from waldur_ansible.common import serializers as common_serializers
from waldur_ansible.python_management import serializers as python_management_serializers, utils as pythhon_management_utils, models as python_management_models

from waldur_core.core import models as core_models, serializers as core_serializers
from waldur_core.structure import permissions as structure_permissions, serializers as structure_serializers
from . import models, jupyter_hub_management_service

REQUEST_TYPES_PLAIN_NAMES = {
    models.JupyterHubManagement: 'overall',
    models.JupyterHubManagementSyncConfigurationRequest: 'sync_configuration',
    models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: 'globalize_virtual_envs',
    models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: 'localize_virtual_envs',
    models.JupyterHubManagementDeleteRequest: 'delete_jupyter_hub',
}


class JupyterHubUserSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    admin = serializers.BooleanField()
    whitelisted = serializers.BooleanField()
    username = serializers.CharField(max_length=255, validators=[
        validators.RegexValidator(
            regex='^[a-zA-Z0-9_]+$',
            message=b'Username may contain only numbers, characters and underscores!',
        ),
    ])

    class Meta(object):
        model = models.JupyterHubUser
        fields = ('uuid', 'admin', 'whitelisted', 'username', 'password',)
        read_only_fields = ('uuid', 'output', 'state', 'created', 'modified',)
        write_only_fields = ('password',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class JupyterHubManagementRequestMixin(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
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


class JupyterHubManagementSyncConfigurationRequestSerializer(JupyterHubManagementRequestMixin):
    class Meta(JupyterHubManagementRequestMixin.Meta):
        model = models.JupyterHubManagementSyncConfigurationRequest


class JupyterHubManagementDeleteRequestSerializer(JupyterHubManagementRequestMixin):
    class Meta(JupyterHubManagementRequestMixin.Meta):
        model = models.JupyterHubManagementDeleteRequest


class JupyterHubManagementMakeVirtualEnvironmentGlobalRequestSerializer(JupyterHubManagementRequestMixin):
    class Meta(JupyterHubManagementRequestMixin.Meta):
        model = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest
        fields = JupyterHubManagementRequestMixin.Meta.fields + ('virtual_env_name',)


class JupyterHubManagementMakeVirtualEnvironmentLocalRequestSerializer(JupyterHubManagementRequestMixin):
    class Meta(JupyterHubManagementRequestMixin.Meta):
        model = models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest
        fields = JupyterHubManagementRequestMixin.Meta.fields + ('virtual_env_name',)


class JupyterHubOAuthConfigSerializer(core_serializers.AugmentedSerializerMixin,
                                      structure_serializers.PermissionFieldFilteringMixin,
                                      serializers.HyperlinkedModelSerializer):
    type = serializers.ChoiceField(choices=models.JupyterHubOAuthType.CHOICES)

    class Meta(object):
        model = models.JupyterHubOAuthConfig
        fields = ('uuid', 'type', 'oauth_callback_url', 'client_id', 'client_secret', 'tenant_id', 'gitlab_host',)
        read_only_fields = ('uuid',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'type'


class JupyterHubManagementSerializer(
        common_serializers.BaseApplicationSerializer,
        structure_serializers.PermissionFieldFilteringMixin):
    REQUEST_IN_PROGRESS_STATES = (core_models.StateMixin.States.CREATION_SCHEDULED, core_models.StateMixin.States.CREATING)

    python_management = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='python_management-detail',
        queryset=python_management_models.PythonManagement.objects.all(),
    )
    state = serializers.SerializerMethodField()
    jupyter_hub_users = JupyterHubUserSerializer(many=True)
    jupyter_hub_oauth_config = JupyterHubOAuthConfigSerializer(allow_null=True)
    updated_virtual_environments = python_management_serializers.VirtualEnvironmentSerializer(many=True, write_only=True)
    name = serializers.SerializerMethodField()
    jupyter_hub_url = serializers.SerializerMethodField()

    class Meta(object):
        model = models.JupyterHubManagement
        fields = ('uuid', 'python_management', 'jupyter_hub_users', 'state', 'session_time_to_live_hours',
                  'created', 'modified', 'updated_virtual_environments', 'name', 'jupyter_hub_url', 'jupyter_hub_oauth_config',)
        read_only_fields = ('request_states', 'created', 'modified', 'jupyter_hub_url',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'project'

    def get_jupyter_hub_url(self, jupyter_hub_management):
        instance_floating_ips = jupyter_hub_management.instance.floating_ips if jupyter_hub_management.instance else None
        return instance_floating_ips[0].address if instance_floating_ips else None

    def get_name(self, jupyter_hub_management):
        instance_name = jupyter_hub_management.instance.name if jupyter_hub_management.instance else 'removed instance'
        return 'JupyterHub - %s - %s' % (jupyter_hub_management.python_management.virtual_envs_dir_path, instance_name)

    def get_state(self, jupyter_hub_management):
        states = []
        configuration_request = pythhon_management_utils.execute_safely(
            lambda: models.JupyterHubManagementSyncConfigurationRequest.objects.filter(jupyter_hub_management=jupyter_hub_management).latest('id'))
        if configuration_request and self.is_in_progress_or_errored(configuration_request):
            return [self.build_state(configuration_request)]

        states.extend(self.get_request_state(
            pythhon_management_utils.execute_safely(
                lambda: models.JupyterHubManagementDeleteRequest.objects.filter(jupyter_hub_management=jupyter_hub_management).latest('id'))))
        states.extend(self.build_states_from_last_group_of_the_request(jupyter_hub_management))

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

    def build_states_from_last_group_of_the_request(self, jupyter_hub_management):
        states = []
        global_requests = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest.objects.filter(jupyter_hub_management=jupyter_hub_management).order_by('-pk')
        local_requests = models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest.objects.filter(jupyter_hub_management=jupyter_hub_management).order_by('-pk')
        merged_requests = list(chain(global_requests, local_requests))
        merged_requests.sort(key=lambda r: r.pk, reverse=True)
        last_request_group = self.get_last_requests_group(merged_requests)
        for request in last_request_group:
            if self.is_in_progress_or_errored(request):
                states.append(self.build_state(request))
        return states

    def get_request_state(self, request):
        if request and self.is_in_progress_or_errored(request):
            return [self.build_state(request)]
        else:
            return []

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
        return request.state in JupyterHubManagementSerializer.REQUEST_IN_PROGRESS_STATES \
            or request.state == core_models.StateMixin.States.ERRED

    def build_state(self, request, state=None):
        request_with_state = state if state else request
        return request_with_state.human_readable_state

    @transaction.atomic
    def create(self, validated_data):
        oauth_config = validated_data.get('jupyter_hub_oauth_config')
        persisted_oauth_config = None
        if not oauth_config:
            for jupyter_hub_user in validated_data.get('jupyter_hub_users'):
                jupyter_hub_user['password'] = sha512_crypt.hash(jupyter_hub_user['password'])
        else:
            for jupyter_hub_user in validated_data.get('jupyter_hub_users'):
                # NB! Should be consistent with JupyterHub configuration located in jupyterhub_config.py.j2
                self.normalize_username(jupyter_hub_user)
            persisted_oauth_config = models.JupyterHubOAuthConfig(
                type=oauth_config['type'],
                oauth_callback_url=oauth_config['oauth_callback_url'],
                client_id=oauth_config['client_id'],
                client_secret=oauth_config['client_secret'],
                tenant_id=oauth_config['tenant_id'] if oauth_config['type'] == models.JupyterHubOAuthType.AZURE else None,
                gitlab_host=oauth_config['gitlab_host'] if oauth_config['type'] == models.JupyterHubOAuthType.GITLAB else None)
            persisted_oauth_config.save()
        jupyter_hub_management = models.JupyterHubManagement(
            user=validated_data.get('user'),
            python_management=validated_data.get('python_management'),
            session_time_to_live_hours=validated_data.get('session_time_to_live_hours'),
            instance=validated_data.get('python_management').instance,
            project=validated_data.get('python_management').instance.service_project_link.project,
            jupyter_hub_oauth_config=persisted_oauth_config)
        jupyter_hub_management.save()

        for user in validated_data.get('jupyter_hub_users'):
            models.JupyterHubUser.objects.create(
                username=user['username'].lower(),
                password=user.get('password', None),
                admin=user['admin'],
                whitelisted=user['whitelisted'],
                jupyter_hub_management=jupyter_hub_management)

        return jupyter_hub_management

    def update(self, instance, validated_data):
        instance.session_time_to_live_hours = validated_data.get('session_time_to_live_hours')
        jupyter_hub_users = validated_data.get('jupyter_hub_users')

        persisted_jupyter_hub_users = instance.jupyter_hub_users.all()
        removed_jupyter_hub_users = jupyter_hub_management_service.JupyterHubManagementService().find_removed_users(persisted_jupyter_hub_users, jupyter_hub_users)
        for removed_jupyter_hub_user in removed_jupyter_hub_users:
            removed_jupyter_hub_user.delete()

        for jupyter_hub_user in jupyter_hub_users:
            self.normalize_username(jupyter_hub_user)
            corresponding_persisted_user = self.find_corresponding_persisted_jupyter_hub_user(persisted_jupyter_hub_users, jupyter_hub_user['username'])
            if corresponding_persisted_user:
                corresponding_user = corresponding_persisted_user[0]
                corresponding_user.username = jupyter_hub_user['username']
                corresponding_user.whitelisted = jupyter_hub_user['whitelisted']
                if jupyter_hub_user['password']:
                    corresponding_user.password = sha512_crypt.hash(jupyter_hub_user['password'])
                corresponding_user.admin = jupyter_hub_user['admin']
                corresponding_user.save()
            else:
                new_jupyter_hub_user = models.JupyterHubUser(
                    jupyter_hub_management=instance,
                    username=jupyter_hub_user['username'],
                    password=sha512_crypt.hash(jupyter_hub_user['password']) if not instance.jupyter_hub_oauth_config else None,
                    admin=jupyter_hub_user['admin'],
                    whitelisted=jupyter_hub_user['whitelisted'])
                new_jupyter_hub_user.save()

        oauth_config = validated_data.get('jupyter_hub_oauth_config')
        if oauth_config:
            instance.jupyter_hub_oauth_config.type = oauth_config['type']
            instance.jupyter_hub_oauth_config.oauth_callback_url = oauth_config['oauth_callback_url']
            instance.jupyter_hub_oauth_config.client_id = oauth_config['client_id']
            instance.jupyter_hub_oauth_config.client_secret = oauth_config['client_secret']
            instance.jupyter_hub_oauth_config.tenant_id = oauth_config['tenant_id'] if oauth_config['type'] == models.JupyterHubOAuthType.AZURE else None
            instance.jupyter_hub_oauth_config.gitlab_host = oauth_config['gitlab_host'] if oauth_config['type'] == models.JupyterHubOAuthType.GITLAB else None
            instance.jupyter_hub_oauth_config.save()

        instance.save()

        return instance

    def normalize_username(self, jupyter_hub_user):
        jupyter_hub_user['username'] = ''.join(c if c.isalnum() else '_' for c in jupyter_hub_user['username'].lower())

    def find_corresponding_persisted_jupyter_hub_user(self, persisted_jupyter_hub_users, username):
        return filter(lambda u: u.username == username.lower(), persisted_jupyter_hub_users)

    def validate(self, attrs):
        super(JupyterHubManagementSerializer, self).validate(attrs)
        if not self.instance:
            attrs['user'] = self.context['request'].user

        self.check_project_permissions(attrs)
        return attrs

    def check_project_permissions(self, attrs):
        if self.instance:
            project = self.instance.project
        else:
            project = attrs['python_management'].project

        if not structure_permissions._has_admin_access(self.context['request'].user, project):
            raise exceptions.PermissionDenied()


class SummaryJupyterHubManagementRequestsSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        if model is models.JupyterHubManagementSyncConfigurationRequest:
            return JupyterHubManagementSyncConfigurationRequestSerializer
        elif model is models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest:
            return JupyterHubManagementMakeVirtualEnvironmentGlobalRequestSerializer
        elif model is models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest:
            return JupyterHubManagementMakeVirtualEnvironmentLocalRequestSerializer
        elif model is models.JupyterHubManagementDeleteRequest:
            return JupyterHubManagementDeleteRequestSerializer
