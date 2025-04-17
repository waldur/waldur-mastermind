import logging

from django.db.models import ObjectDoesNotExist

from waldur_core.core import tasks as core_tasks
from waldur_openstack import models as openstack_models

logger = logging.getLogger(__name__)


class CreateReplicatedPortTask(core_tasks.Task):
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
