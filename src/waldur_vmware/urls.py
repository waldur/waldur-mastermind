from . import views


def register_in(router):
    router.register(r'vmware', views.ServiceViewSet, base_name='vmware')
    router.register(r'vmware-service-project-link', views.ServiceProjectLinkViewSet,
                    base_name='vmware-spl')
    router.register(r'vmware-virtual-machine', views.VirtualMachineViewSet,
                    base_name='vmware-virtual-machine')
