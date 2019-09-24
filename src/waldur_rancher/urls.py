from . import views


def register_in(router):
    router.register(r'rancher', views.ServiceViewSet, base_name='rancher')
    router.register(r'rancher-spl', views.ServiceProjectLinkViewSet,
                    base_name='rancher-spl')
    router.register(r'rancher-clusters', views.ClusterViewSet,
                    base_name='rancher-cluster')
    router.register(r'rancher-nodes', views.NodeViewSet,
                    base_name='rancher-node')
