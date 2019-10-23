from . import views


def register_in(router):
    router.register(r'azure', views.AzureServiceViewSet, basename='azure')
    router.register(r'azure-images', views.ImageViewSet, basename='azure-image')
    router.register(r'azure-sizes', views.SizeViewSet, basename='azure-size')
    router.register(r'azure-locations', views.LocationViewSet, basename='azure-location')
    router.register(r'azure-resource-groups', views.ResourceGroupViewSet, basename='azure-resource-group')
    router.register(r'azure-virtualmachines', views.VirtualMachineViewSet, basename='azure-virtualmachine')
    router.register(r'azure-public-ips', views.PublicIPViewSet, basename='azure-public-ip')
    router.register(r'azure-sql-servers', views.SQLServerViewSet, basename='azure-sql-server')
    router.register(r'azure-sql-databases', views.SQLDatabaseViewSet, basename='azure-sql-database')
    router.register(r'azure-service-project-link',
                    views.AzureServiceProjectLinkViewSet, basename='azure-spl')
