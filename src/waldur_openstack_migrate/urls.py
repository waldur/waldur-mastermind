from . import views


def register_in(router):
    router.register(
        r"openstack-migrations", views.MigrationViewSet, basename="openstack-migrations"
    )
