from waldur_mastermind.marketplace import processors
from waldur_rancher import views as rancher_views


class RancherCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = rancher_views.ClusterViewSet
    fields = (
        'name',
        'description',
        'nodes',
        'tenant_settings',
        'ssh_public_key',
    )


class RancherDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = rancher_views.ClusterViewSet
