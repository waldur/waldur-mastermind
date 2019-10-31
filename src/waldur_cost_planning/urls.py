from . import views


def register_in(router):
    router.register(r'deployment-plans', views.DeploymentPlanViewSet, basename='deployment-plan')
    router.register(r'deployment-presets', views.PresetViewSet, basename='deployment-preset')
