from . import views


def register_in(router):
    router.register(r'aws-regions', views.RegionViewSet, basename='aws-region')
    router.register(r'aws-images', views.ImageViewSet, basename='aws-image')
    router.register(r'aws-sizes', views.SizeViewSet, basename='aws-size')
    router.register(r'aws-instances', views.InstanceViewSet, basename='aws-instance')
    router.register(r'aws-volumes', views.VolumeViewSet, basename='aws-volume')
