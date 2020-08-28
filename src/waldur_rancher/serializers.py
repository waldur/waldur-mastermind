from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core import validators as django_validators
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.media.serializers import ProtectedMediaSerializerMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import VirtualMachine
from waldur_openstack.openstack_tenant import apps as openstack_tenant_apps
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant import (
    serializers as openstack_tenant_serializers,
)
from waldur_openstack.openstack_tenant.serializers import (
    _validate_instance_security_groups,
)

from . import exceptions, models, utils, validators


class ServiceSerializer(
    core_serializers.ExtraFieldOptionsMixin, structure_serializers.BaseServiceSerializer
):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Rancher server URL'),
        'username': _('Rancher access key'),
        'password': _('Rancher secret key'),
    }

    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'base_image_name': _('Base image name'),
        'cloud_init_template': _('Cloud init template'),
        'default_mtu': _('Default MTU of a cluster'),
        'private_registry_url': _('URL of a private registry for a cluster'),
        'private_registry_user': _('Username for accessing a private registry'),
        'private_registry_password': _('Password for accessing a private registry'),
        'allocate_floating_ip_to_all_nodes': _(
            'If True, on provisioning a floating IP will be allocated to each of the nodes'
        ),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.RancherService
        required_fields = ('backend_url', 'username', 'password', 'base_image_name')


class ServiceProjectLinkSerializer(
    structure_serializers.BaseServiceProjectLinkSerializer
):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.RancherServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'vmware-detail'},
        }


class DataVolumeSerializer(
    structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer
):
    size = serializers.IntegerField()
    volume_type = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-volume-type-detail',
        queryset=openstack_tenant_models.VolumeType.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        required=False,
    )

    def get_fields(self):
        fields = super(DataVolumeSerializer, self).get_fields()
        fields['mount_point'] = serializers.ChoiceField(
            choices=settings.WALDUR_RANCHER['MOUNT_POINT_CHOICES'],
            required=settings.WALDUR_RANCHER['MOUNT_POINT_CHOICE_IS_MANDATORY'],
        )
        return fields

    def get_filtered_field_names(self):
        return ['volume_type']

    def validate(self, attrs):
        size = attrs['size']
        mount_point = attrs.get('mount_point')

        if mount_point:
            min_size = settings.WALDUR_RANCHER['MOUNT_POINT_MIN_SIZE'][mount_point]
            if size < min_size * 1024:
                raise serializers.ValidationError(
                    'Volume %s capacity should be at least %s GB'
                    % (mount_point, min_size)
                )
        return attrs


class BaseNodeSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    serializers.HyperlinkedModelSerializer,
):
    ROLE_CHOICES = ('controlplane', 'etcd', 'worker')
    subnet = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-subnet-detail',
        queryset=openstack_tenant_models.SubNet.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        write_only=True,
    )
    flavor = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-flavor-detail',
        queryset=openstack_tenant_models.Flavor.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        write_only=True,
        required=False,
    )
    system_volume_size = serializers.IntegerField(
        write_only=True,
        required=False,
        validators=[
            django_validators.MinValueValidator(
                lambda: settings.WALDUR_RANCHER['SYSTEM_VOLUME_MIN_SIZE']
            )
        ],
    )
    system_volume_type = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-volume-type-detail',
        queryset=openstack_tenant_models.VolumeType.objects.all(),
        lookup_field='uuid',
        allow_null=True,
        required=False,
        write_only=True,
    )
    data_volumes = DataVolumeSerializer(many=True, write_only=True, required=False)
    memory = serializers.IntegerField(write_only=True, required=False)
    cpu = serializers.IntegerField(write_only=True, required=False)
    roles = serializers.MultipleChoiceField(choices=ROLE_CHOICES, write_only=True)

    class Meta(object):
        model = models.Node
        read_only_fields = (
            'error_message',
            'etcd_role',
            'worker_role',
            'initial_data',
            'runtime_state',
            'k8s_version',
            'docker_version',
            'cpu_allocated',
            'cpu_total',
            'ram_allocated',
            'ram_total',
            'pods_allocated',
            'pods_total',
            'labels',
            'annotations',
        )
        exclude = ('state',)

    def get_filtered_field_names(self):
        return ('subnet', 'flavor', 'system_volume_type')

    def get_fields(self):
        fields = super(BaseNodeSerializer, self).get_fields()
        if (
            settings.WALDUR_RANCHER['DISABLE_DATA_VOLUME_CREATION']
            and 'data_volumes' in fields
        ):
            del fields['data_volumes']
        return fields


