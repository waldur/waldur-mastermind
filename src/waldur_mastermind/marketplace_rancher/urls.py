from waldur_mastermind.marketplace_rancher import views


def register_in(router):
    router.register(
        "managed-rancher-cluster-resources",
        views.ManagedRancherViewSet,
        basename="managed-rancher-cluster-resource",
    )


urlpatterns = []
