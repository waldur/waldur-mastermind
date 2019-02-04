from . import views


def register_in(router):
    router.register(r'azure', views.AzureServiceViewSet, base_name='azure')
    router.register(r'azure-images', views.ImageViewSet, base_name='azure-image')
    router.register(r'azure-sizes', views.SizeViewSet, base_name='azure-size')
    router.register(r'azure-locations', views.LocationViewSet, base_name='azure-location')
    router.register(r'azure-resource-groups', views.ResourceGroupViewSet, base_name='azure-resource-group')
    router.register(r'azure-virtualmachines', views.VirtualMachineViewSet, base_name='azure-virtualmachine')
    router.register(r'azure-public-ips', views.PublicIPViewSet, base_name='azure-public-ip')
    router.register(r'azure-sql-servers', views.SQLServerViewSet, base_name='azure-sql-server')
    router.register(r'azure-sql-databases', views.SQLDatabaseViewSet, base_name='azure-sql-database')
    router.register(r'azure-service-project-link',
                    views.AzureServiceProjectLinkViewSet, base_name='azure-spl')
