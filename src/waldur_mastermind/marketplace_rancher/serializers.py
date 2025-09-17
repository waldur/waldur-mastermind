from rest_framework import serializers

from waldur_rancher import models as rancher_models
from waldur_rancher.serializers import RancherCreateNodeSerializer


class ManagedClusterCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique identifier for the cluster")
    worker_nodes_count = serializers.IntegerField()
    worker_nodes_flavor_name = serializers.CharField()
    worker_nodes_data_volume_size = serializers.IntegerField(
        help_text="Data volume size for worker nodes in MB (consistent with OpenStack)"
    )
    worker_nodes_data_volume_type_name = serializers.CharField(required=False)
    openstack_offering_uuid_list = serializers.ListSerializer(
        child=serializers.UUIDField(),
        required=False,
        help_text="List of UUID of OpenStack offerings where tenant can be created",
    )
    install_longhorn = serializers.BooleanField(
        default=False,
        help_text="Longhorn is a distributed block storage deployed on top of Kubernetes cluster",
    )
    worker_nodes_longhorn_volume_size = serializers.IntegerField(
        required=False,
        help_text="Longhorn storage volume size for worker nodes in MB (consistent with OpenStack)",
    )
    worker_nodes_longhorn_volume_type_name = serializers.CharField(required=False)


class ManagedRancherCreateNodeSerializer(RancherCreateNodeSerializer):
    class Meta:
        model = rancher_models.Node
        fields = (
            "role",
            "system_volume_size",
            "system_volume_type",
            "memory",
            "cpu",
            "subnet",
            "flavor",
            "data_volumes",
            "ssh_public_key",
            "tenant",
            "uuid",
        )

    def validate(self, attrs):
        # The value of autoexpand_tenant is hardcoded for now:
        # True for the Managed Rancher plugin
        # False for the regular Rancher plugin
        attrs["autoexpand_tenant"] = True
        attrs["cluster"] = self.context["cluster"]
        attrs = super().validate(attrs)
        return attrs
