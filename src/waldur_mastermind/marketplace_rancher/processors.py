from typing import cast

from rest_framework import serializers as rf_serializers
from rest_framework.reverse import reverse

from waldur_core.structure.models import Customer, Project, ServiceSettings
from waldur_mastermind.marketplace import (
    processors,
)
from waldur_mastermind.marketplace import (
    serializers as marketplace_serializers,
)
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
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

from . import PLUGIN_NAME, serializers


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
        offering = self.order.offering
        provider_customer = Customer.objects.get(
            uuid=offering.secret_options["customer_uuid"]
        )
        consumer_project = self.order.project
        consumer_customer = consumer_project.customer
        project_name = " / ".join(
            [
                consumer_customer.abbreviation or consumer_customer.name,
                consumer_project.name,
                self.order.attributes["name"],
            ]
        )
        return cast(
            Project,
            Project.objects.create(
                customer=provider_customer,
                name=project_name,
                description="Automatically created project for Rancher cluster",
            ),
        )

    def create_tenants(self, user, project: Project) -> list[os_models.Tenant]:
        orders = []
        openstack_offering_uuid_list = cast(
            list[str], self.order.attributes["openstack_offering_uuid_list"]
        )
        for offering_uuid in openstack_offering_uuid_list:
            offering = Offering.objects.get(uuid=offering_uuid)
            plan = Plan.objects.filter(offering=offering).first()
            attributes = {
                "name": f"os-tenant-{project.slug}-{offering.slug}",
            }
            limits = self.get_tenant_limits(offering)
            orders.append(
                submit_creation_order(user, offering, plan, project, attributes, limits)
            )

        return [
            cast(os_models.Tenant, order.resource.scope)
            for order in Order.objects.filter(uuid__in=orders)
        ]

    def create_cluster(
        self,
        user,
        project: Project,
        tenants: list[os_models.Tenant],
    ) -> rancher_models.Cluster:
        offering = self.order.offering
        rancher_offering = cast(Offering, offering.scope)
        if not rancher_offering:
            rancher_offering = Offering.objects.create(
                type=PLUGIN_NAME,
                shared=False,
                billable=False,
                customer=offering.customer,
                name=offering.name,
                description=offering.description,
                plugin_options=offering.plugin_options,
                secret_options=offering.secret_options,
                category=offering.category,
            )
            marketplace_serializers.update_or_create_service_settings_for_offering(
                rancher_offering, offering.secret_options
            )
            offering.scope = rancher_offering
            offering.save()
            settings = cast(ServiceSettings, rancher_offering.scope)
            settings.begin_creating()
            settings.save()
            backend = settings.get_backend()
            backend.sync()
            settings.set_ok()
            settings.save()
            rancher_offering.activate()
            rancher_offering.save()

        # Sync plans from the offering to the rancher_offering
        for plan in offering.plans.all():
            rancher_offering.plans.update_or_create(
                name=plan.name,
                defaults={
                    "backend_id": plan.id,
                },
            )

        plan = Plan.objects.filter(offering=rancher_offering).first()

        nodes = []

        worker_nodes_count = self.order.attributes["worker_nodes_count"]
        server_nodes_count = 3  # TODO: Make it configurable

        os_offering = Offering.objects.get(
            uuid=self.order.attributes["openstack_offering_uuid_list"][0]
        )

        os_service_settings = cast(ServiceSettings, os_offering.scope)

        worker_node_flavor_name = self.order.attributes["worker_nodes_flavor_name"]
        worker_node_flavor = os_models.Flavor.objects.get(
            settings=os_service_settings,
            name=worker_node_flavor_name,
        )

        server_flavor_name = offering.plugin_options[
            "managed_rancher_server_flavor_name"
        ]
        server_node_flavor = os_models.Flavor.objects.get(
            settings=os_service_settings,
            name=server_flavor_name,
        )

        server_system_volume_size_gb = offering.plugin_options[
            "managed_rancher_server_system_volume_size_gb"
        ]
        server_system_volume_type_name = offering.plugin_options.get(
            "managed_rancher_server_system_volume_type_name"
        )

        worker_system_volume_size_gb = self.order.attributes[
            "worker_nodes_data_volume_size"
        ]
        worker_system_volume_type_name = self.order.attributes.get(
            "worker_system_volume_type_name"
        )

        storage_mode = (
            os_offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        )

        server_system_volume_type = os_models.VolumeType.objects.get(
            settings=os_service_settings, name=server_system_volume_type_name
        )
        worker_system_volume_type = os_models.VolumeType.objects.filter(
            settings=os_service_settings, name=worker_system_volume_type_name
        ).first()

        subnet = os_models.SubNet.objects.filter(tenant=tenants[0]).first()
        if not subnet:
            raise rf_serializers.ValidationError(
                f'Subnets for tenant "{tenants[0].name}" not found'
            )

        def format_node(
            flavor: os_models.Flavor,
            volume_size: int,
            volume_type: os_models.VolumeType | None,
            roles: list[str],
        ):
            result = {
                "roles": roles,
                "system_volume_size": volume_size * 1024,
                "memory": flavor.ram,
                "cpu": flavor.cores,
                "flavor": reverse(
                    "openstack-flavor-detail", kwargs={"uuid": flavor.uuid.hex}
                ),
                "subnet": reverse(
                    "openstack-subnet-detail", kwargs={"uuid": subnet.uuid.hex}
                ),
            }
            if storage_mode == STORAGE_MODE_DYNAMIC and volume_type is not None:
                result["system_volume_type"] = reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": volume_type.uuid.hex},
                )
            return result

        nodes = []
        for _ in range(server_nodes_count):
            nodes.append(
                format_node(
                    flavor=server_node_flavor,
                    roles=["etcd", "controlplane"],
                    volume_size=server_system_volume_size_gb,
                    volume_type=server_system_volume_type,
                ),
            )

        for _ in range(worker_nodes_count):
            nodes.append(
                format_node(
                    flavor=worker_node_flavor,
                    roles=["worker"],
                    volume_size=worker_system_volume_size_gb,
                    volume_type=worker_system_volume_type,
                ),
            )

        attributes = {
            "name": "k8s-cluster",
            "nodes": nodes,
            "tenant": reverse(
                "openstack-tenant-detail", kwargs={"uuid": tenants[0].uuid.hex}
            ),
            "install_longhorn": self.order.attributes.get("install_longhorn", False),
        }

        order_uuid = submit_creation_order(
            user, rancher_offering, plan, project, attributes
        )
        wait_for_order(order_uuid)
        return cast(
            rancher_models.Cluster, Order.objects.get(uuid=order_uuid).resource.scope
        )

    def validate_order(self, request):
        available_service_settings = self.validate_openstack_offerings()
        self.validate_flavors(available_service_settings)
        self.validate_volume_types(available_service_settings)

    def validate_flavors(self, available_service_settings: list[int]):
        worker_nodes_flavor_name = self.order.attributes["worker_nodes_flavor_name"]
        server_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_server_flavor_name"
        ]

        for service_setting in available_service_settings:
            for flavor_name in (worker_nodes_flavor_name, server_flavor_name):
                if not os_models.Flavor.objects.filter(
                    settings_id=service_setting, name=flavor_name
                ).exists():
                    raise rf_serializers.ValidationError(
                        f"Flavor is not available in OpenStack offering '{service_setting}': {flavor_name}"
                    )

    def validate_volume_types(self, available_service_settings: list[int]):
        storage_mode = (
            self.order.offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        )
        if storage_mode == STORAGE_MODE_FIXED:
            return

        server_system_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_server_system_volume_type_name"
        )
        worker_system_volume_type_name = self.order.attributes.get(
            "worker_system_volume_type_name"
        )

        for service_setting in available_service_settings:
            for volume_type_name in (
                server_system_volume_type_name,
                worker_system_volume_type_name,
            ):
                if not os_models.VolumeType.objects.filter(
                    settings_id=service_setting, name=volume_type_name
                ).exists():
                    raise rf_serializers.ValidationError(
                        f"Volume type {volume_type_name} is not available in OpenStack offering {service_setting}"
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
        os_offering: Offering,
    ) -> dict[str, int]:
        worker_nodes_count = self.order.attributes["worker_nodes_count"]
        server_nodes_count = 3  # TODO: Make it configurable
        service_settings = cast(ServiceSettings, os_offering.scope)

        worker_node_flavor_name = self.order.attributes["worker_nodes_flavor_name"]
        worker_node_flavor = os_models.Flavor.objects.get(
            settings=service_settings,
            name=worker_node_flavor_name,
        )

        server_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_server_flavor_name"
        ]
        server_node_flavor = os_models.Flavor.objects.get(
            settings=service_settings,
            name=server_flavor_name,
        )

        limits = {
            CORES_TYPE: (
                worker_node_flavor.cores * worker_nodes_count
                + server_node_flavor.cores * server_nodes_count
            ),
            RAM_TYPE: (
                worker_node_flavor.ram * worker_nodes_count
                + server_node_flavor.ram * server_nodes_count
            ),
        }

        server_system_volume_size_gb = self.order.offering.plugin_options[
            "managed_rancher_server_system_volume_size_gb"
        ]
        server_system_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_server_system_volume_type_name"
        )

        worker_system_volume_size_gb = self.order.attributes[
            "worker_nodes_data_volume_size"
        ]
        worker_system_volume_type_name = self.order.attributes.get(
            "worker_system_volume_type_name"
        )

        storage_mode = (
            os_offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        )
        if storage_mode == STORAGE_MODE_FIXED:
            total_storage = (
                server_system_volume_size_gb + worker_system_volume_size_gb
            ) * 1024
            limits[STORAGE_TYPE] = total_storage
        else:
            try:
                os_models.VolumeType.objects.get(
                    settings=service_settings, name=server_system_volume_type_name
                )
            except os_models.VolumeType.DoesNotExist:
                raise rf_serializers.ValidationError(
                    f'Server volume type "{server_system_volume_type_name}" does not exist'
                )

            try:
                os_models.VolumeType.objects.get(
                    settings=service_settings, name=worker_system_volume_type_name
                )
            except os_models.VolumeType.DoesNotExist:
                raise rf_serializers.ValidationError(
                    f'Worker volume type "{worker_system_volume_type_name}" does not exist'
                )
            worker_system_volume_type_quota_name = volume_type_name_to_quota_name(
                worker_system_volume_type_name
            )
            server_system_volume_type_quota_name = volume_type_name_to_quota_name(
                server_system_volume_type_name
            )
            limits.setdefault(worker_system_volume_type_quota_name, 0)
            limits.setdefault(server_system_volume_type_quota_name, 0)
            limits[worker_system_volume_type_quota_name] += (
                worker_nodes_count * worker_system_volume_size_gb * 1024
            )
            limits[server_system_volume_type_quota_name] += (
                server_nodes_count * server_system_volume_size_gb * 1024
            )
        return limits


class ManagedRancherDeleteProcessor(processors.AbstractDeleteResourceProcessor):
    def send_request(self, user, resource: Resource) -> None:
        cluster = cast(rancher_models.Cluster, resource.scope)
        tenant_resource = Resource.objects.get(scope=cluster.tenant)
        submit_termination_order(resource)
        submit_termination_order(tenant_resource)
