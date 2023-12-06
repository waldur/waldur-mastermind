from waldur_mastermind.marketplace import processors
from waldur_vmware import views as vmware_views


class VirtualMachineCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = vmware_views.VirtualMachineViewSet
    fields = (
        'name',
        'description',
        'guest_os',
        'cores_per_socket',
        'template',
        'cluster',
        'datastore',
    )

    def get_post_data(self):
        payload = super().get_post_data()

        limits = self.order.limits
        if limits:
            if 'cpu' in limits:
                payload['cores'] = limits['cpu']
            if 'ram' in limits:
                payload['ram'] = limits['ram']
        return payload
