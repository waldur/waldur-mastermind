from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, status

from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace_vmware import CPU_TYPE, DISK_TYPE, RAM_TYPE
from waldur_vmware import views as vmware_views


class VirtualMachineCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = vmware_views.VirtualMachineViewSet
    fields = (
        "name",
        "description",
        "guest_os",
        "cores_per_socket",
        "template",
        "cluster",
        "datastore",
    )

    def get_post_data(self):
        payload = super().get_post_data()

        limits = self.order.limits
        if limits:
            if CPU_TYPE in limits:
                payload["cores"] = limits[CPU_TYPE]
            if RAM_TYPE in limits:
                payload["ram"] = limits[RAM_TYPE]
        return payload


class VirtualMachineUpdateProcessor(processors.UpdateScopedResourceProcessor):
    """Apply a limit change to the virtual machine behind the resource.

    CPU and RAM are pushed to vCenter by the plugin's own update endpoint, so
    that its validators (the VM must be OK and powered off) and its executor
    apply. Disk is a limit too, but it is the sum of the VM's disks, which are
    resources of their own with their own endpoints -- so a change to it is
    rejected here rather than silently dropped.
    """

    def get_view(self):
        return vmware_views.VirtualMachineViewSet.as_view({"patch": "partial_update"})

    def get_post_data(self):
        limits = self.order.limits
        payload = {}
        if CPU_TYPE in limits:
            payload["cores"] = limits[CPU_TYPE]
        if RAM_TYPE in limits:
            payload["ram"] = limits[RAM_TYPE]
        return payload

    def validate_order(self, request):
        super().validate_order(request)

        if DISK_TYPE not in self.order.limits:
            return

        # What the resource carries now, rather than what the machine reports:
        # the two can differ until the first pull lands, and an order that
        # leaves the disk alone must not be refused over that difference.
        current = (self.order.resource.limits or {}).get(DISK_TYPE)
        if current is None:
            vm = self.get_resource()
            current = vm.total_disk if vm else None

        if current is not None and self.order.limits[DISK_TYPE] != current:
            raise serializers.ValidationError(
                _(
                    "Disk size cannot be changed via limits, "
                    "because it is defined by the disks attached to the virtual machine."
                )
            )

    def send_request(self, user):
        # Reached for a plan switch, which has nothing to push to vCenter: the
        # plan is a billing matter, and the machine's hardware follows its
        # limits. Reporting it as done completes the order.
        return True

    def update_limits_process(self, user):
        vm = self.get_resource()
        if not vm:
            raise serializers.ValidationError(
                _("Virtual machine is not found for the resource.")
            )

        payload = self.get_post_data()
        if not payload:
            return True

        response = common_utils.update_request(
            self.get_view(), user, payload, uuid=vm.uuid.hex
        )
        if response.status_code != status.HTTP_200_OK:
            raise serializers.ValidationError(response.data)

        # The update is asynchronous: the order is completed when the virtual
        # machine leaves the updating state, and the limits of the marketplace
        # resource are refreshed from the VM by the vm_updated handler.
        return False


class VirtualMachineDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = vmware_views.VirtualMachineViewSet
