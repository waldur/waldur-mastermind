from . import views


def register_in(router):
    router.register(r'digitalocean', views.DigitalOceanServiceViewSet, basename='digitalocean')
    router.register(r'digitalocean-images', views.ImageViewSet, basename='digitalocean-image')
    router.register(r'digitalocean-regions', views.RegionViewSet, basename='digitalocean-region')
    router.register(r'digitalocean-sizes', views.SizeViewSet, basename='digitalocean-size')
    router.register(r'digitalocean-droplets', views.DropletViewSet, basename='digitalocean-droplet')
    router.register(r'digitalocean-service-project-link',
                    views.DigitalOceanServiceProjectLinkViewSet, basename='digitalocean-spl')
