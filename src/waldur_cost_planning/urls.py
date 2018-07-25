from . import views


def register_in(router):
    router.register(r'deployment-plans', views.DeploymentPlanViewSet, base_name='deployment-plan')
    router.register(r'deployment-presets', views.PresetViewSet, base_name='deployment-preset')
