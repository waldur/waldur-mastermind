from rest_framework import serializers

from waldur_rancher.serializers import ROLE_CHOICES


class ClusterNodeSerializer(serializers.Serializer):
    flavor_name = serializers.CharField()
    system_volume_size_gb = serializers.IntegerField()
    system_volume_type_name = serializers.CharField(required=False)
    roles = serializers.MultipleChoiceField(choices=ROLE_CHOICES)


class ClusterCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique identifier for the cluster")
    nodes = ClusterNodeSerializer(many=True)
    rancher_plan_name = serializers.CharField()
    openstack_plan_name = serializers.CharField()
    openstack_offering_uuid_list = serializers.ListSerializer(
        child=serializers.UUIDField(),
        required=False,
        help_text="List of UUID of OpenStack offerings where tenant can be created",
    )
    install_longhorn = serializers.BooleanField(
        default=False,
        help_text="Longhorn is a distributed block storage deployed on top of Kubernetes cluster",
    )
