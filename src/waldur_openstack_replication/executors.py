from celery import chain

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.executors import CreateExecutor
from waldur_core.core.tasks import StateTransitionTask
from waldur_openstack import models as openstack_models
from waldur_openstack.executors import get_tenant_create_tasks

from . import models, tasks


def get_create_ports_tasks(src_tenant, dst_tenant, network_uuids=None):
    creation_tasks = []

    src_networks = src_tenant.networks.all()

    if network_uuids:
        src_networks = src_networks.filter(uuid__in=network_uuids)

    for src_network in src_networks:
        dst_network = openstack_models.Network.objects.filter(
            tenant=dst_tenant, name=src_network.name
        ).first()

        if not dst_network:
            continue

        for src_subnet in src_network.subnets.all():
            dst_subnet = openstack_models.SubNet.objects.filter(
                tenant=dst_tenant, name=src_subnet.name
            ).first()

            if not dst_subnet:
                continue

            # ports connected to instances
            instance_ports = src_subnet.ports.exclude(instance__isnull=True).filter(
                instance__state=CoreStates.OK
            )

            # ports in DOWN state not connected to anything, e.g. for VIPs
            free_ports = src_subnet.ports.filter(
                instance__isnull=True,
                admin_state_up=True,
                device_owner="compute:nova",
                status="DOWN",
            )

            src_ports = (instance_ports | free_ports).distinct()

            for src_port in src_ports:
                dst_port = openstack_models.Port.objects.create(
                    name=src_port.name,
                    description=src_port.description,
                    service_settings=dst_tenant.service_settings,
                    project=dst_tenant.project,
                    tenant=dst_tenant,
                    network=dst_network,
                    port_security_enabled=src_port.port_security_enabled,
                    subnet=dst_subnet,
                    fixed_ips=src_port.fixed_ips,
                    mac_address=src_port.mac_address,
                )

                if src_port.security_groups.exists():
                    for src_sg in src_port.security_groups.all():
                        try:
                            dst_sg = openstack_models.SecurityGroup.objects.filter(
                                tenant=dst_tenant, name=src_sg.name
                            ).first()

                            if dst_sg:
                                dst_port.security_groups.add(dst_sg)
                        except Exception:
                            pass

            for port in dst_tenant.ports.all():
                creation_tasks.append(
                    tasks.CreateReplicatedPortTask().si(
                        core_utils.serialize_instance(port)
                    )
                )

    return chain(*creation_tasks)


class MigrationExecutor(CreateExecutor):
    @classmethod
    def get_task_signature(
        cls, migration: models.Migration, serialized_migration, **kwargs
    ):
        creation_tasks = [
            StateTransitionTask().si(
                serialized_migration,
                state_transition="begin_creating",
            ),
            get_tenant_create_tasks(
                migration.dst_resource.scope,
                migration.mappings.get("skip_connection_extnet", False),
            ),
        ]
        if migration.mappings.get("sync_instance_ports", False):
            network_uuids = [
                network.uuid.hex
                for network in migration.mappings.get("mappings", {}).pop(
                    "networks", []
                )
            ]
            creation_tasks.append(
                get_create_ports_tasks(
                    migration.src_resource.scope,
                    migration.dst_resource.scope,
                    network_uuids,
                )
            )

        return chain(*creation_tasks)
