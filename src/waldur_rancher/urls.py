from . import views


def register_in(router):
    router.register(r'rancher', views.ServiceViewSet, basename='rancher')
    router.register(r'rancher-spl', views.ServiceProjectLinkViewSet,
                    basename='rancher-spl')
    router.register(r'rancher-clusters', views.ClusterViewSet,
                    basename='rancher-cluster')
    router.register(r'rancher-nodes', views.NodeViewSet,
                    basename='rancher-node')
