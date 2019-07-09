from waldur_vmware import views as vmware_views
from waldur_mastermind.marketplace import processors


class VirtualMachineCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = vmware_views.VirtualMachineViewSet
    fields = (
        'name',
        'description',
        'guest_os',
        'cores',
        'cores_per_socket',
        'ram',
        'template',
        'cluster',
        'datastore',
    )