class NestedNodeSerializer(BaseNodeSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(), read_only=True
    )

    class Meta(BaseNodeSerializer.Meta):
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-node-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
        }
        exclude = ('cluster', 'object_id', 'content_type', 'name')


class NestedSecurityGroupSerializer(
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    class Meta:
        model = openstack_tenant_models.SecurityGroup
        fields = ('url',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'openstacktenant-sgp-detail'}
        }


class ClusterSerializer(
    structure_serializers.SshPublicKeySerializerMixin,
    structure_serializers.BaseResourceSerializer,
):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='rancher-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    tenant_settings = serializers.HyperlinkedRelatedField(
        queryset=structure_models.ServiceSettings.objects.filter(
            type=openstack_tenant_apps.OpenStackTenantConfig.service_name
        ),
        view_name='servicesettings-detail',
        lookup_field='uuid',
    )

    name = serializers.CharField(
        max_length=150, validators=[validators.ClusterNameValidator]
    )
    nodes = NestedNodeSerializer(many=True, source='node_set')

    install_longhorn = serializers.BooleanField(
        default=False,
        help_text=_(
            "Longhorn is a distributed block storage deployed on top of Kubernetes cluster"
        ),
    )

    security_groups = NestedSecurityGroupSerializer(
        queryset=openstack_tenant_models.SecurityGroup.objects.all(),
        many=True,
        required=False,
        write_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Cluster
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'node_command',
            'nodes',
            'tenant_settings',
            'runtime_state',
            'ssh_public_key',
            'install_longhorn',
            'security_groups',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ('node_command', 'runtime_state',)
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ('nodes', 'tenant_settings',)
        )
        extra_kwargs = dict(
            cluster={'view_name': 'rancher-cluster-detail', 'lookup_field': 'uuid',},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def get_fields(self):
        fields = super(ClusterSerializer, self).get_fields()
        if (
            settings.WALDUR_RANCHER['DISABLE_SSH_KEY_INJECTION']
            and 'ssh_public_key' in fields
        ):
            del fields['ssh_public_key']
        return fields

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        nodes = attrs['node_set']
        name = attrs['name']
        spl = attrs['service_project_link']
        ssh_public_key = attrs.pop('ssh_public_key', None)

        attrs['settings'] = spl.service.settings

        clusters = models.Cluster.objects.filter(
            settings=spl.service.settings, name=name
        )
        if self.instance:
            clusters = clusters.exclude(id=self.instance.id)
        if clusters.exists():
            raise serializers.ValidationError(_('Name is not unique.'))

        tenant_settings = attrs.get('tenant_settings')
        security_groups = attrs.pop('security_groups', [])
        if tenant_settings and security_groups:
            _validate_instance_security_groups(security_groups, tenant_settings)
        utils.expand_added_nodes(
            name, nodes, spl, tenant_settings, ssh_public_key, security_groups
        )
        return super(ClusterSerializer, self).validate(attrs)

    def validate_nodes(self, nodes):
        if len([node for node in nodes if 'etcd' in node['roles']]) not in [1, 3, 5]:
            raise serializers.ValidationError(
                _('Total count of etcd nodes must be 1, 3 or 5. You have got %s nodes.')
                % len(nodes)
            )

        if not len([node for node in nodes if 'worker' in node['roles']]):
            raise serializers.ValidationError(
                _('Count of workers roles must be min 1.')
            )

        if not len([node for node in nodes if 'controlplane' in node['roles']]):
            raise serializers.ValidationError(
                _('Count of controlplane nodes must be min 1.')
            )

        return nodes


class NodeSerializer(serializers.HyperlinkedModelSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(), required=True,
    )
    resource_type = serializers.SerializerMethodField()
    state = serializers.ReadOnlyField(source='get_state_display')
    service_name = serializers.ReadOnlyField(
        source='service_project_link.service.settings.name'
    )
    service_uuid = serializers.ReadOnlyField(source='service_project_link.service.uuid')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')
    cluster_name = serializers.ReadOnlyField(source='cluster.name')
    cluster_uuid = serializers.ReadOnlyField(source='cluster.uuid')
    instance_name = serializers.ReadOnlyField(source='instance.name')
    instance_uuid = serializers.ReadOnlyField(source='instance.uuid')

    class Meta:
        model = models.Node
        fields = (
            'uuid',
            'url',
            'created',
            'modified',
            'name',
            'backend_id',
            'project_uuid',
            'service_name',
            'service_uuid',
            'resource_type',
            'state',
            'cluster',
            'cluster_name',
            'cluster_uuid',
            'instance',
            'instance_name',
            'instance_uuid',
            'controlplane_role',
            'etcd_role',
            'worker_role',
            'get_node_command',
            'k8s_version',
            'docker_version',
            'cpu_allocated',
            'cpu_total',
            'ram_allocated',
            'ram_total',
            'pods_allocated',
            'pods_total',
            'labels',
            'annotations',
            'runtime_state',
        )
        read_only_fields = (
            'backend_id',
            'k8s_version',
            'docker_version',
            'cpu_allocated',
            'cpu_total',
            'ram_allocated',
            'ram_total',
            'pods_allocated',
            'pods_total',
            'labels',
            'annotations',
            'runtime_state',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-node-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
        }

    def validate(self, attrs):
        instance = attrs.get('instance')

        if models.Node.objects.filter(
            object_id=instance.id,
            content_type=ContentType.objects.get_for_model(instance),
        ).exists():
            raise serializers.ValidationError(
                {'instance': 'The selected instance is already in use.'}
            )

        attrs['name'] = instance.name

        return super(NodeSerializer, self).validate(attrs)

    def get_resource_type(self, obj):
        return 'Rancher.Node'


class CreateNodeSerializer(
    structure_serializers.SshPublicKeySerializerMixin, BaseNodeSerializer
):
    class Meta:
        model = models.Node
        fields = (
            'cluster',
            'roles',
            'system_volume_size',
            'system_volume_type',
            'memory',
            'cpu',
            'subnet',
            'flavor',
            'data_volumes',
            'ssh_public_key',
        )
        extra_kwargs = {
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'}
        }

    def validate(self, attrs):
        attrs = super(CreateNodeSerializer, self).validate(attrs)
        cluster = attrs['cluster']
        ssh_public_key = attrs.pop('ssh_public_key', None)
        spl = cluster.service_project_link
        node = attrs
        utils.expand_added_nodes(
            cluster.name, [node], spl, cluster.tenant_settings, ssh_public_key
        )
        return attrs


class ClusterImportableSerializer(serializers.Serializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        write_only=True,
    )

    name = serializers.CharField(read_only=True)
    backend_id = serializers.CharField(source="id", read_only=True)
    type = serializers.SerializerMethodField()
    extra = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    def get_type(self, cluster):
        return 'Rancher.Cluster'

    def get_extra(self, cluster):
        spec = cluster.get('appliedSpec', {})
        config = spec.get('rancherKubernetesEngineConfig', {})
        backend_nodes = config.get('nodes', [])
        return [
            {'name': 'Number of nodes', 'value': len(backend_nodes),},
            {'name': 'Created at', 'value': cluster.get('created'),},
        ]

    def get_is_active(self, cluster):
        return cluster.get('state', '') == models.Cluster.RuntimeStates.ACTIVE


class ClusterImportSerializer(ClusterImportableSerializer):
    backend_id = serializers.CharField(write_only=True)

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Cluster.objects.filter(
            settings=service_project_link.service.settings, backend_id=backend_id
        ).exists():
            raise serializers.ValidationError(
                {'backend_id': _('Cluster has been imported already.')}
            )

        try:
            backend = service_project_link.get_backend()
            cluster = backend.import_cluster(
                backend_id, service_project_link=service_project_link
            )
        except exceptions.RancherException as e:
            raise serializers.ValidationError(
                {
                    'backend_id': _("Can't import cluster with ID %s. %s")
                    % (validated_data['backend_id'], e)
                }
            )

        return cluster


class LinkOpenstackSerializer(serializers.Serializer):
    instance = serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-instance-detail',
        queryset=openstack_tenant_models.Instance.objects.all(),
        lookup_field='uuid',
        write_only=True,
    )


