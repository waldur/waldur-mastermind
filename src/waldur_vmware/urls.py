from . import views


def register_in(router):
    router.register(r'vmware', views.ServiceViewSet, basename='vmware')
    router.register(r'vmware-limits', views.LimitViewSet, basename='vmware-limit')
    router.register(r'vmware-service-project-link', views.ServiceProjectLinkViewSet,
                    basename='vmware-spl')
    router.register(r'vmware-virtual-machine', views.VirtualMachineViewSet,
                    basename='vmware-virtual-machine')
    router.register(r'vmware-disks', views.DiskViewSet,
                    basename='vmware-disk')
    router.register(r'vmware-ports', views.PortViewSet,
                    basename='vmware-port')
    router.register(r'vmware-templates', views.TemplateViewSet,
                    basename='vmware-template')
    router.register(r'vmware-clusters', views.ClusterViewSet,
                    basename='vmware-cluster')
    router.register(r'vmware-networks', views.NetworkViewSet,
                    basename='vmware-network')
    router.register(r'vmware-datastores', views.DatastoreViewSet,
                    basename='vmware-datastore')
    router.register(r'vmware-folders', views.FolderViewSet,
                    basename='vmware-folder')
