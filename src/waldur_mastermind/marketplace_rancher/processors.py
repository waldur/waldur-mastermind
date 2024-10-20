from waldur_mastermind.marketplace import processors
from waldur_rancher import views as rancher_views


class RancherCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = rancher_views.ClusterViewSet
    fields = (
        "name",
        "description",
        "nodes",
        "tenant",
        "ssh_public_key",
        "install_longhorn",
        "security_groups",
    )


class RancherDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = rancher_views.ClusterViewSet