class CatalogSerializer(serializers.HyperlinkedModelSerializer):
    scope = core_serializers.GenericRelatedField()

    class Meta:
        model = models.Catalog
        fields = (
            'uuid',
            'url',
            'created',
            'modified',
            'name',
            'description',
            'catalog_url',
            'branch',
            'commit',
            'runtime_state',
            'scope',
            'scope_type',
        )
        read_only_fields = ('runtime_state', 'commit')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-catalog-detail'},
        }


class CatalogCreateSerializer(CatalogSerializer):
    class Meta(CatalogSerializer.Meta):
        fields = CatalogSerializer.Meta.fields + ('username', 'password')


class CatalogUpdateSerializer(CatalogCreateSerializer):
    class Meta(CatalogSerializer.Meta):
        read_only_fields = CatalogSerializer.Meta.read_only_fields + ('scope',)


class NestedNamespaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Namespace
        fields = (
            'url',
            'uuid',
            'name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-namespace-detail'},
        }


class ProjectSerializer(structure_serializers.BasePropertySerializer):
    namespaces = NestedNamespaceSerializer(many=True)

    class Meta:
        model = models.Project
        view_name = 'rancher-project-detail'
        fields = (
            'url',
            'uuid',
            'name',
            'description',
            'created',
            'modified',
            'runtime_state',
            'cluster',
            'namespaces',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
        }


class NamespaceSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Namespace
        view_name = 'rancher-namespace-detail'
        fields = (
            'url',
            'uuid',
            'name',
            'created',
            'modified',
            'runtime_state',
            'project',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'project': {'lookup_field': 'uuid', 'view_name': 'rancher-project-detail'},
        }


class TemplateSerializer(
    ProtectedMediaSerializerMixin, structure_serializers.BasePropertySerializer,
):
    catalog_name = serializers.ReadOnlyField(source='catalog.name')

    class Meta:
        model = models.Template
        view_name = 'rancher-template-detail'
        fields = (
            'url',
            'uuid',
            'name',
            'description',
            'created',
            'modified',
            'runtime_state',
            'catalog',
            'cluster',
            'project',
            'icon',
            'project_url',
            'default_version',
            'catalog_name',
            'versions',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'catalog': {'lookup_field': 'uuid', 'view_name': 'rancher-catalog-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'rancher-project-detail'},
        }


class ApplicationSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='rancher-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    version = serializers.CharField()
    namespace_name = serializers.CharField(required=False, write_only=True)
    answers = serializers.DictField(required=False)
    rancher_project_name = serializers.ReadOnlyField(source='rancher_project.name')
    catalog_name = serializers.ReadOnlyField(source='template.catalog.name')
    template_name = serializers.ReadOnlyField(source='template.name')

    class Meta:
        model = models.Application
        view_name = 'rancher-app-detail'
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'runtime_state',
            'template',
            'rancher_project',
            'namespace',
            'namespace_name',
            'version',
            'answers',
            'rancher_project_name',
            'catalog_name',
            'template_name',
            'external_url',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'template': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-template-detail',
            },
            'namespace': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-namespace-detail',
                'required': False,
            },
            'rancher_project': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-project-detail',
            },
        }

    def validate(self, attrs):
        attrs = super(ApplicationSerializer, self).validate(attrs)
        if (not attrs.get('namespace') and not attrs.get('namespace_name')) or (
            attrs.get('namespace') and attrs.get('namespace_name')
        ):
            raise serializers.ValidationError(
                _(
                    'Either existing namespace UUID or new namespace name should be specified.'
                )
            )

        template = attrs['template']
        rancher_project = attrs['rancher_project']
        settings_set = {template.settings, rancher_project.settings}

        namespace = attrs.get('namespace')
        namespace_name = attrs.pop('namespace_name', None)
        if namespace:
            settings_set.add(namespace.settings)

            if namespace.project != rancher_project:
                raise serializers.ValidationError(
                    _('Namespace should belong to the same project.')
                )
        elif namespace_name:
            attrs['namespace'] = models.Namespace.objects.create(
                name=namespace_name,
                settings=rancher_project.settings,
                project=rancher_project,
            )
        else:
            raise serializers.ValidationError(_('Namespace is not specified.'))

        if len(settings_set) > 1:
            raise serializers.ValidationError(
                _(
                    'The same settings should be used for template, project and namespace.'
                )
            )

        return attrs

    def create(self, validated_data):
        rancher_project = validated_data['rancher_project']
        validated_data['settings'] = rancher_project.settings
        validated_data['cluster'] = rancher_project.cluster
        return super(ApplicationSerializer, self).create(validated_data)


