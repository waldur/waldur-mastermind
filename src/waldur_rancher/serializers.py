from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import VirtualMachine


from . import models, validators, exceptions


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Rancher server URL'),
        'username': _('Rancher access key'),
        'password': _('Rancher secret key'),
    }

    SERVICE_ACCOUNT_EXTRA_FIELDS = {}

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.RancherService
        required_fields = ('backend_url', 'username', 'password')


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.RancherServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'vmware-detail'},
        }


class ClusterSerializer(structure_serializers.BaseResourceSerializer):
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

    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        required=True,
        write_only=True,
    )

    name = serializers.CharField(max_length=150, validators=[validators.ClusterNameValidator])

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Cluster
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'instance', 'node_command',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'instance',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'node_command',
        )

        extra_kwargs = dict(
            cluster={
                'view_name': 'rancher-cluster-detail',
                'lookup_field': 'uuid',
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def create(self, validated_data):
        instance = validated_data.pop('instance')
        cluster = super(ClusterSerializer, self).create(validated_data)
        models.Node.objects.create(instance=instance, cluster=cluster)
        return cluster

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        instance = attrs.get('instance')

        if models.Node.objects.filter(
                object_id=instance.id,
                content_type=ContentType.objects.get_for_model(instance)
        ).exists():
            raise serializers.ValidationError({'instance': 'The selected instance is already in use.'})

        return super(ClusterSerializer, self).validate(attrs)


class NodeSerializer(serializers.HyperlinkedModelSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        required=True,
    )

    class Meta(object):
        model = models.Node
        fields = ('uuid', 'url', 'created', 'modified', 'cluster', 'instance')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'rancher-node-detail'},
            'cluster': {'lookup_field': 'uuid', 'view_name': 'rancher-cluster-detail'}
        }

    def validate(self, attrs):
        instance = attrs.get('instance')

        if models.Node.objects.filter(
                object_id=instance.id,
                content_type=ContentType.objects.get_for_model(instance)
        ).exists():
            raise serializers.ValidationError({'instance': 'The selected instance is already in use.'})

        return super(NodeSerializer, self).validate(attrs)


class ClusterImportableSerializer(serializers.Serializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='rancher-spl-detail',
        queryset=models.RancherServiceProjectLink.objects.all(),
        write_only=True)

    name = serializers.CharField(read_only=True)
    backend_id = serializers.CharField(source="id", read_only=True)
    kubernetes_version = serializers.CharField(
        source="rancherKubernetesEngineConfig.kubernetesVersion",
        read_only=True)
    created_ts = serializers.IntegerField(read_only=True)
    nodes = serializers.ListField(
        source="appliedSpec.rancherKubernetesEngineConfig.nodes",
        required=False,
        read_only=True)


class ClusterImportSerializer(ClusterImportableSerializer):
    backend_id = serializers.CharField(write_only=True)

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend_id = validated_data['backend_id']

        if models.Cluster.objects.filter(
            service_project_link__service__settings=service_project_link.service.settings,
            backend_id=backend_id
        ).exists():
            raise serializers.ValidationError({'backend_id': _('Cluster has been imported already.')})

        try:
            backend = service_project_link.get_backend()
            cluster = backend.import_cluster(backend_id, service_project_link=service_project_link)
        except exceptions.RancherException:
            raise serializers.ValidationError({
                'backend_id': _("Can't import cluster with ID %s") % validated_data['backend_id']
            })

        return cluster
