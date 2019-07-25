from . import views


def register_in(router):
    router.register(r'vmware', views.ServiceViewSet, base_name='vmware')
    router.register(r'vmware-limits', views.LimitViewSet, base_name='vmware-limit')
    router.register(r'vmware-service-project-link', views.ServiceProjectLinkViewSet,
                    base_name='vmware-spl')
    router.register(r'vmware-virtual-machine', views.VirtualMachineViewSet,
                    base_name='vmware-virtual-machine')
    router.register(r'vmware-disks', views.DiskViewSet,
                    base_name='vmware-disk')
    router.register(r'vmware-ports', views.PortViewSet,
                    base_name='vmware-port')
    router.register(r'vmware-templates', views.TemplateViewSet,
                    base_name='vmware-template')
    router.register(r'vmware-clusters', views.ClusterViewSet,
                    base_name='vmware-cluster')
    router.register(r'vmware-networks', views.NetworkViewSet,
                    base_name='vmware-network')
    router.register(r'vmware-datastores', views.DatastoreViewSet,
                    base_name='vmware-datastore')
    router.register(r'vmware-folders', views.FolderViewSet,
                    base_name='vmware-folder')