class WorkloadSerializer(serializers.HyperlinkedModelSerializer):
    cluster_uuid = serializers.ReadOnlyField(source='cluster.uuid')
    cluster_name = serializers.ReadOnlyField(source='cluster.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    namespace_uuid = serializers.ReadOnlyField(source='namespace.uuid')
    namespace_name = serializers.ReadOnlyField(source='namespace.name')

    class Meta:
        model = models.Workload
        fields = (
            'url',
            'uuid',
            'name',
            'created',
            'modified',
            'runtime_state',
            'cluster',
            'cluster_uuid',
            'cluster_name',
            'project',
            'project_uuid',
            'project_name',
            'namespace',
            'namespace_uuid',
            'namespace_name',
            'scale',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-workload-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'rancher-project-detail'},
            'namespace': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-namespace-detail',
            },
        }


class HPASerializer(serializers.HyperlinkedModelSerializer):
    cluster_uuid = serializers.ReadOnlyField(source='cluster.uuid')
    cluster_name = serializers.ReadOnlyField(source='cluster.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    namespace_uuid = serializers.ReadOnlyField(source='namespace.uuid')
    namespace_name = serializers.ReadOnlyField(source='namespace.name')
    workload_uuid = serializers.ReadOnlyField(source='workload.uuid')
    workload_name = serializers.ReadOnlyField(source='workload.name')

    class Meta:
        model = models.HPA
        fields = (
            'url',
            'uuid',
            'name',
            'description',
            'created',
            'modified',
            'runtime_state',
            'cluster',
            'cluster_uuid',
            'cluster_name',
            'project',
            'project_uuid',
            'project_name',
            'namespace',
            'namespace_uuid',
            'namespace_name',
            'workload',
            'workload_uuid',
            'workload_name',
            'min_replicas',
            'max_replicas',
            'current_replicas',
            'desired_replicas',
            'metrics',
        )
        read_only_fields = (
            'state',
            'runtime_state',
            'current_replicas',
            'desired_replicas',
            'cluster',
            'project',
            'namespace',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-hpa-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'rancher-project-detail'},
            'namespace': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-namespace-detail',
            },
            'workload': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-workload-detail',
            },
        }

    def create(self, validated_data):
        workload = validated_data['workload']
        validated_data['settings'] = workload.settings
        validated_data['cluster'] = workload.cluster
        validated_data['project'] = workload.project
        validated_data['namespace'] = workload.namespace
        return super(HPASerializer, self).create(validated_data)


class ConsoleLogSerializer(serializers.Serializer):
    length = serializers.IntegerField(required=False)


class RancherUserClusterLinkSerializer(serializers.HyperlinkedModelSerializer):
    cluster_name = serializers.ReadOnlyField(source='cluster.name')
    cluster_uuid = serializers.ReadOnlyField(source='cluster.uuid')

    class Meta:
        model = models.RancherUserClusterLink
        fields = ('cluster', 'role', 'cluster_name', 'cluster_uuid')
        extra_kwargs = {
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'},
        }


class RancherUserProjectLinkSerializer(serializers.HyperlinkedModelSerializer):
    project_name = serializers.ReadOnlyField(source='project.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')

    class Meta:
        model = models.RancherUserProjectLink
        fields = ('project', 'role', 'project_name', 'project_uuid')
        extra_kwargs = {
            'project': {'lookup_field': 'uuid', 'view_name': 'rancher-project-detail'},
        }


class RancherUserSerializer(serializers.HyperlinkedModelSerializer):
    cluster_roles = RancherUserClusterLinkSerializer(many=True, read_only=True)

    project_roles = RancherUserProjectLinkSerializer(many=True, read_only=True)

    user_name = serializers.ReadOnlyField(source='user.username')
    full_name = serializers.ReadOnlyField(source='user.full_name')

    def __init__(self, instance=None, *args, **kwargs):
        if instance:
            if isinstance(instance, list):
                request = kwargs.get('context', {}).get('request')
                if request:
                    cluster_uuid = request.GET.get('cluster_uuid')
                    for user in instance:
                        if cluster_uuid:
                            user.cluster_roles = user.rancheruserclusterlink_set.filter(
                                cluster__uuid=cluster_uuid
                            )
                            user.project_roles = user.rancheruserprojectlink_set.filter(
                                project__cluster__uuid=cluster_uuid
                            )
                        else:
                            user.cluster_roles = user.rancheruserclusterlink_set.all()
                            user.project_roles = user.rancheruserprojectlink_set.all()
            else:
                instance.cluster_roles = instance.rancheruserclusterlink_set.all()
                instance.project_roles = instance.rancheruserprojectlink_set.all()

        super().__init__(instance=instance, *args, **kwargs)

    class Meta:
        model = models.RancherUser
        fields = (
            'url',
            'uuid',
            'user',
            'cluster_roles',
            'project_roles',
            'settings',
            'is_active',
            'user_name',
            'full_name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-user-detail'},
            'user': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'settings': {'lookup_field': 'uuid'},
        }


class ClusterTemplateNodeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ClusterTemplateNode
        fields = (
            'min_vcpu',
            'min_ram',
            'system_volume_size',
            'preferred_volume_type',
            'roles',
        )

    roles = serializers.SerializerMethodField()

    def get_roles(self, node):
        roles = []
        if node.controlplane_role:
            roles.append('controlplane')
        if node.etcd_role:
            roles.append('etcd')
        if node.worker_role:
            roles.append('worker')
        return roles


class ClusterTemplateSerializer(serializers.HyperlinkedModelSerializer):
    nodes = ClusterTemplateNodeSerializer(many=True)

    class Meta:
        model = models.ClusterTemplate
        fields = (
            'uuid',
            'name',
            'description',
            'created',
            'modified',
            'nodes',
        )


class IngressSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='rancher-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    rancher_project_name = serializers.ReadOnlyField(source='rancher_project.name')
    namespace_name = serializers.ReadOnlyField(source='namespace.name')

    class Meta:
        model = models.Ingress
        view_name = 'rancher-ingress-detail'
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'runtime_state',
            'rancher_project',
            'rancher_project_name',
            'namespace',
            'namespace_name',
            'rules',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'namespace': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-namespace-detail',
                'required': False,
            },
            'rancher_project': {
                'lookup_field': 'uuid',
                'view_name': 'rancher-project-detail',
            },
        }

    def validate(self, attrs):
        attrs = super(IngressSerializer, self).validate(attrs)
        rancher_project = attrs['rancher_project']
        namespace = attrs['namespace']

        if namespace.project != rancher_project:
            raise serializers.ValidationError(
                _('Namespace should belong to the same project.')
            )

        return attrs

    def create(self, validated_data):
        rancher_project = validated_data['rancher_project']
        validated_data['settings'] = rancher_project.settings
        validated_data['cluster'] = rancher_project.cluster
        return super(IngressSerializer, self).create(validated_data)


class ImportYamlSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    default_namespace = serializers.HyperlinkedRelatedField(
        view_name='rancher-namespace-detail',
        lookup_field='uuid',
        queryset=models.Namespace.objects.all(),
        required=False,
        allow_null=True,
    )
    namespace = serializers.HyperlinkedRelatedField(
        view_name='rancher-namespace-detail',
        lookup_field='uuid',
        queryset=models.Namespace.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        fields = (
            'yaml',
            'default_namespace',
            'namespace',
        )

    def validate(self, attrs):
        cluster = self.context['view'].get_object()
        namespace = attrs.get('namespace')
        default_namespace = attrs.get('default_namespace')

        if namespace and namespace.project.cluster != cluster:
            raise serializers.ValidationError(
                _('Namespace should be related to the same cluster.')
            )

        if default_namespace and default_namespace.project.cluster != cluster:
            raise serializers.ValidationError(
                _('Default namespace should be related to the same cluster.')
            )

        return attrs


def get_rancher_cluster_for_openstack_instance(serializer, scope):
    request = serializer.context['request']
    queryset = filter_queryset_for_user(models.Cluster.objects.all(), request.user)
    try:
        cluster = queryset.filter(
            tenant_settings=scope.service_project_link.service.settings
        ).get()
    except models.Cluster.DoesNotExist:
        return None
    except MultipleObjectsReturned:
        return None
    return {
        'name': cluster.name,
        'uuid': cluster.uuid,
    }


def add_rancher_cluster_to_openstack_instance(sender, fields, **kwargs):
    fields['rancher_cluster'] = serializers.SerializerMethodField()
    setattr(sender, 'get_rancher_cluster', get_rancher_cluster_for_openstack_instance)


core_signals.pre_serializer_fields.connect(
    sender=openstack_tenant_serializers.InstanceSerializer,
    receiver=add_rancher_cluster_to_openstack_instance,
)
