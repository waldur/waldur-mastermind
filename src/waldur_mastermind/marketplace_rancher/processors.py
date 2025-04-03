from rest_framework import serializers as rf_serializers
from rest_framework.reverse import reverse

from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
    TENANT_TYPE,
)
from waldur_mastermind.marketplace_rancher.utils import (
    submit_creation_order,
    submit_termination_order,
    wait_for_order,
)
from waldur_openstack import models as os_models
from waldur_openstack.utils import volume_type_name_to_quota_name
from waldur_rancher import models as rancher_models
from waldur_rancher import views as rancher_views

from . import serializers


class RancherCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = rancher_views.ClusterViewSet
    fields = (
        "name",
        "description",
        "nodes",
        "tenant",
        "ssh_public_key",
        "install_longhorn",
        "security_groups",
    )


class RancherDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = rancher_views.ClusterViewSet


class ManagedRancherCreateProcessor(processors.AbstractCreateResourceProcessor):
    def send_request(self, user) -> rancher_models.Cluster:
        serializer = serializers.ClusterCreateSerializer(data=self.order.attributes)
        serializer.is_valid(raise_exception=True)

        project = self.create_project()
        tenants = self.create_tenants(user, project)
        return self.create_cluster(user, project, tenants)

    def create_project(self) -> Project:
        """
        For each cluster dedicated project is created to limit what
        operations users would be able to perform on underlying nodes.
        """
        offering: Offering = self.order.offering
        provider_customer = Customer.objects.get(
            uuid=offering.secret_options["customer_uuid"]
        )
        consumer_project: Project = self.order.project
        consumer_customer: Customer = consumer_project.customer
        project_name = " / ".join(
            [
                consumer_customer.abbreviation or consumer_customer.name,
                consumer_project.name,
                self.order.attributes["name"],
            ]
        )
        return Project.objects.create(
            customer=provider_customer,
            name=project_name,
            description="Automatically created project for Rancher cluster",
        )

    def create_tenants(self, user, project: Project) -> list[os_models.Tenant]:
        orders = []
        plan_name = self.order.attribute["openstack_plan_name"]
        for offering_uuid in self.order.attributes["openstack_offering_uuid_list"]:
            offering = Offering.objects.get(uuid=offering_uuid)
            plan = Plan.objects.get(offering=offering, name=plan_name)
            attributes = {
                "name": f"os-tenant-{project.slug}-{offering.slug}",
            }
            limits = self.get_tenant_limits(self.order.attributes["nodes"], offering)
            orders.append(
                submit_creation_order(user, offering, plan, project, attributes, limits)
            )

        return [order.resource.scope for order in Order.objects.filter(uuid__in=orders)]

    def create_cluster(
        self, user, project: Project, tenants: list[os_models.Tenant]
    ) -> rancher_models.Cluster:
        rancher_offering_uuid = self.order.offering.secret_options[
            "rancher_offering_uuid"
        ]
        rancher_offering = Offering.objects.get(uuid=rancher_offering_uuid)
        plan = Plan.objects.get(
            offering=rancher_offering,
            name=self.order.offering.attributes["rancher_plan_name"],
        )

        nodes = []
        tenant = tenants[0]  # TODO: Support multiple tenants

        for node_index, node_spec in enumerate(self.order.attributes["nodes"]):
            system_volume_type = os_models.VolumeType.objects.get(
                settings=tenant.service_settings,
                name=node_spec["system_volume_type_name"],
            )
            flavor = os_models.Flavor.objects.get(
                settings=tenant.service_settings,
                name=node_spec["flavor_name"],
            )
            node = {
                "roles": node_spec["roles"],
                "system_volume_size": node_spec["system_volume_size_gb"] * 1024,
                "system_volume_type": reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": system_volume_type},
                ),
                "memory": flavor.ram,
                "cpu": flavor.cores,
                "flavor": reverse("openstack-flavor-detail", kwargs={"uuid": flavor}),
            }
            nodes.append(node)

        attributes = {
            "name": "k8s-cluster",
            "nodes": nodes,
            "tenant": [tenant.uuid.hex for tenant in tenants],
            "install_longhorn": self.order.attributes["install_longhorn"],
        }

        order_uuid = submit_creation_order(
            user, rancher_offering, plan, project, attributes
        )
        wait_for_order(order_uuid)
        return Order.objects.get(uuid=order_uuid).resource.scope

    def validate_order(self, request):
        available_service_settings = self.validate_openstack_offerings()
        self.validate_flavors(available_service_settings)
        self.validate_volume_types(available_service_settings)

    def validate_flavors(self, available_service_settings: list[int]):
        requested_flavor_names = set(
            node["flavor_name"] for node in self.order.attributes["nodes"]
        )

        for service_setting in available_service_settings:
            available_flavor_names = set(
                os_models.Flavor.objects.filter(
                    settings_id=service_setting
                ).values_list("name", flat=True)
            )
            unavailable_flavor_names = requested_flavor_names - available_flavor_names
            if unavailable_flavor_names:
                raise rf_serializers.ValidationError(
                    "These flavors are not available in OpenStack offering '{}': {}".format(
                        service_setting.uuid, ", ".join(unavailable_flavor_names)
                    )
                )

    def validate_volume_types(self, available_service_settings: list[int]):
        requested_volume_types = set(
            node.get("volume_type")
            for node in self.order.attributes["nodes"]
            if node.get("volume_type")
        )

        for service_setting in available_service_settings:
            available_volume_types = set(
                os_models.VolumeType.objects.filter(settings_id=service_setting)
                .values_list("name", flat=True)
                .distinct()
            )
            unavailable_volume_types = requested_volume_types - available_volume_types
            if unavailable_volume_types:
                raise rf_serializers.ValidationError(
                    "These volume types are not available in OpenStack offering '{}': {}".format(
                        service_setting.uuid, ", ".join(unavailable_volume_types)
                    )
                )

    def validate_openstack_offerings(self):
        available_offerings = set(
            self.order.offering.plugin_options["openstack_offering_uuid_list"]
        )
        requested_offerings = set(self.order.attributes["openstack_offering_uuid_list"])
        unavailable_offerings = requested_offerings - available_offerings
        if unavailable_offerings:
            raise rf_serializers.ValidationError(
                "These OpenStack offerings are not available: {}".format(
                    ", ".join(unavailable_offerings)
                )
            )

        if not requested_offerings:
            raise rf_serializers.ValidationError(
                "Please select at least one OpenStack offering."
            )

        if len(requested_offerings) % 2 == 0:
            raise rf_serializers.ValidationError(
                "Number of selected OpenStack offerings should be odd (1, 3, 5)"
            )

        return list(
            Offering.objects.filter(
                type=TENANT_TYPE,
                uuid__in=self.order.attributes["openstack_offering_uuid_list"],
            ).values_list("object_id", flat=True)
        )

    def get_tenant_limits(
        self,
        nodes,
        offering: Offering,
    ) -> dict[str, int]:
        flavors_map = {
            flavor.name: flavor
            for flavor in os_models.Flavor.objects.filter(
                settings=offering.scope,
                name__in=[node["flavor_name"] for node in nodes],
            )
        }

        limits = {
            CORES_TYPE: sum(flavors_map[node["flavor_name"]].cores for node in nodes),
            RAM_TYPE: sum(flavors_map[node["flavor_name"]].ram for node in nodes),
        }

        storage_mode = offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        if storage_mode == STORAGE_MODE_FIXED:
            total_storage = sum(
                node["system_volume_size_gb"] * 1024
                for node in nodes
                if "volume_type" not in node
            )
            limits[STORAGE_TYPE] = total_storage
        else:
            volume_types = {}
            for node in nodes:
                volume_type = node.get("volume_type")
                if not volume_type:
                    continue
                quota_name = volume_type_name_to_quota_name(volume_type)
                volume_types.setdefault(quota_name, 0)
                volume_types[quota_name] += node["system_volume_size_gb"]
            limits.update(volume_type)
        return limits


class ManagedRancherDeleteProcessor(processors.AbstractDeleteResourceProcessor):
    def send_request(self, user, resource):
        tenant_resource = Resource.objects.filter(scope=resource.scope.tenant)
        submit_termination_order(resource)
        submit_termination_order(tenant_resource)
