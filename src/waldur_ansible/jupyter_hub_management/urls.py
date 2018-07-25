from . import views, models


def register_in(router):
    router.register(r'jupyter-hub-management', views.JupyterHubManagementViewSet, base_name=models.JupyterHubManagement.get_url_name())
