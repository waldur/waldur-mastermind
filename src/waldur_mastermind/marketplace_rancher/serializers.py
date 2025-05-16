from rest_framework import serializers


class ClusterCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique identifier for the cluster")
    worker_nodes_count = serializers.IntegerField()
    worker_nodes_flavor_name = serializers.CharField()
    worker_nodes_data_volume_size = serializers.IntegerField()
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
    worker_nodes_longhorn_volume_size = serializers.IntegerField(required=False)
    worker_nodes_longhorn_volume_type_name = serializers.CharField(required=False)
