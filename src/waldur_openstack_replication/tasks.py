import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import tasks as core_tasks
from waldur_openstack import models as openstack_models

logger = logging.getLogger(__name__)


class CreateReplicatedPortTask(core_tasks.Task):
    def run(self, port_data, *args, **kwargs):
        """Create and configure a replicated port from the provided data"""
        try:
            # Get the destination objects
            dst_tenant = openstack_models.Tenant.objects.get(
                id=port_data["dst_tenant_id"]
            )
            dst_network = openstack_models.Network.objects.get(
                id=port_data["dst_network_id"]
            )
            dst_subnet = openstack_models.SubNet.objects.get(
                id=port_data["dst_subnet_id"]
            )

            # Create the port
            dst_port = openstack_models.Port.objects.create(
                name=port_data["name"],
                description=port_data["description"],
                service_settings=dst_tenant.service_settings,
                project=dst_tenant.project,
                tenant=dst_tenant,
                network=dst_network,
                port_security_enabled=port_data["port_security_enabled"],
                subnet=dst_subnet,
                fixed_ips=port_data["fixed_ips"],
                mac_address=port_data["mac_address"],
            )

            # Add security groups
            for sg_name in port_data.get("security_group_names", []):
                try:
                    dst_sg = openstack_models.SecurityGroup.objects.filter(
                        tenant=dst_tenant, name=sg_name
                    ).first()
                    if dst_sg:
                        dst_port.security_groups.add(dst_sg)
                except Exception as e:
                    logger.warning(
                        "Failed to add security group %s to port %s: %s",
                        sg_name,
                        dst_port.name,
                        str(e),
                    )

            return self.execute(dst_port, *args, **kwargs)

        except ObjectDoesNotExist as e:
            logger.warning(
                "Required objects for port creation not found. Port data: %s. Error: %s",
                port_data,
                str(e),
            )
            return
        except Exception as e:
            logger.error(
                "Unexpected error creating replicated port. Port data: %s. Error: %s",
                port_data,
                str(e),
            )
            raise

    def execute(self, dst_port):
        for fixed_ip in dst_port.fixed_ips:
            src_subnet_backend_id = fixed_ip.get("subnet_id")

            if not src_subnet_backend_id:
                continue

            try:
                src_subnet = openstack_models.SubNet.objects.get(
                    backend_id=src_subnet_backend_id
                )
                dst_subnet = openstack_models.SubNet.objects.get(
                    tenant=dst_port.tenant, name=src_subnet.name
                )
                fixed_ip["subnet_id"] = dst_subnet.backend_id
            except ObjectDoesNotExist:
                continue

            backend = dst_port.tenant.get_backend()
            security_groups = list(
                dst_port.security_groups.values_list("backend_id", flat=True)
            )
            backend.create_instance_port(dst_port, security_groups)
