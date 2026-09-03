import collections
from ipaddress import ip_network
from time import sleep
from typing import cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers as rf_serializers
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core.models import User
from waldur_core.core.utils import get_system_robot
from waldur_core.structure.models import Customer, Project, ServiceSettings
from waldur_core.structure.views import ResourceViewSet
from waldur_mastermind.common.utils import create_request
from waldur_mastermind.marketplace import (
    processors,
)
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)
from waldur_mastermind.marketplace_rancher.utils import (
    submit_creation_order,
    submit_termination_order,
    wait_for_tenant,
)
from waldur_openstack import executors as os_executors
from waldur_openstack import models as os_models
from waldur_openstack import views as os_views
from waldur_openstack.utils import volume_type_name_to_quota_name
from waldur_rancher import exceptions
from waldur_rancher import models as rancher_models
from waldur_rancher.enums import AGENT_ROLE, SERVER_ROLE, NodeRoleType
from waldur_rancher.executors import ClusterCreateExecutor, ClusterDeleteExecutor
from waldur_rancher.serializers import RancherClusterCreateSerializer

from . import const, serializers


class RancherCreateProcessor(processors.AbstractCreateResourceProcessor):
    def send_request(self, user):
        deployment_mode = self.order.offering.plugin_options.get(
            "deployment_mode", const.DEPLOYMENT_MODE_SELF_MANAGED
        )
        if deployment_mode == const.DEPLOYMENT_MODE_MANAGED:
            return self._create_managed_cluster(user)
        else:
            return self._create_self_managed_cluster(user)

    def _create_managed_cluster(self, user) -> rancher_models.Cluster:
        serializer = serializers.ManagedClusterCreateSerializer(
            data=self.order.attributes
        )
        serializer.is_valid(raise_exception=True)

        project = self.create_project()
        tenants = self.create_tenants(user, project)
        self.update_subnets(tenants)
        self.create_security_groups(tenants)
        load_balancers = self.create_load_balancers(user, project, tenants)
        cluster = self.create_cluster(user, project, tenants)
        for sg in const.OS_LB_SECURITY_GROUPS:
            rancher_models.ClusterSecurityGroup.objects.create(cluster=cluster, name=sg)

        for load_balancer in load_balancers:
            if load_balancer.floating_ips.count() == 0:
                continue
            rancher_models.ClusterPublicIP.objects.get_or_create(
                cluster=cluster,
                floating_ip=load_balancer.floating_ips.first(),
            )

        return cluster

    def _get_post_data(self):
        return processors.get_order_post_data(
            self.order,
            (
                "name",
                "description",
                "nodes",
                "tenant",
                "ssh_public_key",
                "install_longhorn",
                "security_groups",
                "vm_project",
            ),
        )

    def _create_self_managed_cluster(self, user):
        return self._trigger_cluster_creation(user, self._get_post_data())

    def _trigger_cluster_creation(self, user, data) -> rancher_models.Cluster:
        serializer = RancherClusterCreateSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        cluster: rancher_models.Cluster = serializer.save()
        nodes = serializer.validated_data.get("node_set")
        install_longhorn = serializer.validated_data["install_longhorn"]

        for node_data in nodes:
            node_data["cluster"] = cluster
            rancher_models.Node.objects.create(**node_data)

        transaction.on_commit(
            lambda: ClusterCreateExecutor.execute(
                cluster,
                user=user,
                install_longhorn=install_longhorn,
                is_heavy_task=True,
            )
        )

        return cluster

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
        project = Project.objects.create(
            customer=provider_customer,
            name=project_name,
            description="Automatically created project for Rancher cluster",
        )
        return cast(Project, project)

    def create_tenants(self, user, project: Project) -> list[os_models.Tenant]:
        tenants = []
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
            order_uuid = submit_creation_order(
                user, offering, plan, project, attributes, limits
            )
            order = Order.objects.get(uuid=order_uuid)
            tenant = cast(os_models.Tenant, order.resource.scope)
            tenants.append(tenant)

        return tenants

    def update_subnets(self, tenants: list[os_models.Tenant]):
        # Limit applocation pools for subnets used for Rancher nodes
        for tenant in tenants:
            subnet = (
                os_models.SubNet.objects.filter(tenant=tenant)
                .order_by("created", "id")
                .first()
            )
            if not subnet:
                continue

            network = ip_network(subnet.cidr, strict=False)
            first_host = network.network_address + const.OS_SUBNET_4_OCTET_START_IP
            last_host = network.network_address + const.OS_SUBNET_4_OCTET_END_IP

            subnet.allocation_pools = [
                {
                    "start": str(first_host),
                    "end": str(last_host),
                }
            ]
            subnet.save()

            os_executors.SubNetUpdateExecutor().execute(
                subnet, updated_fields=["allocation_pools"]
            )
        # Wait for subnets to be updated
        sleep(5)

    def create_cluster(
        self,
        user: User,
        project: Project,
        tenants: list[os_models.Tenant],
    ) -> rancher_models.Cluster:
        nodes = []

        worker_nodes_count = self.order.attributes["worker_nodes_count"]
        server_nodes_count = 3  # TODO: Make it configurable

        nodes = []
        for tenant in tenants:
            for _ in range(server_nodes_count):
                nodes.append(
                    self.format_node(role=SERVER_ROLE, tenant=tenant),
                )

            for _ in range(worker_nodes_count):
                nodes.append(
                    self.format_node(role=AGENT_ROLE, tenant=tenant),
                )

        attributes = {
            "name": f"k8s-{self.order.resource.slug}",
            "nodes": nodes,
            "install_longhorn": self.order.attributes.get("install_longhorn", False),
            "vm_project": reverse("project-detail", kwargs={"uuid": project.uuid.hex}),
        }
        return self._trigger_cluster_creation(user, attributes)

    def format_node(
        self,
        role: NodeRoleType,
        tenant: os_models.Tenant,
    ):
        rancher_offering = self.order.offering
        tenant_resource = Resource.objects.get(scope=tenant)

        os_service_settings = cast(ServiceSettings, tenant_resource.offering.scope)

        storage_mode = (
            tenant_resource.offering.plugin_options.get("storage_mode")
            or STORAGE_MODE_FIXED
        )

        if role == SERVER_ROLE:
            flavor = os_models.Flavor.objects.get(
                settings=os_service_settings,
                name=rancher_offering.plugin_options[
                    "managed_rancher_server_flavor_name"
                ],
            )

            system_volume_size = rancher_offering.plugin_options[
                "managed_rancher_server_system_volume_size_gb"
            ]
            data_volume_size = rancher_offering.plugin_options[
                "managed_rancher_server_data_volume_size_gb"
            ]
        else:
            flavor = os_models.Flavor.objects.get(
                settings=os_service_settings,
                name=self.order.attributes["worker_nodes_flavor_name"],
            )

            system_volume_size = rancher_offering.plugin_options[
                "managed_rancher_worker_system_volume_size_gb"
            ]
            data_volume_size = self.order.attributes["worker_nodes_data_volume_size"]

        subnet = (
            os_models.SubNet.objects.filter(tenant=tenant)
            .order_by("created", "id")
            .first()
        )
        if not subnet:
            raise rf_serializers.ValidationError(
                f'Subnets for tenant "{tenant.name}" not found'
            )

        result = {
            "role": role,
            "system_volume_size": system_volume_size * 1024,
            "flavor": reverse(
                "openstack-flavor-detail", kwargs={"uuid": flavor.uuid.hex}
            ),
            "subnet": reverse(
                "openstack-subnet-detail", kwargs={"uuid": subnet.uuid.hex}
            ),
            "tenant": reverse(
                "openstack-tenant-detail", kwargs={"uuid": tenant.uuid.hex}
            ),
        }
        data_volume_spec: dict[str, int | str] = {
            "size": data_volume_size,
            "mount_point": "/opt/rke2_storage",
            "filesystem": "btrfs",
        }
        if storage_mode == STORAGE_MODE_DYNAMIC:
            if role == SERVER_ROLE:
                system_volume_type_name = rancher_offering.plugin_options.get(
                    "managed_rancher_server_system_volume_type_name"
                )

                data_volume_type_name = rancher_offering.plugin_options.get(
                    "managed_rancher_server_data_volume_type_name"
                )
            else:
                system_volume_type_name = rancher_offering.plugin_options.get(
                    "managed_rancher_worker_system_volume_type_name"
                )
                data_volume_type_name = self.order.attributes.get(
                    "worker_nodes_data_volume_type_name"
                )

            system_volume_type = os_models.VolumeType.objects.filter(
                settings=os_service_settings,
                name=system_volume_type_name,
            ).first()
            if system_volume_type:
                result["system_volume_type"] = reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": system_volume_type.uuid.hex},
                )

            data_volume_type = os_models.VolumeType.objects.filter(
                settings=os_service_settings,
                name=data_volume_type_name,
            ).first()
            if data_volume_type:
                data_volume_spec["volume_type"] = reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": data_volume_type.uuid.hex},
                )
        result["data_volumes"] = [data_volume_spec]

        # Setup Longhorn volume if needed
        install_longhorn = self.order.attributes.get("install_longhorn", False)
        if install_longhorn and role == AGENT_ROLE:
            longhorn_volume_size = self.order.attributes[
                "worker_nodes_longhorn_volume_size"
            ]
            longhorn_volume_spec: dict[str, int | str] = {
                "size": longhorn_volume_size,
                "mount_point": "/opt/longhorn_storage",
                "filesystem": "btrfs",
            }
            if storage_mode == STORAGE_MODE_DYNAMIC:
                longhorn_volume_type_name = self.order.attributes.get(
                    "worker_nodes_longhorn_volume_type_name"
                )
                longhorn_volume_type = os_models.VolumeType.objects.filter(
                    settings=os_service_settings,
                    name=longhorn_volume_type_name,
                ).first()
                if longhorn_volume_type:
                    longhorn_volume_spec["volume_type"] = reverse(
                        "openstack-volume-type-detail",
                        kwargs={"uuid": longhorn_volume_type.uuid.hex},
                    )

            result["data_volumes"].append(longhorn_volume_spec)
        return result

    def create_security_groups(
        self,
        tenants: list[os_models.Tenant],
    ):
        view = os_views.TenantViewSet.as_view({"post": "create_security_group"})
        for tenant in tenants:
            for group in const.OS_LB_SECURITY_GROUPS:
                # Wait for tenant to become OK in case if it is being pulled
                wait_for_tenant(tenant.uuid)
                response = create_request(
                    view,
                    get_system_robot(),
                    {"name": group, "rules": []},
                    uuid=tenant.uuid.hex,
                )
                data = cast(dict, response.data)

                if response.status_code != status.HTTP_201_CREATED:
                    raise rf_serializers.ValidationError(data)

    def create_load_balancers(
        self,
        user: User,
        project: Project,
        tenants: list[os_models.Tenant],
    ) -> list[os_models.Instance]:
        instances = []
        for tenant in tenants:
            flavor_name = self.order.offering.plugin_options[
                "managed_rancher_load_balancer_flavor_name"
            ]
            try:
                flavor = os_models.Flavor.objects.get(
                    settings=tenant.service_settings, name=flavor_name
                )
            except os_models.Flavor.DoesNotExist:
                raise rf_serializers.ValidationError(
                    "Unable to create load balance because OpenStack flavor does not exist."
                )

            base_image_name = self.order.offering.secret_options["base_image_name"]
            try:
                image = os_models.Image.objects.get(
                    settings=tenant.service_settings, name=base_image_name
                )
            except os_models.Image.DoesNotExist:
                raise rf_serializers.ValidationError(
                    "Unable to create load balance because OpenStack image does not exist."
                )

            subnet = (
                os_models.SubNet.objects.filter(tenant=tenant)
                .order_by("created", "id")
                .first()
            )
            if not subnet:
                raise rf_serializers.ValidationError(
                    "Unable to create load balance because OpenStack subnet does not exist."
                )

            system_volume_size_gb = self.order.offering.plugin_options[
                "managed_rancher_load_balancer_system_volume_size_gb"
            ]
            system_volume_type_name = self.order.offering.plugin_options.get(
                "managed_rancher_load_balancer_system_volume_type_name"
            )

            data_volume_size_gb = self.order.offering.plugin_options[
                "managed_rancher_load_balancer_data_volume_size_gb"
            ]
            data_volume_type_name = self.order.offering.plugin_options.get(
                "managed_rancher_load_balancer_data_volume_type_name"
            )
            cloud_init_template = self.order.offering.secret_options.get(
                "managed_rancher_load_balancer_cloud_init_template"
            )

            system_volume_type = os_models.VolumeType.objects.get(
                settings=tenant.service_settings, name=system_volume_type_name
            )
            data_volume_type = os_models.VolumeType.objects.get(
                settings=tenant.service_settings, name=data_volume_type_name
            )
            security_groups = os_models.SecurityGroup.objects.filter(
                tenant=tenant,
                name__in=const.OS_LB_SECURITY_GROUPS + ["default"],
            )
            subnet_3_oct = subnet.cidr.rsplit(".", maxsplit=1)[0]
            cloud_init_scipt = cloud_init_template.format(subnet_3_oct=subnet_3_oct)

            network = ip_network(subnet.cidr, strict=False)
            lb_address = str(network.network_address + const.OS_LB_VM_4_OCTET_IP)

            post_data = {
                "name": f"{const.OS_LB_PREFIX}{self.order.resource.slug}",
                "flavor": reverse(
                    "openstack-flavor-detail", kwargs={"uuid": flavor.uuid.hex}
                ),
                "image": reverse(
                    "openstack-image-detail", kwargs={"uuid": image.uuid.hex}
                ),
                "service_settings": reverse(
                    "servicesettings-detail",
                    kwargs={"uuid": tenant.service_settings.uuid.hex},
                ),
                "tenant": reverse(
                    "openstack-tenant-detail", kwargs={"uuid": tenant.uuid.hex}
                ),
                "project": reverse("project-detail", kwargs={"uuid": project.uuid.hex}),
                "system_volume_size": system_volume_size_gb * 1024,
                "system_volume_type": reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": system_volume_type.uuid.hex},
                ),
                "data_volume_size": data_volume_size_gb * 1024,
                "data_volume_type": reverse(
                    "openstack-volume-type-detail",
                    kwargs={"uuid": data_volume_type.uuid.hex},
                ),
                "user_data": cloud_init_scipt,
                "security_groups": [
                    {
                        "url": reverse(
                            "openstack-sgp-detail", kwargs={"uuid": group.uuid.hex}
                        )
                    }
                    for group in security_groups
                ],
                "ports": [
                    {
                        "subnet": reverse(
                            "openstack-subnet-detail", kwargs={"uuid": subnet.uuid.hex}
                        ),
                        "fixed_ips": [
                            {
                                "subnet_id": subnet.backend_id,
                                "ip_address": lb_address,
                            }
                        ],
                    }
                ],
            }
            view = os_views.MarketplaceInstanceViewSet.as_view({"post": "create"})
            response = create_request(view, user, post_data)

            if response.status_code != status.HTTP_201_CREATED:
                raise exceptions.RancherException(response.data)

            data = cast(dict, response.data)
            instance_uuid = data["uuid"]
            instance = os_models.Instance.objects.get(uuid=instance_uuid)
            instances.append(instance)
        return instances

    def validate_order(self, request):
        if settings.WALDUR_RANCHER["READ_ONLY_MODE"]:
            raise rf_serializers.ValidationError(
                "Rancher integration is in read-only mode."
            )
        if (
            self.order.offering.plugin_options.get(
                "deployment_mode", const.DEPLOYMENT_MODE_SELF_MANAGED
            )
            == const.DEPLOYMENT_MODE_MANAGED
        ):
            serializer = serializers.ManagedClusterCreateSerializer(
                data=self.order.attributes
            )
            serializer.is_valid(raise_exception=True)

            available_service_settings = self.validate_openstack_offerings()
            self.validate_flavors(available_service_settings)
            self.validate_volume_types(available_service_settings)
            self.validate_limits()
        else:
            serializer = RancherClusterCreateSerializer(data=self._get_post_data())
            serializer.is_valid(raise_exception=True)

    def validate_flavors(self, available_service_settings: list[int]):
        worker_nodes_flavor_name = self.order.attributes["worker_nodes_flavor_name"]
        server_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_server_flavor_name"
        ]
        load_balancer_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_load_balancer_flavor_name"
        ]

        for service_setting in available_service_settings:
            for flavor_name in (
                worker_nodes_flavor_name,
                server_flavor_name,
                load_balancer_flavor_name,
            ):
                if not flavor_name:
                    continue
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
        server_data_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_server_data_volume_type_name"
        )
        worker_system_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_worker_system_volume_type_name"
        )
        worker_data_volume_type_name = self.order.attributes.get(
            "worker_nodes_data_volume_type_name"
        )
        worker_longhorn_volume_type_name = self.order.attributes.get(
            "worker_nodes_longhorn_volume_type_name"
        )
        load_balancer_system_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_load_balancer_system_volume_type_name"
        )
        load_balancer_data_volume_type_name = self.order.offering.plugin_options.get(
            "managed_rancher_load_balancer_data_volume_type_name"
        )

        for service_setting in available_service_settings:
            volume_type_list = [
                server_system_volume_type_name,
                server_data_volume_type_name,
                worker_system_volume_type_name,
                worker_data_volume_type_name,
                load_balancer_system_volume_type_name,
                load_balancer_data_volume_type_name,
            ]
            volume_type_list.append(worker_longhorn_volume_type_name)
            for volume_type_name in volume_type_list:
                if not volume_type_name:
                    continue
                if not os_models.VolumeType.objects.filter(
                    settings_id=service_setting, name=volume_type_name
                ).exists():
                    raise rf_serializers.ValidationError(
                        f"Volume type {volume_type_name} is not available in OpenStack offering {service_setting}"
                    )

    def validate_limits(self):
        aggregated_limits = collections.defaultdict(int)
        openstack_offering_uuid_list = cast(
            list[str], self.order.attributes["openstack_offering_uuid_list"]
        )
        for offering_uuid in openstack_offering_uuid_list:
            offering = Offering.objects.get(uuid=offering_uuid)
            limits = self.get_tenant_limits(offering)
            aggregated_limits[CORES_TYPE] += limits[CORES_TYPE]
            aggregated_limits[RAM_TYPE] += limits[RAM_TYPE]
            aggregated_limits[STORAGE_TYPE] += sum(
                limit_value
                for limit_type, limit_value in limits.items()
                if limit_type.startswith("gigabytes_") or limit_type == STORAGE_TYPE
            )

        type_to_max_limit = {
            CORES_TYPE: self.order.offering.plugin_options.get(
                "managed_rancher_tenant_max_cpu"
            ),
            RAM_TYPE: self.order.offering.plugin_options.get(
                "managed_rancher_tenant_max_ram"
            ),
            STORAGE_TYPE: self.order.offering.plugin_options.get(
                "managed_rancher_tenant_max_disk"
            ),
        }

        for limit_type, aggregated_limit in aggregated_limits.items():
            max_allowed_limit = type_to_max_limit[limit_type]

            if not max_allowed_limit:
                continue

            if limit_type != CORES_TYPE:
                # Convert GBs to MBs
                max_allowed_limit *= 1024

            if limit_type == CORES_TYPE:
                units = "cores"
            else:
                units = "MB"

            if aggregated_limit > max_allowed_limit:
                raise rf_serializers.ValidationError(
                    f"The requested total {limit_type} limit {aggregated_limit} {units} exceeds the maximum allowed {max_allowed_limit} for tenants."
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
                type=OPENSTACK_TENANT_OFFERING,
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
        server_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_server_flavor_name"
        ]
        load_balancer_flavor_name = self.order.offering.plugin_options[
            "managed_rancher_load_balancer_flavor_name"
        ]
        flavors = {}
        flavors.setdefault(worker_node_flavor_name, 0)
        flavors.setdefault(server_flavor_name, 0)
        flavors.setdefault(load_balancer_flavor_name, 0)
        flavors[worker_node_flavor_name] += worker_nodes_count
        flavors[server_flavor_name] += server_nodes_count
        flavors[load_balancer_flavor_name] += 1
        limits = {}
        for flavor_name, node_count in flavors.items():
            flavor = os_models.Flavor.objects.get(
                settings=service_settings,
                name=flavor_name,
            )
            limits.setdefault(CORES_TYPE, 0)
            limits.setdefault(RAM_TYPE, 0)
            limits[CORES_TYPE] += flavor.cores * node_count
            limits[RAM_TYPE] += flavor.ram * node_count

        server_system_volume_size_gb: int = self.order.offering.plugin_options[
            "managed_rancher_server_system_volume_size_gb"
        ]
        server_system_volume_type_name: str = self.order.offering.plugin_options.get(
            "managed_rancher_server_system_volume_type_name"
        )

        server_data_volume_size_gb: int = self.order.offering.plugin_options.get(
            "managed_rancher_server_data_volume_size_gb"
        )
        server_data_volume_type_name: str = self.order.offering.plugin_options.get(
            "managed_rancher_server_data_volume_type_name"
        )

        worker_system_volume_size_gb: int = self.order.offering.plugin_options.get(
            "managed_rancher_worker_system_volume_size_gb"
        )
        worker_system_volume_type_name: str = self.order.offering.plugin_options.get(
            "managed_rancher_worker_system_volume_type_name"
        )

        worker_data_volume_size_mb: int = self.order.attributes[
            "worker_nodes_data_volume_size"
        ]

        worker_data_volume_type_name: str = self.order.attributes.get(
            "worker_nodes_data_volume_type_name"
        )

        load_balancer_system_volume_size_gb: int = self.order.offering.plugin_options[
            "managed_rancher_load_balancer_system_volume_size_gb"
        ]
        load_balancer_system_volume_type_name: str = (
            self.order.offering.plugin_options.get(
                "managed_rancher_load_balancer_system_volume_type_name"
            )
        )

        load_balancer_data_volume_size_gb: int = self.order.offering.plugin_options[
            "managed_rancher_load_balancer_data_volume_size_gb"
        ]
        load_balancer_data_volume_type_name: str = (
            self.order.offering.plugin_options.get(
                "managed_rancher_load_balancer_data_volume_type_name"
            )
        )

        install_longhorn = self.order.attributes.get("install_longhorn", False)
        worker_longhorn_volume_size_mb = 0
        if install_longhorn:
            worker_longhorn_volume_size_mb = self.order.attributes[
                "worker_nodes_longhorn_volume_size"
            ]

        storage_mode = (
            os_offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        )
        if storage_mode == STORAGE_MODE_FIXED:
            total_storage = (
                (server_system_volume_size_gb + server_data_volume_size_gb)
                * server_nodes_count
                + (
                    worker_system_volume_size_gb
                    + (worker_data_volume_size_mb / 1024)
                    + (worker_longhorn_volume_size_mb / 1024)
                )
                * worker_nodes_count
                + load_balancer_system_volume_size_gb
                + load_balancer_data_volume_size_gb
            ) * 1024
            limits[STORAGE_TYPE] = total_storage
        else:
            volumes = [
                (
                    server_system_volume_type_name,
                    server_nodes_count * server_system_volume_size_gb,
                ),
                (
                    worker_system_volume_type_name,
                    worker_nodes_count * worker_system_volume_size_gb,
                ),
                (
                    server_data_volume_type_name,
                    server_nodes_count * server_data_volume_size_gb,
                ),
                (
                    worker_data_volume_type_name,
                    worker_nodes_count * (worker_data_volume_size_mb / 1024),
                ),
                (
                    load_balancer_system_volume_type_name,
                    load_balancer_system_volume_size_gb,
                ),
                (
                    load_balancer_data_volume_type_name,
                    load_balancer_data_volume_size_gb,
                ),
            ]
            if install_longhorn:
                worker_longhorn_volume_type_name: str = self.order.attributes.get(
                    "worker_nodes_longhorn_volume_type_name"
                )
                volumes.append(
                    (
                        worker_longhorn_volume_type_name,
                        worker_nodes_count * (worker_longhorn_volume_size_mb / 1024),
                    )
                )
            for volume_type_name, volume_size in volumes:
                volume_type_quota_name = volume_type_name_to_quota_name(
                    volume_type_name
                )
                limits.setdefault(volume_type_quota_name, 0)
                limits[volume_type_quota_name] += volume_size * 1024
        return limits

    @classmethod
    def get_resource_model(cls):
        return rancher_models.Cluster


class RancherDeleteProcessor(processors.AbstractDeleteResourceProcessor):
    def validate_order(self, request):
        cluster = cast(rancher_models.Cluster, self.get_resource().scope)
        for validator in ResourceViewSet.destroy_validators:
            validator(cluster)

    def send_request(self, user, resource: Resource):
        cluster = cast(rancher_models.Cluster, resource.scope)

        deployment_mode = self.order.offering.plugin_options.get(
            "deployment_mode", const.DEPLOYMENT_MODE_SELF_MANAGED
        )
        if deployment_mode == const.DEPLOYMENT_MODE_MANAGED:
            self.delete_managed_cluster(user, cluster)
        else:
            self.delete_self_managed_cluster(user, cluster)
        return False

    def delete_self_managed_cluster(self, user, cluster: rancher_models.Cluster):
        ClusterDeleteExecutor.execute(
            cluster,
            user=user,
            is_heavy_task=True,
        )

    def delete_managed_cluster(self, user, cluster: rancher_models.Cluster):
        self.delete_self_managed_cluster(user, cluster)
        for resource in Resource.objects.filter(
            object_id__in=cluster.linked_tenant_ids,
            content_type=ContentType.objects.get_for_model(os_models.Tenant),
        ):
            submit_termination_order(resource)
