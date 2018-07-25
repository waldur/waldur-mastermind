from . import views


def register_in(router):
    router.register(r'aws', views.AmazonServiceViewSet, base_name='aws')
    router.register(r'aws-regions', views.RegionViewSet, base_name='aws-region')
    router.register(r'aws-images', views.ImageViewSet, base_name='aws-image')
    router.register(r'aws-sizes', views.SizeViewSet, base_name='aws-size')
    router.register(r'aws-instances', views.InstanceViewSet, base_name='aws-instance')
    router.register(r'aws-volumes', views.VolumeViewSet, base_name='aws-volume')
    router.register(r'aws-service-project-link',
                    views.AmazonServiceProjectLinkViewSet, base_name='aws-spl')
